try:
    from udockerd._version import __version__
except ImportError:
    __version__ = "0+unknown"

__all__ = ["__version__"]
