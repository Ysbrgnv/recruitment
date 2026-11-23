class UserFacingError(RuntimeError):
    """Safe error whose message may be shown in the web interface."""


class TrackerBusyError(UserFacingError):
    pass


class TrackerUnavailableError(UserFacingError):
    pass


class HHUnavailableError(UserFacingError):
    _ERROR_KINDS = {
        "timeout", "network_error", "rate_limited", "server_error",
        "auth_failed", "auth_revoked", "forbidden", "not_found",
        "client_error", "auth_disabled", "malformed_response",
    }
    _REQUEST_SCOPES = {"search", "details", "vacancy", "auth"}
    _ENDPOINTS = {
        "/resumes", "/resumes/{id}", "/vacancies",
        "/vacancies/{id}", "/oauth/token",
    }

    def __init__(
        self,
        *args,
        status_code=None,
        error_kind=None,
        request_scope=None,
        endpoint=None,
        retry_after=None,
    ):
        super().__init__(*args)
        self.error_kind = error_kind if error_kind in self._ERROR_KINDS else None
        self.request_scope = request_scope if request_scope in self._REQUEST_SCOPES else None
        self.endpoint = endpoint if endpoint in self._ENDPOINTS else None
        self.status_code = (
            status_code
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else None
        )
        self.retry_after = (
            retry_after
            if isinstance(retry_after, int)
            and not isinstance(retry_after, bool)
            and retry_after >= 0
            else None
        )

    def __reduce__(self):
        return (type(self), self.args, self.__dict__)


class AIUnavailableError(UserFacingError):
    def __init__(self, message: str, request_sent: bool = False):
        super().__init__(message)
        self.request_sent = request_sent


class MailUnavailableError(UserFacingError):
    pass


class DuplicateTaskError(UserFacingError):
    pass

