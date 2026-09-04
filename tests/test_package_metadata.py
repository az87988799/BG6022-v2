from importlib import metadata, resources

import orca_agent


def test_package_version_has_one_metadata_source() -> None:
    assert orca_agent.__version__ == metadata.version("orca-agent")
    assert orca_agent.__version__ == "0.1.0"


def test_py_typed_marker_is_packaged() -> None:
    assert resources.files("orca_agent").joinpath("py.typed").is_file()
