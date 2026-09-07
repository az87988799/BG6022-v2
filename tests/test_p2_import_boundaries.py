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
ADAPTER_ALLOWLIST = {
    "identity/rdkit_normalizer.py": {"rdkit"},
    "identity/geometry.py": {"rdkit"},
    "identity/optimized_compatibility.py": {"rdkit"},
    "identity/http_pubchem.py": {"httpx"},
    "execution/local_backend.py": {"subprocess"},
    "execution/local_runner.py": {"subprocess"},
    "execution/orca_config.py": {"subprocess"},
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
        elif isinstance(node, ast.Call):
            # Dynamic loading is forbidden, including aliased importlib entry points.
            dynamic_names = {"import_module", "__import__"}
            dynamic_names.update(
                alias.asname or alias.name
                for item in ast.walk(tree)
                if isinstance(item, ast.ImportFrom) and item.module == "importlib"
                for alias in item.names
                if alias.name == "import_module"
            )
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
            )
            if name in dynamic_names:
                modules.append("dynamic_import")
    return modules


def test_p2_package_has_no_external_execution_or_network_imports() -> None:
    violations = [
        f"{path.name}:{module}"
        for path in PACKAGE_ROOT.rglob("*.py")
        for module in _imports(path)
        if module == "dynamic_import"
        or (
            _top_level(module) in FORBIDDEN_TOP_LEVEL
            and _top_level(module)
            not in ADAPTER_ALLOWLIST.get(path.relative_to(PACKAGE_ROOT).as_posix(), set())
        )
    ]
    assert violations == []


def test_reducer_does_not_import_infrastructure_or_io() -> None:
    reducer = PACKAGE_ROOT / "orchestration" / "reducer.py"
    modules = _imports(reducer)
    assert all(_top_level(module) not in {"sqlite3", "pathlib"} for module in modules)
    assert all(not module.startswith("orca_agent.infrastructure") for module in modules)


def test_dynamic_import_aliases_are_detected(tmp_path):
    for source in (
        "from importlib import import_module as load; load('rdkit')",
        "import importlib as loader; loader.import_module('httpx')",
        "__import__('httpx')",
    ):
        path = tmp_path / "bad_adapter.py"
        path.write_text(source, encoding="utf-8")
        assert "dynamic_import" in _imports(path)


def test_p3_execution_stages_keep_the_offline_boundary() -> None:
    assert (PACKAGE_ROOT / "execution").exists()
    assert (PACKAGE_ROOT / "evidence").exists()
    assert (PACKAGE_ROOT / "reporting").exists()
    assert not (PACKAGE_ROOT / "orca").exists()
    p5_local_files = {
        "execution/local_backend.py",
        "execution/local_runner.py",
        "execution/orca_config.py",
    }
    violations = [
        f"{path.name}:{module}"
        for stage in ("execution", "evidence", "reporting")
        for path in (PACKAGE_ROOT / stage).rglob("*.py")
        if path.relative_to(PACKAGE_ROOT).as_posix() not in p5_local_files
        for module in _imports(path)
        if _top_level(module) in {"subprocess", "socket", "requests", "httpx", "openai"}
    ]
    assert violations == []
