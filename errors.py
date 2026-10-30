class UserFacingError(RuntimeError):
    """Safe error whose message may be shown in the web interface."""


class TrackerBusyError(UserFacingError):
    pass


class TrackerUnavailableError(UserFacingError):
    pass


class HHUnavailableError(UserFacingError):
    pass


class AIUnavailableError(UserFacingError):
    def __init__(self, message: str, request_sent: bool = False):
        super().__init__(message)
        self.request_sent = request_sent


class MailUnavailableError(UserFacingError):
    pass


class DuplicateTaskError(UserFacingError):
    pass

