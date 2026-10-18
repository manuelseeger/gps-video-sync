"""Validate a release tag and emit GitHub Actions outputs."""

import argparse
import re


def release_config(tag):
    match = re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-preview\.([1-9][0-9]*))?",
        tag,
    )
    if not match:
        raise ValueError("Expected major.minor.patch or major.minor.patch-preview.N (N >= 1)")
    base = ".".join(match.group(1, 2, 3))
    preview = match.group(4)
    return {
        "version": f"{base}rc{preview}" if preview else base,
        "environment": "testpypi" if preview else "pypi",
        "prerelease": "true" if preview else "false",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    args = parser.parse_args()
    for key, value in release_config(args.tag).items():
        print(f"{key}={value}")
