"""Authenticate and NOOP only. Never sends email."""
from app.core.runtime_config import load_runtime
load_runtime()
from app.core import settings
from app.integrations.gmail.gmail_transport import check_connection
from app.core.errors import MailUnavailableError
if __name__=='__main__':
    try:check_connection()
    except MailUnavailableError as exc:raise SystemExit(str(exc)) from None
    print('GMAIL_AUTH_OK (no email sent)')

