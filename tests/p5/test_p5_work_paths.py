import os

import pytest

from orca_agent.execution.work_paths import execution_directory


@pytest.mark.parametrize(
    "value",
    ["../escape", "execution_../a", "execution_a:b", "C:\\tmp", "NUL", "/tmp", "execution_"],
)
def test_work_directory_rejects_untrusted_names(tmp_path, value):
    with pytest.raises(ValueError):
        execution_directory(tmp_path, value, create=True)
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("component", ["work", "execution", "stdout"])
def test_work_directory_rejects_aliases_before_access(tmp_path, component):
    execution = "execution_" + "a" * 32
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = tmp_path / "work" / execution
    target = {"work": directory.parent, "execution": directory, "stdout": directory / "stdout.out"}[
        component
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and component != "stdout":
        import subprocess

        # A junction needs no Developer Mode; no shell deletion or moving.
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "New-Item -ItemType Junction -Path $env:P5_TEST_LINK -Target $env:P5_TEST_TARGET",
            ],
            capture_output=True,
            env={**os.environ, "P5_TEST_LINK": str(target), "P5_TEST_TARGET": str(outside)},
        )
        assert result.returncode == 0, result.stderr
    else:
        try:
            target.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("host cannot create a symbolic link")
    with pytest.raises(ValueError):
        execution_directory(tmp_path, execution, create=True)
    assert not list(outside.iterdir())
