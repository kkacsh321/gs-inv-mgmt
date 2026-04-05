import hashlib
import hmac
import os


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    raw = (password or "").strip()
    if len(raw) < 8:
        raise ValueError("Password must be at least 8 characters.")
    resolved_salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        raw.encode("utf-8"),
        bytes.fromhex(resolved_salt),
        200_000,
    )
    return digest.hex(), resolved_salt


def verify_password(password: str, expected_hash: str, salt: str) -> bool:
    if not expected_hash or not salt:
        return False
    try:
        candidate_hash, _ = hash_password(password, salt=salt)
    except Exception:
        return False
    return hmac.compare_digest(candidate_hash, expected_hash)
