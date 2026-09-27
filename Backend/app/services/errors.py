"""Domain error types with stable machine-readable codes.

The API exception handler converts these into the consistent JSON shape:
{"success": false, "error": {"code": "...", "message": "..."}}
"""


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class UnauthorizedError(APIError):
    def __init__(self, message: str = "Authentication required."):
        super().__init__(401, "UNAUTHORIZED", message)


class ForbiddenError(APIError):
    def __init__(self, message: str = "You do not have permission for this action.",
                 code: str = "FORBIDDEN"):
        super().__init__(403, code, message)


class NotFoundError(APIError):
    def __init__(self, message: str = "Resource not found.", code: str = "NOT_FOUND"):
        super().__init__(404, code, message)


class ConflictError(APIError):
    def __init__(self, message: str, code: str = "CONFLICT"):
        super().__init__(409, code, message)


class BadRequestError(APIError):
    def __init__(self, message: str, code: str = "BAD_REQUEST"):
        super().__init__(400, code, message)


class ServiceUnavailableError(APIError):
    def __init__(self, message: str = "Service temporarily unavailable."):
        super().__init__(503, "SERVICE_UNAVAILABLE", message)
