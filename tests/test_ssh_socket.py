from pathlib import Path
import os
import tempfile
import pytest
from zai_devdashd import probes


@pytest.fixture
def short_home(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ssh-") as name:
        home = Path(name).resolve()
        home.chmod(0o700)
        monkeypatch.setattr(Path, "home", lambda: home)
        yield home


def test_socket_path_safe_with_space_in_root(short_home):
    root = short_home / "Application Support" / "zai-devdashd"
    path = probes.worker_socket_path(root)
    assert not any(c.isspace() for c in str(path))
    assert len(str(path)) < 100
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_default_socket_has_no_space(short_home):
    assert " " not in str(probes.worker_socket_path())


def test_unusable_home_refuses_before_creating_paths(tmp_path, monkeypatch):
    home = tmp_path / "Home with spaces"
    monkeypatch.setattr(Path, "home", lambda: home)
    with pytest.raises(ValueError):
        probes.worker_socket_path()
    assert not home.exists()


def test_socket_namespace_refuses_symlink(short_home):
    target = short_home / "target"
    target.mkdir(mode=0o700)
    (short_home / ".zai-devdashd").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        probes.worker_socket_path()
    assert list(target.iterdir()) == []
