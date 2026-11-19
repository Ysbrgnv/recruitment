from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = Path(os.getenv("APP_ENV_FILE", str(BASE_DIR / ".env"))).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(ENV_PATH)

def env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value

def env_float(name: str, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value

HH_API_BASE = "https://api.hh.kz"
HH_AUTHORIZE_URL = "https://hh.kz/oauth/authorize"
HH_TOKEN_URL = "https://hh.kz/oauth/token"
HH_CLIENT_ID = os.getenv("HH_CLIENT_ID", "").strip()
HH_CLIENT_SECRET = os.getenv("HH_CLIENT_SECRET", "").strip()
HH_REDIRECT_URI = os.getenv("HH_REDIRECT_URI", "").strip()
EMPLOYER_ID = os.getenv("EMPLOYER_ID", "").strip()
HH_USER_AGENT = os.getenv("HH_USER_AGENT", "Recruitment-Assistant/1.0").strip()

NANO_GPT_API_KEY = os.getenv("NANO_GPT_API_KEY", "").strip()
API_NANO_URL = os.getenv("API_NANO_URL", "https://nano-gpt.com/api/v1/chat/completions").strip()
MODEL_NANO = os.getenv("MODEL_NANO", "alibaba/qwen3.8-flash").strip()

DEFAULT_EVALUATION_COUNT = env_int("RESUMES_TO_FIND", 2, 1, 10)
POOL_MULTIPLIER = env_int("RESUME_POOL_MULTIPLIER", 3, 1, 10)
POOL_EXTRA = env_int("RESUME_POOL_EXTRA", 2, 0, 20)
MAX_CANDIDATE_POOL = env_int("RESUME_MAX_CANDIDATE_POOL", 50, 1, 100)
SEARCH_PER_PAGE = env_int("RESUME_SEARCH_PER_PAGE", 30, 10, 100)
SEARCH_SCAN_LIMIT = env_int("RESUME_SEARCH_SCAN_LIMIT", 100, 10, 500)
SEARCH_MAX_PAGES = env_int("RESUME_SEARCH_MAX_PAGES", 4, 1, 10)
CANDIDATE_POOL_V2_ENABLED = (
    os.getenv("CANDIDATE_POOL_V2_ENABLED", "false")
    .strip()
    .casefold()
    in {"1", "true", "yes", "on"}
)
CANDIDATE_POOL_V3_ENABLED = (
    os.getenv("CANDIDATE_POOL_V3_ENABLED", "false").strip().casefold()
    in {"1", "true", "yes", "on"}
)
CANDIDATE_POOL_V3_EXPERIENCE_FILTER_ENABLED = (
    os.getenv("CANDIDATE_POOL_V3_EXPERIENCE_FILTER_ENABLED", "false").strip().casefold()
    in {"1", "true", "yes", "on"}
)
CANDIDATE_POOL_V3_MAX_PAGES = env_int("CANDIDATE_POOL_V3_MAX_PAGES", 10, 1)
CANDIDATE_POOL_V3_SCAN_LIMIT = env_int("CANDIDATE_POOL_V3_SCAN_LIMIT", 100, 1)
CANDIDATE_POOL_V3_EXPERIENCE_TOLERANCE_MONTHS = env_int("CANDIDATE_POOL_V3_EXPERIENCE_TOLERANCE_MONTHS", 2, 0)
CANDIDATE_POOL_V3_PAGE_SNAPSHOT_TTL_HOURS = env_int("CANDIDATE_POOL_V3_PAGE_SNAPSHOT_TTL_HOURS", 24, 1)
CANDIDATE_POOL_V3_RECOVERY_SUCCESS_THRESHOLD = env_int("CANDIDATE_POOL_V3_RECOVERY_SUCCESS_THRESHOLD", 10, 1)
CANDIDATE_POOL_V3_FAILURE_MIN_SAMPLE = env_int("CANDIDATE_POOL_V3_FAILURE_MIN_SAMPLE", 10, 1)
CANDIDATE_POOL_V3_FAILURE_WINDOW = env_int("CANDIDATE_POOL_V3_FAILURE_WINDOW", 20, 1)
CANDIDATE_POOL_V3_FAILURE_RATE_THRESHOLD = env_float("CANDIDATE_POOL_V3_FAILURE_RATE_THRESHOLD", 0.50, 0.0, 1.0)
CANDIDATE_POOL_V3_CONSECUTIVE_FAILURE_LIMIT = env_int("CANDIDATE_POOL_V3_CONSECUTIVE_FAILURE_LIMIT", 5, 1)
HH_MAX_RETRY_AFTER_SECONDS = env_int("HH_MAX_RETRY_AFTER_SECONDS", 30, 1)
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = env_int("APP_PORT", 8765, 1, 65535)
REQUEST_TIMEOUT = 30
NANO_TIMEOUT = env_int("NANO_TIMEOUT", 300, 30, 1800)

def validate_hh_settings() -> list[str]:
    values = {"EMPLOYER_ID": EMPLOYER_ID, "HH_CLIENT_ID": HH_CLIENT_ID, "HH_CLIENT_SECRET": HH_CLIENT_SECRET, "HH_REDIRECT_URI": HH_REDIRECT_URI}
    return [key for key, value in values.items() if not value]

def validate_nano_settings() -> list[str]:
    values = {"NANO_GPT_API_KEY": NANO_GPT_API_KEY, "API_NANO_URL": API_NANO_URL, "MODEL_NANO": MODEL_NANO}
    return [key for key, value in values.items() if not value]

MAX_BACKGROUND_WORKERS = env_int("MAX_BACKGROUND_WORKERS", 2, 1, 8)
TASK_RETENTION_HOURS = env_int("TASK_RETENTION_HOURS", 24, 1, 168)
RESERVATION_STALE_MINUTES = env_int("RESERVATION_STALE_MINUTES", 30, 5, 1440)

# Separate, bounded HH stage workers; X-Ray keeps MAX_BACKGROUND_WORKERS.
HH_BACKGROUND_WORKERS = env_int("HH_BACKGROUND_WORKERS", 4, 1, 16)

HH_AI_WORKERS = env_int("HH_AI_WORKERS", 4, 1, 16)
HH_MAIL_WORKERS = env_int("HH_MAIL_WORKERS", 1, 1, 4)
