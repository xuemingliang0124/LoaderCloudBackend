"""业务异常：服务层抛出，全局 handler 统一转响应。"""


class BusinessError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
