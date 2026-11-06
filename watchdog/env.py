"""Environment bootstrap (idempotent): loads `.env` without overriding real environment variables."""
import os

from dotenv import dotenv_values, load_dotenv

from watchdog.paths import PROJECT_ROOT

_loaded = False


def load_env() -> None:
    """Real env vars (Docker, shell, CLI overrides) win over `.env`. Legacy `USER=`/`PASS=` account keys
    are mapped to MOLLY_USER / MOLLY_PASS so the account can't silently become the OS login name."""
    global _loaded
    if _loaded:
        return
    env_file = PROJECT_ROOT / ".env"
    load_dotenv(env_file, override=False)
    file_vals = dotenv_values(env_file) if env_file.exists() else {}
    for legacy, key in (("USER", "MOLLY_USER"), ("PASS", "MOLLY_PASS")):
        if file_vals.get(legacy) and not os.getenv(key):
            os.environ[key] = str(file_vals[legacy])
    _loaded = True
