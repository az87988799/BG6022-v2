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
    parser.add_argument(
        "--planner", choices=("deepseek_chat", "baseline"), default="deepseek_chat"
    )
    parser.add_argument("--new", "--new-conversation", action="store_true")
    parser.add_argument("--conversation")
    parser.add_argument("--json", action="store_true")
    return parser


def _pointer_path(state_root: Path) -> Path:
    return state_root / "last_session.json"


def _load_pointer(path: Path, state_root: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    if Path(str(value.get("state_root", ""))).resolve() != state_root:
        return None
    conversation_id = value.get("conversation_id")
    return conversation_id if isinstance(conversation_id, str) and conversation_id else None


def _save_pointer(path: Path, state_root: Path, conversation_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "conversation_id": conversation_id,
        "state_root": str(state_root),
        "updated_at_utc": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _configured_deepseek() -> tuple[bool, str]:
    from orca_agent.llm.deepseek_chat import DeepSeekChatAdapter

    adapter = DeepSeekChatAdapter()
    if not adapter.api_key:
        return False, "DEEPSEEK_API_KEY 未配置"
    if not adapter.model:
        return False, "BG6022_P7_MODEL 未配置"
    return True, ""


def main() -> int:
    args = _build_parser().parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    os.chdir(repository_root)
    state_root = (repository_root / ".tmp" / "p7" / "chat").resolve()
    lock_path = state_root / "chat.lock"
    pointer_path = _pointer_path(state_root)

    if args.planner == "deepseek_chat":
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

    try:
        with SingleInstanceLock(lock_path):
            return _run_chat(args, repository_root, state_root, pointer_path)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2


def _run_chat(args, repository_root: Path, state_root: Path, pointer_path: Path) -> int:
    from orca_agent.bootstrap.p7_modules import build_p7_runtime
    from orca_agent.interfaces.p7_chat import P7ChatDriver

    runtime = build_p7_runtime(
        state_root,
        planner_name=args.planner,
        allow_llm=args.planner == "deepseek_chat",
        fallback="none",
        backend_kind="fake",
        allow_real_orca=False,
    )
    selected = args.conversation
    if selected is None and not args.new:
        selected = _load_pointer(pointer_path, state_root)
    if selected is not None:
        try:
            selected = str(runtime.conversation.get_state(selected).conversation_id)
        except Exception:
            selected = None
    if selected is None:
        selected = str(runtime.conversation.new_conversation()["conversation_id"])
    _save_pointer(pointer_path, state_root, selected)

    def remember(conversation_id: str) -> None:
        _save_pointer(pointer_path, state_root, conversation_id)

    driver = P7ChatDriver(
        runtime,
        selected,
        auto_work=True,
        max_effects=8,
        max_seconds=1.5,
        json_output=args.json,
        on_conversation_changed=remember,
    )
    return driver.run()


if __name__ == "__main__":
    raise SystemExit(main())
