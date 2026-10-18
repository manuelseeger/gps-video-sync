import argparse
import ast
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
import re
import tarfile
import zipfile


MODULES = {
    "__init__.py", "__main__.py", "_chapter_order.py", "cli.py", "gopro.py", "gpmf.py",
    "igc.py", "models.py", "mp4.py", "plugins.py", "sync.py", "verify.py",
}


def check(directory, expected_version=None):
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and one source archive")
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    metadata_names = [name for name in wheel if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise ValueError("Expected one wheel METADATA file")
    metadata = BytesParser().parsebytes(wheel[metadata_names[0]])
    version = metadata["Version"]
    if metadata["Name"] != "gps-video-sync" or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?", version):
        raise ValueError("Invalid project name or version (expected major.minor.patch with optional rcN)")
    if expected_version and version != expected_version:
        raise ValueError(f"Version {version} does not match tag {expected_version}")
    dist_info = f"gps_video_sync-{version}.dist-info"
    allowed_wheel = {f"gps_video_sync/{name}" for name in MODULES} | {
        f"{dist_info}/{name}" for name in (
            "METADATA", "WHEEL", "entry_points.txt", "RECORD", "licenses/LICENSE"
        )
    }
    if set(wheel) != allowed_wheel:
        raise ValueError(f"Unexpected or missing wheel files: {set(wheel) ^ allowed_wheel}")
    with tarfile.open(sources[0]) as archive:
        source = {}
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"Non-regular archive entry: {member.name}")
            path = PurePosixPath(member.name)
            if path.parts[0] != f"gps_video_sync-{version}" or ".." in path.parts:
                raise ValueError(f"Invalid archive path: {member.name}")
            source[str(PurePosixPath(*path.parts[1:]))] = archive.extractfile(member).read()
    allowed_source = {f"src/gps_video_sync/{name}" for name in MODULES} | {
        ".gitignore", "README.md", "LICENSE", "pyproject.toml", "PKG-INFO",
        "docs/reference.md",
    }
    if set(source) != allowed_source:
        raise ValueError(f"Unexpected or missing source files: {set(source) ^ allowed_source}")
    source_metadata = BytesParser().parsebytes(source["PKG-INFO"])
    if source_metadata["Version"] != version or source_metadata["Name"] != metadata["Name"]:
        raise ValueError("Source and wheel metadata differ")
    for name in MODULES:
        if source[f"src/gps_video_sync/{name}"] != wheel[f"gps_video_sync/{name}"]:
            raise ValueError(f"Source and wheel differ: {name}")
    tree = ast.parse(wheel["gps_video_sync/__init__.py"])
    api_version = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "__version__"
                               for target in node.targets))
    if api_version != version:
        raise ValueError("Python API version does not match package metadata")
    for name in sorted(wheel):
        print(f"wheel: {name}")
    for name in sorted(source):
        print(f"sdist: {name}")
    print(f"Validated gps-video-sync {version}; manually review all text before publication.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Check release archive contents and version consistency (standard library only).')
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", help="Expected canonical package version, such as 3.2.1 or 3.2.1rc1")
    args = parser.parse_args()
    check(args.directory, args.version)
