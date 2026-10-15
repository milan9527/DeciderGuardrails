"""Password hashes and bounded, revocable sessions for the single-task demo."""
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "DeciderGuardrails"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Demo passwords must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${value.hex()}"


def validate_username(username: str):
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", username):
        raise ValueError("Invalid demo username")


def validate_credentials(username: str, password_hash: str):
    validate_username(username)
    if not re.fullmatch(r"scrypt\$[0-9a-f]{32}\$[0-9a-f]{64}", password_hash):
        raise ValueError("DEMO_AUTH_CONFIG must contain a valid scrypt password hash")


def verify_password(password: str, password_hash: str) -> bool:
    _, salt, expected = password_hash.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                            n=16384, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual, bytes.fromhex(expected))


@dataclass
class Session:
    username: str
    expires_at: float


class Sessions:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self.entries: dict[str, Session] = {}

    @staticmethod
    def key(token: str):
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, username: str) -> str:
        now = time.monotonic()
        self.entries = {key: session for key, session in self.entries.items()
                        if session.expires_at > now}
        if len(self.entries) >= 100:
            del self.entries[next(iter(self.entries))]
        token = secrets.token_urlsafe(32)
        self.entries[self.key(token)] = Session(username, now + self.ttl)
        return token

    def get(self, token: str) -> Session | None:
        session = self.entries.get(self.key(token))
        if session and session.expires_at <= time.monotonic():
            self.revoke(token)
            return None
        return session

    def revoke(self, token: str):
        self.entries.pop(self.key(token), None)
