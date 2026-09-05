import pytest

from orca_agent.application.errors import StateIntegrityError
from orca_agent.infrastructure.artifacts import ArtifactStore


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/outside",
        "C:/outside",
        "C:outside",
        "C:\\outside",
        "\\\\server\\share\\file",
        "a\\..\\outside",
    ],
)
def test_nonportable_artifact_paths(tmp_path, path):
    with pytest.raises(StateIntegrityError):
        ArtifactStore(tmp_path).path_for(path)
