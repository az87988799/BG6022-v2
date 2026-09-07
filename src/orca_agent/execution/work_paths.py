"""Reject work-directory aliases before resolving or creating execution paths."""

import re
from pathlib import Path


def _reject_alias(path):
    if path.is_symlink() or (
        path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("execution paths cannot contain symlinks or reparse points")


def execution_directory(root: Path, execution_id: str, *, create=False) -> Path:
    if re.fullmatch(r"execution_[0-9a-f]{32}", execution_id) is None:
        raise ValueError("invalid execution directory identity")
    work = root / "work"
    directory = work / execution_id
    for path in (work, directory):
        _reject_alias(path)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError("execution directory is missing")
    for path in directory.iterdir():
        _reject_alias(path)
    directory.resolve().relative_to(root.resolve())
    return directory
