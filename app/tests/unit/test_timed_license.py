"""Timed-лицензии: ключ действует N часов с момента ВВОДА, не с даты.

Подписываем эфемерной парой ключей и подменяем публичный ключ приложения и
host_id — так тестируется весь путь install → verify без боевого приватного
ключа.

Проверку зовём через ``_verify_uncached``, а не через публичную
``verify_license``: та кеширует результат и в безлицензионной сборке может быть
замкнута на «годен». Разбор ключей от этого не зависит и проверяется напрямую —
тесты одинаково верны и когда проверка включена, и когда выключена.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cbmail.core import licensing

_HOST = "deadbeef" * 8


@pytest.fixture
def signer(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_HEX", pub_hex)
    monkeypatch.setattr(licensing, "compute_host_id", lambda: _HOST)
    monkeypatch.setenv("LICENSE_FILE", str(tmp_path / "license.key"))
    licensing._cache.update(ok=None, reason="", ts=0.0)

    def make(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = base64.b64encode(priv.sign(canonical)).decode()
        return json.dumps({"payload": payload, "sig": sig})

    return make


def _timed_payload(hours: int = 6, nonce: str = "n1") -> dict:
    return {
        "host_id": _HOST,
        "mode": "timed",
        "duration_hours": hours,
        "nonce": nonce,
        "customer": "test",
        "issued": "2026-08-01",
    }


def test_timed_key_activates_and_is_valid(signer, monkeypatch):
    ok, reason = licensing.install_license(signer(_timed_payload()))
    assert ok, reason
    valid, msg = licensing._verify_uncached()
    assert valid
    assert "активна" in msg


def test_timed_key_expires_after_duration(signer, tmp_path):
    licensing.install_license(signer(_timed_payload(hours=6)))
    # Подкручиваем момент активации на 7 часов назад — срок вышел.
    path = tmp_path / "license.key"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["activated_at"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    path.write_text(json.dumps(doc), encoding="utf-8")
    valid, msg = licensing._verify_uncached()
    assert not valid
    assert "expired" in msg


def test_timed_key_survives_reactivation_resets_clock(signer, tmp_path):
    licensing.install_license(signer(_timed_payload(nonce="a")))
    path = tmp_path / "license.key"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["activated_at"] = (datetime.now(timezone.utc) - timedelta(hours=5, minutes=59)).isoformat()
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert licensing._verify_uncached()[0]
    # Ввод другого ключа сбрасывает отсчёт заново.
    licensing.install_license(signer(_timed_payload(nonce="b")))
    valid, msg = licensing._verify_uncached()
    assert valid
    assert "активна" in msg


def test_wrong_host_rejected(signer, monkeypatch):
    payload = _timed_payload()
    payload["host_id"] = "0" * 64
    ok, reason = licensing.install_license(signer(payload))
    assert not ok
    assert "другого сервера" in reason


def test_perpetual_absolute_key_still_valid(signer):
    payload = {"host_id": _HOST, "customer": "c", "issued": "2026-08-01", "expires": None}
    ok, _ = licensing.install_license(signer(payload))
    assert ok
    assert licensing._verify_uncached()[0]
