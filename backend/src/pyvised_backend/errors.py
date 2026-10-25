"""Errors that the API turns into an `ErrorInfo` answer."""


class ServiceError(Exception):
    """A failure to report to the client: HTTP status, short code and detail text."""

    status = 422

    def __init__(self, error: str, detail: str = "", *, status: int | None = None) -> None:
        super().__init__(f"{error}: {detail}" if detail else error)
        self.error = error
        self.detail = detail
        if status is not None:
            self.status = status
