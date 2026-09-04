"""Application-facing P2 command results and errors."""

from .errors import ApplicationError
from .results import ApplicationResult

__all__ = ["ApplicationError", "ApplicationResult"]
