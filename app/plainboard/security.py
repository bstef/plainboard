"""Secret encryption at rest and the optional single-password login."""

from __future__ import annotations

import hmac
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "pb_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30

_fernet: Fernet | None = None
_signer: URLSafeTimedSerializer | None = None
_key: str | None = None


def init_security(data_dir: Path) -> None:
    global _fernet, _signer, _key
    key_file = data_dir / "secret.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
    key = key_file.read_bytes().strip()
    _fernet = Fernet(key)
    _key = key.decode()
    _signer = URLSafeTimedSerializer(key.decode(), salt="plainboard-session")


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    assert _fernet is not None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    assert _fernet is not None
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def password_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


def make_session_cookie() -> str:
    assert _signer is not None
    return _signer.dumps({"ok": True})


def session_cookie_valid(value: str | None) -> bool:
    if not value or _signer is None:
        return False
    try:
        return bool(_signer.loads(value, max_age=SESSION_MAX_AGE).get("ok"))
    except BadSignature:
        return False


def sign(data: dict, purpose: str) -> str:
    """A signed value for a cookie that is not the login."""
    assert _key is not None
    return URLSafeTimedSerializer(_key, salt=f"plainboard-{purpose}").dumps(data)


def unsign(value: str | None, purpose: str, max_age: int) -> dict | None:
    if not value or _key is None:
        return None
    try:
        return URLSafeTimedSerializer(_key, salt=f"plainboard-{purpose}").loads(value, max_age=max_age)
    except BadSignature:
        return None
