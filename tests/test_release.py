import hashlib
import importlib.util
import json
from io import BytesIO
from pathlib import Path
import tarfile
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_release", Path(__file__).resolve().parents[1] / "scripts/check_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
INDEX_SPEC = importlib.util.spec_from_file_location(
    "check_index", Path(__file__).resolve().parents[1] / "scripts/check_index.py")
index = importlib.util.module_from_spec(INDEX_SPEC)
INDEX_SPEC.loader.exec_module(index)


@pytest.fixture
def archives(tmp_path):
    version = "0.1.0"
    prefix = f"gps_video_sync-{version}"
    info = f"{prefix}.dist-info"
    metadata = b"Name: gps-video-sync\nVersion: 0.1.0\n"
    wheel = {f"gps_video_sync/{name}": b"" for name in release.MODULES}
    wheel["gps_video_sync/__init__.py"] = b'__version__ = "0.1.0"\n'
    wheel.update({f"{info}/{name}": b"" for name in (
        "METADATA", "WHEEL", "entry_points.txt", "RECORD", "licenses/LICENSE")})
    wheel[f"{info}/METADATA"] = metadata
    source = {f"src/{name}": data for name, data in wheel.items()
              if name.startswith("gps_video_sync/")}
    source.update({name: b"" for name in (
        ".gitignore", "README.md", "LICENSE", "pyproject.toml", "PKG-INFO",
        "docs/releasing.md")})
    source["PKG-INFO"] = metadata

    def write():
        with zipfile.ZipFile(tmp_path / f"{prefix}-py3-none-any.whl", "w") as archive:
            for name, data in wheel.items():
                archive.writestr(name, data)
        with tarfile.open(tmp_path / f"{prefix}.tar.gz", "w:gz") as archive:
            for name, data in source.items():
                member = tarfile.TarInfo(f"{prefix}/{name}")
                member.size = len(data)
                archive.addfile(member, BytesIO(data))
        return tmp_path

    return wheel, source, write


@pytest.mark.parametrize("changed", [False, True])
def test_index_hashes_match_both_archives(tmp_path, monkeypatch, changed):
    wheel = tmp_path / "gps_video_sync-0.1.0-py3-none-any.whl"
    source = tmp_path / "gps_video_sync-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")
    remote = {"urls": [{"filename": path.name,
                        "digests": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
                       for path in (wheel, source)]}
    if changed:
        remote["urls"][1]["digests"]["sha256"] = "wrong-source-hash"
    monkeypatch.setattr(index, "urlopen", lambda *args, **kwargs: BytesIO(json.dumps(remote).encode()))
    if changed:
        with pytest.raises(ValueError, match="wheel/source hashes"):
            index.check(tmp_path, "0.1.0", "testpypi")
    else:
        index.check(tmp_path, "0.1.0", "testpypi")


def test_release_archives(archives):
    _, _, write = archives
    release.check(write(), "0.1.0")


@pytest.mark.parametrize("name", ["sync.json", "tests/fixtures/flight.igc", "private.mp4"])
def test_source_excludes_private_files(archives, name):
    _, source, write = archives
    source[name] = b"private"
    with pytest.raises(ValueError, match="Unexpected or missing source files"):
        release.check(write())


def test_wheel_excludes_private_files(archives):
    wheel, _, write = archives
    wheel["gps_video_sync/sync.json"] = b"private"
    with pytest.raises(ValueError, match="Unexpected or missing wheel files"):
        release.check(write())


def test_tag_version_must_match(archives):
    _, _, write = archives
    with pytest.raises(ValueError, match="does not match tag"):
        release.check(write(), "1.0.0")


def test_api_version_must_match(archives):
    wheel, source, write = archives
    wheel["gps_video_sync/__init__.py"] = b'__version__ = "1.0.0"\n'
    source["src/gps_video_sync/__init__.py"] = wheel["gps_video_sync/__init__.py"]
    with pytest.raises(ValueError, match="API version"):
        release.check(write())


def test_source_must_match_wheel(archives):
    _, source, write = archives
    source["src/gps_video_sync/cli.py"] = b"# different\n"
    with pytest.raises(ValueError, match="Source and wheel differ"):
        release.check(write())
