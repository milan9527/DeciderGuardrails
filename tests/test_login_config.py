import json
import stat

from demo_api.auth import verify_password
from scripts.login_config import load_login, runtime_config


def test_local_login_persists_and_runtime_config_contains_only_salted_hash(tmp_path):
    path = tmp_path / "demo-login.json"
    login = load_login(path)
    assert login == load_login(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = runtime_config(login, "previous-bearer-token")
    assert login["password"] not in config
    assert json.loads(config)["username"] == "admin"
    assert verify_password(login["password"], json.loads(config)["password_hash"])
    assert runtime_config(login, config) == config  # Redeployment does not rotate credentials.
    changed = runtime_config({**login, "password": "a-new-password-for-the-demo"}, config)
    assert changed != config
    assert not verify_password(login["password"], json.loads(changed)["password_hash"])
