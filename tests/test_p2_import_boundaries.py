import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "orca_agent"
FORBIDDEN_TOP_LEVEL = {
    "httpx",
    "langchain",
    "openai",
    "rdkit",
    "requests",
    "socket",
    "subprocess",
}


def _top_level(module: str) -> str:
    return module.split(".", maxsplit=1)[0]


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def test_p2_package_has_no_external_execution_or_network_imports() -> None:
    violations = [
        f"{path.name}:{module}"
        for path in PACKAGE_ROOT.rglob("*.py")
        for module in _imports(path)
        if _top_level(module) in FORBIDDEN_TOP_LEVEL
    ]
    assert violations == []


def test_reducer_does_not_import_infrastructure_or_io() -> None:
    reducer = PACKAGE_ROOT / "orchestration" / "reducer.py"
    modules = _imports(reducer)
    assert all(_top_level(module) not in {"sqlite3", "pathlib"} for module in modules)
    assert all(not module.startswith("orca_agent.infrastructure") for module in modules)


def test_no_future_execution_package_was_created() -> None:
    assert not (PACKAGE_ROOT / "execution").exists()
    assert not (PACKAGE_ROOT / "orca").exists()
    assert not (PACKAGE_ROOT / "evidence").exists()
    assert not (PACKAGE_ROOT / "reporting").exists()
