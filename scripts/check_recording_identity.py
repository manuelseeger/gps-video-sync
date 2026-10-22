"""Check explicit identity pairs and optional GoPro filename hints without private output."""
import argparse
from collections import Counter
from itertools import combinations, product
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

from gps_video_sync import (
    IdentityFailed, IdentityUnavailable, RecordingIdentity, compare_identity,
    identity_from_dict, read_recording_identity,
)


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": "invalid_arguments"}))
        raise SystemExit(2)


def survey(folder):
    files = sorted(p for p in folder.iterdir() if p.suffix.upper() in (".MP4", ".LRV") and p.is_file())
    groups = {}
    for path in files:
        match = re.match(r"^G([HXL])([0-9]{2})([0-9]{4})(?:[^0-9].*)?\.(MP4|LRV)$", path.name, re.I)
        if match is None:
            continue
        kind, chapter, recording, extension = (v.upper() for v in match.groups())
        if (kind == "L") != (extension == "LRV"):
            continue
        group = groups.setdefault((recording, chapter), {"MP4": [], "LRV": []})
        group[extension].append(path)
    pairs = [pair for group in groups.values() for pair in product(group["MP4"], group["LRV"])]
    chapters, captures = [], []
    for (left, a), (right, b) in combinations(groups.items(), 2):
        target = chapters if left[0] == right[0] else captures
        target.extend(product(a["MP4"] + a["LRV"], b["MP4"] + b["LRV"]))
    return files, pairs, chapters, captures


def fingerprint(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def check(args):
    positives = [tuple(map(Path, pair)) for pair in args.pair]
    negatives = [tuple(map(Path, pair)) for pair in args.different]
    paths = {p for pair in positives + negatives for p in pair}
    hint_pairs, chapters, captures = [], [], []
    for folder in args.gopro_folder:
        files, pairs, chapter_pairs, capture_pairs = survey(Path(folder))
        if not pairs:
            raise ValueError("No survey pairs")
        paths.update(files)
        hint_pairs.extend(pairs)
        chapters.extend(chapter_pairs)
        captures.extend(capture_pairs)
    results = {}
    unavailable, failed = Counter(), Counter()
    totals = Counter(files=len(paths), available=0, checks_failed=0, alias_checks=0,
                     alias_same=0, repeat_and_persistence_passed=0, cross_recording_collisions=0)
    with TemporaryDirectory(prefix="recording-identity-") as directory:
        work = Path(directory)
        moved = work / "moved"
        moved.mkdir()
        for index, path in enumerate(sorted(paths)):
            before = fingerprint(path)
            result = read_recording_identity(path)
            results[path] = result
            if isinstance(result, RecordingIdentity):
                totals["available"] += 1
            elif isinstance(result, IdentityUnavailable):
                unavailable[result.reason] += 1
            elif isinstance(result, IdentityFailed):
                failed[result.code] += 1
            repeated = read_recording_identity(path)
            restored = identity_from_dict(json.loads(json.dumps(result.to_dict())))
            if result.to_dict() == repeated.to_dict() == restored.to_dict():
                totals["repeat_and_persistence_passed"] += 1
            else:
                totals["checks_failed"] += 1
            if isinstance(result, RecordingIdentity):
                alias = work / f"alias-{index}.MP4"
                alias.symlink_to(path.resolve(strict=True))
                same = compare_identity(result, read_recording_identity(alias)) == "same"
                alias = alias.rename(moved / f"renamed-{index}.LRV")
                same = same and compare_identity(result, read_recording_identity(alias)) == "same"
                totals["alias_checks"] += 1
                totals["alias_same"] += int(same)
                totals["checks_failed"] += int(not same)
            totals["checks_failed"] += int(before != fingerprint(path))
    for name, pairs, expected in (("positive", positives, "same"), ("negative", negatives, "different"),
                                   ("survey", hint_pairs, "same"), ("chapter", chapters, "different"),
                                   ("cross_recording", captures, "different")):
        totals[name + "_pairs"] = len(pairs)
        passed = 0
        for a, b in pairs:
            verdict = compare_identity(results[a], results[b])
            passed += int(verdict == expected)
            if name == "cross_recording" and verdict == "same":
                totals["cross_recording_collisions"] += 1
        totals[name + "_passed"] = passed
        totals["checks_failed"] += len(pairs) - passed
    return {**totals, "unavailable": dict(sorted(unavailable.items())), "failed": dict(sorted(failed.items()))}


def main(argv=None):
    parser = PrivateParser(prog="check_recording_identity.py", description=__doc__)
    parser.add_argument("--pair", nargs=2, action="append", default=[], metavar=("ORIGINAL", "PREVIEW"),
                        help="Expected matching pair. Repeat for arbitrary filenames.")
    parser.add_argument("--different", nargs=2, action="append", default=[], metavar=("LEFT", "RIGHT"),
                        help="Expected different recording or chapter. Repeat as needed.")
    parser.add_argument("--gopro-folder", action="append", default=[], metavar="DIRECTORY",
                        help="Survey direct MP4 and LRV children using GoPro filename hints.")
    args = parser.parse_args(argv)
    if not args.pair and not args.gopro_folder:
        parser.error("Supply positive pairs or a survey folder")
    try:
        summary = check(args)
    except (OSError, ValueError):
        print(json.dumps({"error": "input_or_check_error"}))
        return 2
    print(json.dumps(summary, sort_keys=True))
    return int(summary["checks_failed"] != 0)


if __name__ == "__main__":
    sys.exit(main())
