"""Explicit, non-secret runtime configuration for the P7 chat surface.

The interactive launcher is deliberately the only place that selects a P7
profile.  Application services receive this immutable value object so a
historical task cannot silently inherit another process' defaults.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orca_agent.domain.hashing import sha256_hex

P7_PROFILES = ("real", "deepseek_fake", "offline")
LEGACY_PROFILE = "legacy"
DEFAULT_ORCA_VERSION = "6.1.1"
DEFAULT_MODEL = "deepseek-v4-flash"

_ENV_KEYS = frozenset(
    {
        "BG6022_P7_PROFILE",
        "BG6022_P7_PLANNER",
        "BG6022_P7_MODEL",
        "BG6022_P7_MODEL_CALL_BUDGET",
        "BG6022_ORCA_EXECUTABLE",
        "BG6022_ORCA_VERSION",
        "DEEPSEEK_API_KEY",
    }
)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read the small project ``.env`` format without importing a dotenv package."""

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (FileNotFoundError, OSError):
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in _ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_project_environment(
    project_root: str | Path, *, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Load non-secret configuration from ``.env`` then process variables.

    The returned mapping may contain the key because the adapter needs it, but
    callers must never include it in diagnostics or persisted configuration.
    Process variables always win over the project file.
    """

    root = Path(project_root).resolve()
    values = _parse_env_file(root / ".env")
    process = os.environ if environ is None else environ
    for key in _ENV_KEYS:
        value = process.get(key)
        if value is not None:
            values[key] = value
    return values


def _optional_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text else Path(text).expanduser().resolve()


@dataclass(frozen=True)
class P7RuntimeConfig:
    """Immutable, validated configuration shared by all P7 services."""

    profile: str
    planner_name: str
    allow_llm: bool
    allow_network: bool
    identity_provider: str
    backend_kind: str
    allow_real_orca: bool
    state_root: Path
    orca_executable: Path | None = None
    orca_version: str | None = None
    model: str = DEFAULT_MODEL
    model_call_budget: int = 120
    expected_orca_version: str = DEFAULT_ORCA_VERSION

    def __post_init__(self) -> None:
        if self.profile not in (*P7_PROFILES, LEGACY_PROFILE):
            raise ValueError(f"unsupported P7 profile: {self.profile}")
        if self.planner_name not in {"baseline", "fake", "deepseek_chat"}:
            raise ValueError("planner_name must be baseline, fake, or deepseek_chat")
        if self.identity_provider not in {"fake", "pubchem", "local"}:
            raise ValueError("identity_provider must be fake, pubchem, or local")
        if self.backend_kind not in {"fake", "local_orca"}:
            raise ValueError("backend_kind must be fake or local_orca")
        if type(self.allow_llm) is not bool or type(self.allow_network) is not bool:
            raise ValueError("runtime feature flags must be booleans")
        if type(self.allow_real_orca) is not bool:
            raise ValueError("allow_real_orca must be a boolean")
        if type(self.model_call_budget) is not int or not 0 <= self.model_call_budget <= 10000:
            raise ValueError("model_call_budget must be between 0 and 10000")
        if not str(self.model).strip():
            raise ValueError("model must not be blank")
        if self.profile == "real":
            expected = {
                "planner_name": "deepseek_chat",
                "allow_llm": True,
                "allow_network": True,
                "identity_provider": "pubchem",
                "backend_kind": "local_orca",
                "allow_real_orca": True,
            }
            for field, required in expected.items():
                if getattr(self, field) != required:
                    raise ValueError(f"real profile requires {field}={required!r}")
        elif self.profile == "deepseek_fake":
            expected = {
                "planner_name": "deepseek_chat",
                "allow_llm": True,
                "allow_network": False,
                "identity_provider": "fake",
                "backend_kind": "fake",
                "allow_real_orca": False,
            }
            for field, required in expected.items():
                if getattr(self, field) != required:
                    raise ValueError(f"deepseek_fake profile requires {field}={required!r}")
        elif self.profile == "offline":
            expected = {
                "planner_name": "baseline",
                "allow_llm": False,
                "allow_network": False,
                "identity_provider": "fake",
                "backend_kind": "fake",
                "allow_real_orca": False,
            }
            for field, required in expected.items():
                if getattr(self, field) != required:
                    raise ValueError(f"offline profile requires {field}={required!r}")

        if self.profile == "real" and self.orca_version is not None:
            if self.orca_version != self.expected_orca_version:
                raise ValueError("real profile ORCA version must match the supported exact version")

        object.__setattr__(self, "state_root", Path(self.state_root).resolve())
        object.__setattr__(self, "orca_executable", _optional_path(self.orca_executable))
        if self.orca_version is not None:
            object.__setattr__(self, "orca_version", str(self.orca_version).strip())

    @classmethod
    def for_profile(
        cls,
        profile: str,
        state_root: str | Path,
        *,
        project_root: str | Path | None = None,
        model: str | None = None,
        model_call_budget: int | None = None,
        orca_executable: str | Path | None = None,
        orca_version: str | None = None,
    ) -> P7RuntimeConfig:
        """Construct one of the three supported user-facing profiles."""

        if profile not in P7_PROFILES:
            raise ValueError(f"profile must be one of {', '.join(P7_PROFILES)}")
        root = Path(project_root or Path.cwd()).resolve()
        env = load_project_environment(root)
        env_model = env.get("BG6022_P7_MODEL") or DEFAULT_MODEL
        env_budget = env.get("BG6022_P7_MODEL_CALL_BUDGET")
        budget = model_call_budget
        if budget is None:
            try:
                budget = int(env_budget) if env_budget else 120
            except ValueError as error:
                raise ValueError("BG6022_P7_MODEL_CALL_BUDGET must be an integer") from error
        defaults = {
            "real": {
                "planner_name": "deepseek_chat",
                "allow_llm": True,
                "allow_network": True,
                "identity_provider": "pubchem",
                "backend_kind": "local_orca",
                "allow_real_orca": True,
            },
            "deepseek_fake": {
                "planner_name": "deepseek_chat",
                "allow_llm": True,
                "allow_network": False,
                "identity_provider": "fake",
                "backend_kind": "fake",
                "allow_real_orca": False,
            },
            "offline": {
                "planner_name": "baseline",
                "allow_llm": False,
                "allow_network": False,
                "identity_provider": "fake",
                "backend_kind": "fake",
                "allow_real_orca": False,
            },
        }[profile]
        configured_planner = (env.get("BG6022_P7_PLANNER") or "").strip()
        expected_planner = str(defaults["planner_name"])
        if configured_planner and configured_planner != expected_planner:
            raise ValueError(
                "BG6022_P7_PLANNER conflicts with the selected P7 profile "
                f"({configured_planner!r} != {expected_planner!r})"
            )
        return cls(
            profile=profile,
            state_root=Path(state_root),
            model=model or env_model,
            model_call_budget=budget,
            # Only the real profile may carry an ORCA executable into P5.
            # Keeping fake/offline profiles free of a stale machine path is
            # important when the same project .env is used for both modes.
            orca_executable=(
                orca_executable or env.get("BG6022_ORCA_EXECUTABLE") if profile == "real" else None
            ),
            orca_version=(
                orca_version or env.get("BG6022_ORCA_VERSION") if profile == "real" else None
            ),
            **defaults,
        )

    @classmethod
    def legacy(
        cls,
        state_root: str | Path,
        *,
        planner_name: str,
        allow_llm: bool,
        allow_network: bool,
        backend_kind: str,
        allow_real_orca: bool,
        model_call_budget: int = 120,
    ) -> P7RuntimeConfig:
        """Compatibility constructor for library callers from before profiles."""

        return cls(
            profile=LEGACY_PROFILE,
            planner_name=planner_name,
            allow_llm=allow_llm,
            allow_network=allow_network,
            identity_provider="local" if backend_kind == "local_orca" else "fake",
            backend_kind=backend_kind,
            allow_real_orca=allow_real_orca,
            state_root=Path(state_root),
            model_call_budget=model_call_budget,
        )

    @property
    def execution_ready(self) -> bool:
        if self.profile != "real":
            return True
        return bool(
            self.orca_executable
            and self.orca_executable.is_file()
            and not self.orca_executable.is_symlink()
            and self.orca_version == self.expected_orca_version
        )

    @property
    def execution_readiness_reasons(self) -> tuple[str, ...]:
        if self.profile != "real":
            return ()
        reasons: list[str] = []
        if self.orca_executable is None:
            reasons.append("BG6022_ORCA_EXECUTABLE 未配置")
        elif not self.orca_executable.is_file():
            reasons.append("ORCA 可执行文件不存在或不是普通文件")
        elif self.orca_executable.is_symlink():
            reasons.append("ORCA 可执行文件不能是符号链接")
        if self.orca_version != self.expected_orca_version:
            reasons.append(f"ORCA 版本必须是 {self.expected_orca_version}")
        return tuple(reasons)

    @property
    def profile_hash(self) -> str:
        return sha256_hex(self.public_dict())

    def public_dict(self) -> dict[str, object]:
        """Return the safe-to-persist/display subset; never includes API keys."""

        return {
            "profile": self.profile,
            "planner": self.planner_name,
            "model": self.model,
            "allow_llm": self.allow_llm,
            "allow_network": self.allow_network,
            "identity_provider": self.identity_provider,
            "backend": self.backend_kind,
            "allow_real_orca": self.allow_real_orca,
            "state_root": str(self.state_root),
            "orca_executable": None if self.orca_executable is None else str(self.orca_executable),
            "orca_version": self.orca_version,
            "expected_orca_version": self.expected_orca_version,
            "model_call_budget": self.model_call_budget,
            "execution_ready": self.execution_ready,
        }


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_ORCA_VERSION",
    "LEGACY_PROFILE",
    "P7_PROFILES",
    "P7RuntimeConfig",
    "load_project_environment",
]
