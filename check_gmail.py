"""Authenticate and NOOP only. Never sends email."""
from runtime_config import load_runtime
load_runtime()
import settings
from gmail_transport import check_connection
from errors import MailUnavailableError
if __name__=='__main__':
    try:check_connection()
    except MailUnavailableError as exc:raise SystemExit(str(exc)) from None
    print('GMAIL_AUTH_OK (no email sent)')

