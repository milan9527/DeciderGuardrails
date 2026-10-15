"""Keep cleartext demo credentials local; send only a salted hash to ECS."""
import json
import os
import secrets
from pathlib import Path

from demo_api.auth import hash_password, validate_credentials, validate_username, verify_password


def load_login(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        login = {"username": "admin", "password": secrets.token_urlsafe(24)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(login, indent=2) + "\n")
    path.chmod(0o600)
    login = json.loads(path.read_text())
    if (not isinstance(login, dict) or set(login) != {"username", "password"}
            or not isinstance(login["username"], str)
            or not isinstance(login["password"], str)
            or not 12 <= len(login["password"]) <= 256):
        raise ValueError("Login file must contain username and a 12–256 character password")
    validate_username(login["username"])
    return login


def runtime_config(login: dict, previous: str = "") -> str:
    try:
        old = json.loads(previous)
        validate_credentials(old["username"], old["password_hash"])
        if old["username"] == login["username"] and verify_password(login["password"], old["password_hash"]):
            return previous
    except (ValueError, TypeError, KeyError):
        pass
    return json.dumps({"username": login["username"], "password_hash": hash_password(login["password"])})
