"""Почему картинка не дошла до получателя — проверка всех картинок в письмах.

«Картинки не доезжают» — жалоба, которую нельзя проверить глазами: в редакторе
и в предпросмотре картинка видна (браузер ходит за ней с вашего компьютера, где
вы уже вошли в сервис), а у получателя на её месте подпись. Причины разные и
снаружи неразличимы:

* адрес недоступен из интернета — картинка лежит там, куда почта не дотянется;
* адрес относительный (``/media/...``) — в письме так нельзя, нужен полный;
* хост отдаёт картинку браузеру, но не отдаёт почтовому прокси;
* картинка доступна, но письмо попало в спам — тогда Gmail не грузит её сам,
  и дело не в картинке вовсе (проверьте папку «Спам» у получателя).

Скрипт обходит картинки во всех шаблонах и кампаниях и ходит за каждой так же,
как это делает Gmail — его прокси, а не браузер. Заодно показывает подпись
(alt): её читает получатель, когда картинки отключены, а в Outlook они
отключены по умолчанию.

Запуск на сервере (файл кладёт деплой, внутрь образа он не попадает):
    docker cp _config/check-images.py cbmail-web:/tmp/check-images.py
    docker exec cbmail-web python /tmp/check-images.py

Код возврата всегда 0: это отчёт, а не проверка.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict

#: Так за картинкой ходит Gmail. Браузерный запрос тут не годится: часть
#: хостингов пускает браузер и отбивает прокси, и проверка «в браузере
#: открывается» ничего не доказывает.
GMAIL_PROXY_UA = (
    "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
)

TIMEOUT_SECONDS = 15

#: Подписи, которые оставил конструктор, а не человек: получатель с
#: отключёнными картинками читает именно их.
PLACEHOLDER_ALTS = {"sample product", "image", "img", "picture", "photo"}

#: Мёртвая ссылка отписки: абсолютный адрес, где есть и слово unsubscribe, и
#: подстановка адреса получателя. Такую кнопку оставила старая система, и
#: ведёт она в никуда. Получатель жмёт «Отписаться», ничего не происходит — и
#: жмёт «Спам», а это бьёт по репутации домена сильнее любой вёрстки.
#:
#: Оба условия обязательны. Рабочая ссылка сервиса — это плейсхолдер
#: {{unsubscribe_url}}, адреса получателя в нём нет; а одного слова
#: unsubscribe мало, под него попала бы живая страница отписки на сайте.
_HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_EMAIL_PLACEHOLDER_RE = re.compile(r"\{\{\s*email\s*\}\}", re.IGNORECASE)

_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_ALT_RE = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def _attr(pattern: re.Pattern[str], tag: str) -> str:
    match = pattern.search(tag)
    if match is None:
        return ""
    return (match.group(1) if match.group(1) is not None else match.group(2)) or ""


def find_images(html: str | None) -> list[tuple[str, str]]:
    """Пары «адрес, подпись» для всех картинок разметки."""
    if not html:
        return []
    return [(_attr(_SRC_RE, tag).strip(), _attr(_ALT_RE, tag).strip()) for tag in _IMG_RE.findall(html)]


def find_dead_unsubscribe(html: str | None) -> list[str]:
    """Ссылки отписки, оставшиеся от старой системы."""
    if not html:
        return []
    out: list[str] = []
    for match in _HREF_RE.finditer(html):
        href = ((match.group(1) if match.group(1) is not None else match.group(2)) or "").strip()
        low = href.lower()
        if (
            low.startswith(("http://", "https://"))
            and "unsubscribe" in low
            and _EMAIL_PLACEHOLDER_RE.search(href)
        ):
            out.append(href)
    return out


def check_url(url: str) -> tuple[bool, str]:
    """Доступна ли картинка почтовому клиенту. Возвращает (ок, пояснение)."""
    if not url:
        return False, "адрес пустой — картинка не выбрана"
    low = url.lower()
    if low.startswith("data:"):
        return False, "картинка встроена в письмо (data:) — такие почта обычно не показывает"
    if low.startswith("cid:"):
        return True, "вложение письма (cid:) — проверяется только отправкой"
    if not low.startswith(("http://", "https://")):
        return False, "адрес относительный — в письме нужен полный, с https://"

    request = urllib.request.Request(url, method="GET", headers={"User-Agent": GMAIL_PROXY_UA})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            # Читаем немного: важно, что отдаётся картинка, а не страница
            # входа — её сервер тоже вернёт с кодом 200.
            chunk = response.read(512)
            if not ctype.startswith("image/"):
                return False, f"отдаётся не картинка, а {ctype or 'непонятно что'} (вход в сервис?)"
            if not chunk:
                return False, "файл пустой"
            return True, f"{ctype}"
    except urllib.error.HTTPError as exc:
        hint = " — файла нет" if exc.code == 404 else " — закрыта для посторонних" if exc.code in (401, 403) else ""
        return False, f"HTTP {exc.code}{hint}"
    except urllib.error.URLError as exc:
        return False, f"не достучаться: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 — сообщение важнее разбора причины
        return False, f"ошибка запроса: {exc}"


def main() -> int:
    try:
        from cbmail.db.models import Campaign, MessageTemplate
        from cbmail.db.session import get_sync_engine
        from sqlalchemy import select
        from sqlalchemy.orm import Session as OrmSession
    except Exception as exc:  # noqa: BLE001 — сообщение важнее разбора причины
        print(f"Проверить картинки не удалось: {exc}")
        return 0

    # Где встречается каждый адрес: один и тот же файл обычно стоит в
    # нескольких письмах, а ходить за ним повторно незачем.
    places: dict[str, list[str]] = defaultdict(list)
    alts: dict[str, set[str]] = defaultdict(set)
    dead_unsub: dict[str, list[str]] = defaultdict(list)

    with OrmSession(get_sync_engine()) as session:
        for tpl in session.execute(select(MessageTemplate).order_by(MessageTemplate.id)).scalars():
            where = f"шаблон #{tpl.id} «{tpl.name or 'без названия'}»"
            for url, alt in find_images(tpl.mask):
                places[url].append(where)
                alts[url].add(alt)
            for href in find_dead_unsubscribe(tpl.mask):
                dead_unsub[href].append(where)
        for camp in session.execute(select(Campaign).order_by(Campaign.id)).scalars():
            where = f"кампания #{camp.id} «{camp.name}»"
            for url, alt in find_images(camp.message):
                places[url].append(where)
                alts[url].add(alt)
            for href in find_dead_unsubscribe(camp.message):
                dead_unsub[href].append(where)

    if dead_unsub:
        print("Ссылки «Отписаться» от старой системы — ведут в никуда:\n")
        for href, where_list in dead_unsub.items():
            ok, note = check_url(href.replace("{{email}}", "test@example.com"))
            print(f"  {href}")
            print(f"        {'отвечает' if ok else note}")
            print(f"        где: {', '.join(sorted(set(where_list))[:4])}")
        print("  Получатель жмёт «Отписаться», ничего не происходит — и жмёт «Спам».")
        print("  Это бьёт по репутации домена сильнее любой вёрстки.")
        print("  Почините в редакторе: вместо адреса впишите {{unsubscribe_url}}.")
        print()

    if not places:
        print("Картинок в письмах нет.")
        return 0

    print(f"Картинок всего: {len(places)}. Проверяю так, как за ними ходит Gmail…\n")

    broken: list[str] = []
    for url in sorted(places):
        ok, note = check_url(url)
        mark = "OK " if ok else "НЕТ"
        shown = url if len(url) <= 96 else url[:93] + "..."
        print(f"[{mark}] {shown}")
        print(f"        {note}")
        where = sorted(set(places[url]))
        print(f"        где: {', '.join(where[:4])}{' и ещё ' + str(len(where) - 4) if len(where) > 4 else ''}")
        if not ok:
            broken.append(url)
            print("        -> получатель увидит на этом месте подпись, а не картинку")

        weak = [a for a in alts[url] if not a or a.lower() in PLACEHOLDER_ALTS]
        if weak:
            empty = any(not a for a in weak)
            text = "подписи нет" if empty else f"подпись «{sorted(a for a in weak if a)[0]}»"
            print(f"        подпись: {text} — её читают все, у кого картинки отключены")
            print("        -> Outlook отключает картинки по умолчанию; напишите, что на картинке")
        print()

    print(f"Итого: не дойдёт до получателя {len(broken)} из {len(places)}.")
    if not broken:
        print("Все картинки доступны снаружи. Если получатель их всё равно не видит —")
        print("проверьте, не попало ли письмо в спам: там почта не загружает картинки сама.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
