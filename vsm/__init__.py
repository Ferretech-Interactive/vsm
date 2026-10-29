__version__ = "0.1.0b2"

# Verify HTTPS certificates the way the operating system does. Python otherwise only reads the
# root certificates already present in the Windows store, and a freshly installed Windows has
# not fetched most of them yet, so downloads (UE4SS, update checks) would fail there.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # not installed, or an unsupported platform: fall back to Python's default
    pass


class VsmError(Exception):
    """An expected failure that should be shown to the user as-is."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
