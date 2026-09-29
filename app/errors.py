"""业务错误：携带 HTTP 状态码，交由接口层统一渲染。"""


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NotFoundError(ServiceError):
    def __init__(self, message: str = "资源不存在"):
        super().__init__(message, 404, "not_found")


class ConflictError(ServiceError):
    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message, 409, code)


class PermissionError(ServiceError):  # noqa: A001 - 与内置同名但语义清晰
    def __init__(self, message: str = "无权限执行该操作"):
        super().__init__(message, 403, "forbidden")


class ValidationError(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, 422, "validation_error")
