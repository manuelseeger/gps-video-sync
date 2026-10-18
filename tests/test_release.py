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
TAG_SPEC = importlib.util.spec_from_file_location(
    "release_tag", Path(__file__).resolve().parents[1] / "scripts/release_tag.py")
tag = importlib.util.module_from_spec(TAG_SPEC)
TAG_SPEC.loader.exec_module(tag)


@pytest.fixture
def archives(tmp_path, request):
    version = getattr(request, "param", "0.1.0")
    prefix = f"gps_video_sync-{version}"
    info = f"{prefix}.dist-info"
    metadata = f"Name: gps-video-sync\nVersion: {version}\n".encode()
    wheel = {f"gps_video_sync/{name}": b"" for name in release.MODULES}
    wheel["gps_video_sync/__init__.py"] = f'__version__ = "{version}"\n'.encode()
    wheel.update({f"{info}/{name}": b"" for name in (
        "METADATA", "WHEEL", "entry_points.txt", "RECORD", "licenses/LICENSE")})
    wheel[f"{info}/METADATA"] = metadata
    source = {f"src/{name}": data for name, data in wheel.items()
              if name.startswith("gps_video_sync/")}
    source.update({name: b"" for name in (
        ".gitignore", "README.md", "LICENSE", "pyproject.toml", "PKG-INFO",
        "docs/reference.md")})
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


@pytest.mark.parametrize("archives", ["0.1.0", "3.2.1rc1"], indirect=True)
def test_release_archives(archives):
    _, _, write = archives
    release.check(write())


@pytest.mark.parametrize("value,version,environment,prerelease", [
    ("3.2.1", "3.2.1", "pypi", "false"),
    ("0.1.0", "0.1.0", "pypi", "false"),
    ("3.2.1-preview.1", "3.2.1rc1", "testpypi", "true"),
    ("3.2.1-preview.12", "3.2.1rc12", "testpypi", "true"),
])
def test_release_tag_routing(value, version, environment, prerelease):
    assert tag.release_config(value) == {
        "version": version, "environment": environment, "prerelease": prerelease,
    }


@pytest.mark.parametrize("value", [
    "v3.2.1", "3.2", "3.2.1rc1", "3.2.1-preview.0", "3.2.1-preview.01",
    "03.2.1", "3.02.1", "3.2.01", "3.2.1-preview", "3.2.1-beta.1",
    "3.2.1-preview.1-extra", "3.2.1\n", "3.2.1;echo unsafe",
])
def test_invalid_release_tags(value):
    with pytest.raises(ValueError, match="Expected major.minor.patch"):
        tag.release_config(value)


@pytest.mark.parametrize("archives", ["3.2.1-preview.1", "3.2.1rc0", "03.2.1"], indirect=True)
def test_noncanonical_package_versions(archives):
    _, _, write = archives
    with pytest.raises(ValueError, match="Invalid project name or version"):
        release.check(write())


@pytest.mark.parametrize("archives", ["3.2.1rc1"], indirect=True)
def test_preview_tag_matches_canonical_archive_version(archives):
    _, _, write = archives
    config = tag.release_config("3.2.1-preview.1")
    release.check(write(), config["version"])
    with pytest.raises(ValueError, match="does not match tag"):
        release.check(write(), "3.2.1")


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
