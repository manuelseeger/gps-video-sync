import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen


def check(directory, version, index):
    host = {"testpypi": "test.pypi.org", "pypi": "pypi.org"}[index]
    with urlopen(f"https://{host}/pypi/gps-video-sync/{version}/json", timeout=30) as response:
        release = json.load(response)
    local = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in directory.iterdir() if path.suffix == ".whl" or path.name.endswith(".tar.gz")}
    remote = {item["filename"]: item["digests"]["sha256"] for item in release["urls"]}
    if len(local) != 2 or local != remote:
        raise ValueError("Index wheel/source hashes do not match the approved build")
    print(f"Both {index} archives match the approved build")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Compare both local release archive hashes with the index release metadata.')
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--index", choices=["testpypi", "pypi"], required=True)
    args = parser.parse_args()
    check(args.directory, args.version, args.index)
