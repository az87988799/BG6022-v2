"""BG6022 V2 package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("orca-agent")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = ["__version__"]
