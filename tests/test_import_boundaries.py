import ast
import sys
from pathlib import Path

DOMAIN_ROOT = Path(__file__).parents[1] / "src" / "orca_agent" / "domain"
FORBIDDEN_MODULES = {
    "subprocess",
    "sqlite3",
    "socket",
    "requests",
    "httpx",
    "rdkit",
    "openai",
    "langchain",
}
ALLOWED_EXTERNAL_MODULES = set(sys.stdlib_module_names) | {
    "pydantic",
    "pydantic_core",
}


def _top_level(module: str) -> str:
    return module.split(".", maxsplit=1)[0]


def test_domain_imports_have_no_runtime_or_network_dependencies() -> None:
    violations: list[str] = []
    for source_file in DOMAIN_ROOT.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top_level = _top_level(module)
                if top_level in FORBIDDEN_MODULES or top_level not in ALLOWED_EXTERNAL_MODULES:
                    violations.append(f"{source_file.name}:{node.lineno}:{module}")

    assert violations == [], "domain import boundary violations: " + ", ".join(violations)
