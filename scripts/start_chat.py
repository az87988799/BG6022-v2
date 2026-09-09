"""Fixed-root interactive P7 launcher used by ``start_chat.cmd``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO


class SingleInstanceLock(AbstractContextManager["SingleInstanceLock"]):
    """Hold an OS-level lock for the lifetime of the chat process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: TextIO | None = None

    def __enter__(self) -> SingleInstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="ascii")
        self.handle.seek(0)
        self.handle.write("0")
        self.handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError) as error:
            self.handle.close()
            self.handle = None
            raise RuntimeError("已有一个 P7 对话窗口正在运行；请先关闭它") from error
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the fixed-root P7 chat")
    parser.add_argument("--profile", choices=("real", "deepseek_fake", "offline"))
    parser.add_argument("--planner", choices=("deepseek_chat", "baseline"))
    parser.add_argument("--model-call-budget", type=int)
    parser.add_argument("--orca-executable", type=Path)
    parser.add_argument("--orca-version")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--new", "--new-conversation", action="store_true")
    parser.add_argument("--conversation")
    parser.add_argument("--json", action="store_true")
    return parser


def _pointer_path(state_root: Path) -> Path:
    return state_root / "last_session.json"


def _load_pointer(path: Path, state_root: Path, profile: str) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RuntimeError("上次会话指针损坏，未自动创建新会话") from error
    if not isinstance(value, dict):
        raise RuntimeError("上次会话指针格式无效，未自动创建新会话")
    if Path(str(value.get("state_root", ""))).resolve() != state_root:
        raise RuntimeError("上次会话指针的 state root 不匹配，未自动创建新会话")
    if value.get("profile") != profile:
        raise RuntimeError("上次会话指针的 profile 不匹配，未自动创建新会话")
    conversation_id = value.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise RuntimeError("上次会话指针缺少 conversation_id，未自动创建新会话")
    return conversation_id


def _save_pointer(path: Path, state_root: Path, profile: str, conversation_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "conversation_id": conversation_id,
        "profile": profile,
        "state_root": str(state_root),
        "updated_at_utc": datetime.now(UTC).isoformat(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _configured_deepseek() -> tuple[bool, str]:
    from orca_agent.llm.deepseek_chat import DeepSeekChatAdapter

    adapter = DeepSeekChatAdapter()
    if not adapter.api_key or _is_placeholder(adapter.api_key):
        return False, "DEEPSEEK_API_KEY 未配置"
    if not adapter.model or _is_placeholder(adapter.model):
        return False, "BG6022_P7_MODEL 未配置"
    return True, ""


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().casefold()
    return (
        lowered
        in {
            "your_deepseek_api_key_here",
            "填写自己的密钥",
            "your_model_here",
        }
        or lowered.startswith("your_")
        or "填写" in lowered
    )


def main() -> int:
    args = _build_parser().parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    os.chdir(repository_root)
    from orca_agent.application.p7_runtime_config import load_project_environment

    project_environment = load_project_environment(repository_root)
    profile = args.profile
    if profile is None:
        profile = project_environment.get("BG6022_P7_PROFILE")
    if args.planner is not None and args.profile is None:
        profile = "offline" if args.planner == "baseline" else "deepseek_fake"
    if profile is None:
        profile = "real"
    if args.planner is not None:
        expected_planner = "baseline" if profile == "offline" else "deepseek_chat"
        if args.planner != expected_planner:
            print(
                f"P7 profile={profile} 与 --planner={args.planner} 组合不合法。",
                file=sys.stderr,
            )
            return 2
    state_root = (repository_root / ".tmp" / "p7" / f"{profile}-chat").resolve()
    lock_path = state_root / "chat.lock"
    pointer_path = _pointer_path(state_root)

    if profile in {"real", "deepseek_fake"} and not args.doctor:
        try:
            configured, reason = _configured_deepseek()
        except Exception as error:
            print(f"P7 DeepSeek 配置检查失败：{type(error).__name__}", file=sys.stderr)
            return 2
        if not configured:
            print(
                f"P7 无法启动：{reason}。请复制 .env.example 为 .env 并填写真实值。",
                file=sys.stderr,
            )
            return 2

    if args.doctor:
        try:
            from orca_agent.application.p7_runtime_config import P7RuntimeConfig
            from orca_agent.execution.orca_config import doctor

            config = P7RuntimeConfig.for_profile(
                profile,
                state_root,
                project_root=repository_root,
                model_call_budget=args.model_call_budget,
                orca_executable=args.orca_executable,
                orca_version=args.orca_version,
            )
            result = doctor(
                state_root,
                executable=config.orca_executable,
                probe=True,
                expected_version=config.expected_orca_version,
            )
            result["profile"] = config.public_dict()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ready") else 2
        except Exception as error:
            print(f"P7 doctor 失败：{type(error).__name__}", file=sys.stderr)
            return 2

    try:
        with SingleInstanceLock(lock_path):
            return _run_chat(args, repository_root, state_root, pointer_path, profile)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2


def _run_chat(
    args, repository_root: Path, state_root: Path, pointer_path: Path, profile: str
) -> int:
    from orca_agent.application.p7_runtime_config import P7RuntimeConfig
    from orca_agent.bootstrap.p7_modules import build_p7_runtime
    from orca_agent.interfaces.p7_chat import P7ChatDriver

    config = P7RuntimeConfig.for_profile(
        profile,
        state_root,
        project_root=repository_root,
        model_call_budget=args.model_call_budget,
        orca_executable=args.orca_executable,
        orca_version=args.orca_version,
    )
    runtime = build_p7_runtime(
        state_root,
        fallback="none",
        runtime_config=config,
    )
    selected = args.conversation
    if selected is None and not args.new:
        selected = _load_pointer(pointer_path, state_root, profile)
    if selected is not None:
        try:
            selected = str(runtime.conversation.get_state(selected).conversation_id)
        except Exception as error:
            raise RuntimeError("指定的 conversation 不存在或状态校验失败") from error
    if selected is None:
        selected = str(runtime.conversation.new_conversation()["conversation_id"])
    _save_pointer(pointer_path, state_root, profile, selected)

    def remember(conversation_id: str) -> None:
        _save_pointer(pointer_path, state_root, profile, conversation_id)

    driver = P7ChatDriver(
        runtime,
        selected,
        auto_work=True,
        max_effects=8,
        max_seconds=1.5,
        json_output=args.json,
        on_conversation_changed=remember,
    )
    try:
        return driver.run()
    except KeyboardInterrupt:
        print("\nP7 对话已中断；已运行的下游任务状态保持不变。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
