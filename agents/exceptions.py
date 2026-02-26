class MagnusTradeError(Exception):
    def __init__(self, message, code=5000):
        self.message = message
        self.code = code
        super().__init__(self.message)

class SignatureError(MagnusTradeError):
    """Polymarket rejected signature (usually clock desync)."""
    def __init__(self, message="Invalid Signature"):
        super().__init__(message, code=4001)

class InsufficientFunds(MagnusTradeError):
    def __init__(self, message="Insufficient proxy balance"):
        super().__init__(message, code=4002)

class SlippageError(MagnusTradeError):
    """Price moved too fast before order could fill."""
    def __init__(self, message="Slippage: price moved too fast"):
        super().__init__(message, code=4003)