class XiaoNuoError(Exception):
    """Only a stable, non-sensitive error code may cross the API boundary."""

    def __init__(self, code: str, status: int = 503):
        self.code = code
        self.status = status
        super().__init__(code)


class ProviderError(XiaoNuoError):
    pass
