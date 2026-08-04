"""Node-locked-лицензирование для FastAPI-версии.

Портирован из Django-версии (``src/_project/licensing.py``) без изменения
модели защиты и — что важно — БЕЗ смены ключей и формата файла лицензии,
чтобы уже выпущенные ключи продолжали действовать.

Модель защиты и честные ограничения — те же:
  * привязка исполнения к конкретному хосту (Linux ``machine-id``) с
    необязательной датой окончания; подпись Ed25519;
  * это поднимает планку против «скопировал папку на другой сервер и запустил»,
    но не является абсолютной защитой: root с исходниками может вырезать
    проверку. Поэтому в проде код запечён в образ, а не лежит редактируемым
    ``.py`` (см. Dockerfile).

Отличие от Django-порта: здесь нет ни middleware Django, ни ``sys.argv``-эвристик
для management-команд. Enforcement выполняется ASGI-middleware, которое отдаёт
503 на всё, кроме ``/license``, пока ключ недействителен — ровно как раньше,
чтобы клиент мог самостоятельно ввести новый ключ на заблокированном сервисе.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

# --- Публичный ключ разработчика (Ed25519, 32 байта hex). Приватный НИКОГДА
# не хранится в приложении. Значение совпадает с Django-версией, поэтому
# ключи, выпущенные ранее, остаются валидными.
PUBLIC_KEY_HEX = "3e6b59c600988d00eff3cb63fee3fd65ac8599f2b9fa511bf0e5693c8727da38"

# Соль фиксирована, чтобы raw machine-id нельзя было тривиально сопоставить с
# host_id. ДОЛЖНА совпадать с tools/make_license.py и Django-версией.
_HOST_SALT = "cbmail-license::v1"

_DEFAULT_LICENSE_FILENAME = "license.key"

# Кэшируем результат проверки, чтобы per-request middleware было дешёвым.
_cache: dict[str, object] = {"ok": None, "reason": "", "ts": 0.0}
_CACHE_TTL = 300.0

# Пути, доступные всегда — даже при недействительной лицензии, чтобы клиент мог
# ввести новый ключ на заблокированном сервисе.
LICENSE_ALLOW_PREFIXES = ("/license",)


def host_id_from_machine_id(machine_id: str) -> str:
    """Детерминированный host_id из raw machine-id (та же формула, что в генераторе)."""
    return hashlib.sha256(
        (_HOST_SALT + "::" + (machine_id or "").strip()).encode()
    ).hexdigest()


def _read_machine_id() -> str:
    # В docker хостовый /etc/machine-id монтируется в /etc/host-machine-id,
    # чтобы отпечаток отслеживал физический сервер, а не эфемерный id контейнера.
    for path in ("/etc/host-machine-id", "/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, encoding="utf-8") as fh:
                val = fh.read().strip()
                if val:
                    return val
        except OSError:
            continue
    return ""


def compute_host_id() -> str:
    return host_id_from_machine_id(_read_machine_id())


def _license_path() -> str:
    env_path = os.environ.get("LICENSE_FILE")
    if env_path:
        return env_path
    # По умолчанию — рядом с рабочим каталогом приложения.
    return os.path.join(os.getcwd(), _DEFAULT_LICENSE_FILENAME)


def _load_public_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not PUBLIC_KEY_HEX or PUBLIC_KEY_HEX.startswith("__"):
        raise ValueError("public key not configured")
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY_HEX))


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_license(force: bool = False) -> tuple[bool, str]:
    """Вернуть (ok, reason). Fail-closed при любой ошибке."""
    now = time.time()
    if not force and _cache["ok"] is not None and (now - float(_cache["ts"])) < _CACHE_TTL:
        return bool(_cache["ok"]), str(_cache["reason"])

    ok, reason = _verify_uncached()
    _cache.update(ok=ok, reason=reason, ts=now)
    return ok, reason


def _verify_uncached() -> tuple[bool, str]:
    try:
        from cryptography.exceptions import InvalidSignature
    except Exception as exc:  # cryptography отсутствует => fail closed
        return False, f"crypto backend unavailable: {exc}"

    path = _license_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return False, f"license file not found: {path}"
    except (OSError, ValueError) as exc:
        return False, f"license file unreadable: {exc}"

    payload = doc.get("payload")
    sig_b64 = doc.get("sig")
    if not isinstance(payload, dict) or not sig_b64:
        return False, "license malformed"

    try:
        pub = _load_public_key()
        pub.verify(base64.b64decode(sig_b64), _canonical(payload))
    except InvalidSignature:
        return False, "license signature invalid"
    except Exception as exc:
        return False, f"license verify error: {exc}"

    if payload.get("host_id") != compute_host_id():
        return False, "license not valid for this host"

    # Timed-ключ: срок отсчитывается от МОМЕНТА ВВОДА (activated_at), а не от
    # календарной даты. activated_at проставляется install_license при активации
    # и живёт в файле лицензии (том. монтируется rw), поэтому переживает
    # перезапуск контейнера — 6 часов идут непрерывно с момента ввода.
    if _is_timed(payload):
        return _verify_timed(doc, payload)

    # Классический ключ: абсолютная дата окончания (или бессрочный при null).
    expires = payload.get("expires")
    if expires:
        try:
            if date.today() > date.fromisoformat(expires):
                return False, f"license expired on {expires}"
        except ValueError:
            return False, "license expiry malformed"

    return True, "ok"


def _is_timed(payload: dict) -> bool:
    return payload.get("mode") == "timed" or payload.get("duration_hours") is not None


def _timed_expiry(doc: dict) -> datetime | None:
    activated = doc.get("activated_at")
    if not activated:
        return None
    try:
        activated_dt = datetime.fromisoformat(activated)
    except ValueError:
        return None
    if activated_dt.tzinfo is None:
        activated_dt = activated_dt.replace(tzinfo=timezone.utc)
    hours = float(doc.get("payload", {}).get("duration_hours") or 0)
    return activated_dt + timedelta(hours=hours)


def _verify_timed(doc: dict, payload: dict) -> tuple[bool, str]:
    expiry = _timed_expiry(doc)
    if expiry is None:
        return False, "timed license not activated"
    now = datetime.now(timezone.utc)
    if now > expiry:
        return False, f"license expired at {expiry:%Y-%m-%d %H:%M UTC}"
    remaining = expiry - now
    total_min = int(remaining.total_seconds() // 60)
    return True, f"ok — активна ещё {total_min // 60} ч {total_min % 60} мин (до {expiry:%d.%m %H:%M UTC})"


def install_license(text: str) -> tuple[bool, str]:
    """Проверить вставленный ключ (JSON) и, если он валиден для ЭТОГО хоста и не
    истёк, записать в файл лицензии. Возвращает (ok, человекочитаемая_причина)."""
    text = (text or "").strip()
    if not text:
        return False, "пустой ключ"
    try:
        doc = json.loads(text)
    except ValueError:
        return False, "ключ не является корректным JSON"

    payload = doc.get("payload")
    sig_b64 = doc.get("sig")
    if not isinstance(payload, dict) or not sig_b64:
        return False, "ключ повреждён (нет payload/sig)"

    try:
        from cryptography.exceptions import InvalidSignature

        pub = _load_public_key()
        pub.verify(base64.b64decode(sig_b64), _canonical(payload))
    except InvalidSignature:
        return False, "подпись ключа недействительна"
    except Exception as exc:
        return False, f"ошибка проверки ключа: {exc}"

    if payload.get("host_id") != compute_host_id():
        return False, "ключ выдан для другого сервера"

    out: dict = {"payload": payload, "sig": sig_b64}
    if _is_timed(payload):
        # Момент ввода — точка отсчёта 6 часов. Фиксируем в файле лицензии.
        out["activated_at"] = datetime.now(timezone.utc).isoformat()
    else:
        expires = payload.get("expires")
        if expires:
            try:
                if date.today() > date.fromisoformat(expires):
                    return False, f"ключ уже истёк ({expires})"
            except ValueError:
                return False, "некорректная дата в ключе"

    path = _license_path()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        return False, (
            f"не удалось сохранить ключ ({exc}); файл лицензии должен быть доступен на запись"
        )

    _cache.update(ok=None, reason="", ts=0.0)  # сбрасываем кэш проверки
    return True, "ok"


def license_status() -> dict:
    """Сводка для страницы активации и диагностики."""
    ok, reason = verify_license(force=True)
    return {"ok": ok, "reason": reason, "host_id": compute_host_id()}
