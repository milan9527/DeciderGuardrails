"""Start the local API with username/password credentials in a private local file."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    import uvicorn
    from scripts.login_config import load_login, runtime_config

    path = ROOT / ".deployment/local-login.json"
    os.environ["DEMO_AUTH_CONFIG"] = runtime_config(load_login(path))
    os.environ["COOKIE_SECURE"] = "false"  # Loopback HTTP only; production defaults to Secure.
    os.environ.setdefault("DECIDER_KEY_FILE", str(ROOT / "DeciderKey.txt"))
    print(f"Local username/password: {path} (values are not printed)")
    uvicorn.run("demo_api.app:app", host="127.0.0.1", port=8000, proxy_headers=False)
