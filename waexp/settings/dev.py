from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent.parent
_env_path = BASE_DIR / "env" / ".env.dev"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)

from .base import *  # noqa: E402,F401,F403


DEBUG = env_bool("DJANGO_DEBUG", True)

