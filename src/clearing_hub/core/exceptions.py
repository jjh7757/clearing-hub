"""SPEC 5절의 오류 형식: HTTP 상태 + {"error": 코드, "message": 설명, "transfer_id"?}"""


class ApiException(Exception):
    def __init__(self, status_code: int, code: str, message: str, transfer_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.transfer_id = transfer_id


class Unauthorized(ApiException):
    def __init__(self, code: str = "UNAUTHORIZED", message: str = "인증에 실패했습니다"):
        super().__init__(401, code, message)


class Forbidden(ApiException):
    def __init__(self, message: str = "다른 은행의 거래입니다"):
        super().__init__(403, "FORBIDDEN", message)


class NotFound(ApiException):
    def __init__(self, code: str, message: str, transfer_id: str | None = None):
        super().__init__(404, code, message, transfer_id)


class BadRequest(ApiException):
    def __init__(self, code: str, message: str, transfer_id: str | None = None):
        super().__init__(400, code, message, transfer_id)
