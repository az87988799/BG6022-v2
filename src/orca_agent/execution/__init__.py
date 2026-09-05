"""P3 execution boundary: validation, gateway, and offline fake backend."""

from .fake_backend import FakeBackend, FakeBackendResult
from .gateway import FakeExecutionGateway
from .validator import P3ValidationResult, require_valid_water_action

__all__ = [
    "FakeBackend",
    "FakeBackendResult",
    "FakeExecutionGateway",
    "P3ValidationResult",
    "require_valid_water_action",
]
