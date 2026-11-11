"""Environment-first runtime configuration; file optional, never overrides injected values."""
import os
from pathlib import Path
from dotenv import load_dotenv

def load_runtime():
    default = Path(__file__).resolve().parents[2] / '.env.stage2'
    path=os.getenv('APP_RUNTIME_ENV_FILE',str(default))
    if path:load_dotenv(path,override=False)

