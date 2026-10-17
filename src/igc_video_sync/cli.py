"""Command line interface and a portable, pickle-free telemetry interchange format."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import numpy as np

from . import __version__, read_track, read_video, synchronize
from .models import Telemetry


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Cannot encode {type(value).__name__}")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=_json_default,
                                    allow_nan=False) + "\n")


def read_npz(path):
    """Read one exported NPZ without loading Python pickle objects."""
    with np.load(path, allow_pickle=False) as data:
        time = np.asarray(data["time"], dtype=float)
        if time.ndim != 1 or not len(time) or not np.all(np.isfinite(time)):
            raise ValueError("NPZ time must be a nonempty finite one-dimensional array")
        if np.any(np.diff(time) <= 0):
            raise ValueError("NPZ time must be strictly increasing")
        reserved = {"time", "gps_time", "gps_utc", "chapters", "schema_version"}
        channels = {k: np.asarray(data[k], dtype=float) for k in data.files if k not in reserved}
        for key, values in channels.items():
            if values.ndim == 0 or len(values) != len(time):
                raise ValueError(f"NPZ channel {key!r} must match time length")
        gps_time = np.asarray(data["gps_time"], dtype=float) if "gps_time" in data else np.empty(0)
        gps_utc = np.asarray(data["gps_utc"], dtype=float) if "gps_utc" in data else np.empty(0)
        if gps_time.ndim != 1 or gps_utc.shape != gps_time.shape:
            raise ValueError("NPZ gps_time and gps_utc must be matching one-dimensional arrays")
        chapters = json.loads(str(data["chapters"].item())) if "chapters" in data else []
        if not isinstance(chapters, list) or any(not isinstance(c, dict) for c in chapters):
            raise ValueError("NPZ chapters must be a JSON list of objects")
        return Telemetry(time, channels, chapters, gps_time, gps_utc)


def _telemetry(args):
    if not np.isfinite(args.hz) or args.hz <= 0:
        raise ValueError("--hz must be a positive finite number")
    if args.source == "npz":
        if len(args.videos) != 1:
            raise ValueError("--source npz requires exactly one exported NPZ file")
        return read_npz(args.videos[0])
    try:
        return read_video(args.videos, source=args.source, hz=args.hz, cache_dir=args.cache_dir)
    except KeyError as exc:
        if exc.args == (args.source,):
            raise ValueError(f"Unknown or unavailable video source: {args.source}") from exc
        raise


def _export(telemetry, path):
    if Path(path).suffix.lower() == ".npz":
        reserved = {"time", "gps_time", "gps_utc", "chapters", "schema_version"}
        if reserved.intersection(telemetry.channels):
            raise ValueError("Telemetry channel name conflicts with NPZ metadata")
        np.savez_compressed(path, time=telemetry.time, **telemetry.channels,
                            gps_time=telemetry.gps_time, gps_utc=telemetry.gps_utc,
                            chapters=json.dumps(telemetry.chapters, default=_json_default),
                            schema_version="1.0.0")
    elif Path(path).suffix.lower() == ".json":
        write_json(path, dict(schema_version="1.0.0", time=telemetry.time,
                             channels=telemetry.channels, chapters=telemetry.chapters,
                             gps_time=telemetry.gps_time, gps_utc=telemetry.gps_utc))
    else:
        raise ValueError("--out must end in .npz or .json")


def _parser():
    parser = argparse.ArgumentParser(description="Synchronize video telemetry with a GPS track")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("sync", "telemetry", "verify", "info"):
        sub = commands.add_parser(command)
        sub.add_argument("videos", metavar="VIDEO", nargs="+")
        sub.add_argument("--source", default="gopro", help="video source plugin, or npz")
        sub.add_argument("--cache-dir", type=Path)
        sub.add_argument("--hz", type=float, default=10, help="telemetry sample rate (default: 10)")
        if command in {"sync", "verify"}:
            sub.add_argument("--track", required=True, type=Path)
            sub.add_argument("--track-format", default="igc")
            sub.add_argument("--altitude", choices=("auto", "gps", "pressure"), default="auto")
        if command == "sync":
            sub.add_argument("--json", required=True, type=Path)
        elif command == "telemetry":
            sub.add_argument("--out", required=True, type=Path)
        elif command == "verify":
            sub.add_argument("--output", required=True, type=Path)
            sub.add_argument("--result", type=Path)
            sub.add_argument("--frames", nargs="+", type=float, metavar="VIDEO_SECONDS")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        telemetry = _telemetry(args)
        if args.command == "telemetry":
            _export(telemetry, args.out)
            return 0
        if args.command == "info":
            print(json.dumps(dict(schema_version="1.0.0", samples=len(telemetry.time),
                                  start_seconds=float(telemetry.time[0]) if len(telemetry.time) else None,
                                  end_seconds=float(telemetry.time[-1]) if len(telemetry.time) else None,
                                  channels={k: list(v.shape) for k, v in telemetry.channels.items()},
                                  gps_samples=len(telemetry.gps_time), chapters=telemetry.chapters),
                             indent=2, default=_json_default, allow_nan=False))
            return 0
        try:
            track = read_track(args.track, format=args.track_format, altitude=args.altitude)
        except KeyError as exc:
            if exc.args == (args.track_format,):
                raise ValueError(f"Unknown track format: {args.track_format}") from exc
            raise
        result = (json.loads(args.result.read_text()) if args.command == "verify" and args.result
                  else synchronize(track, telemetry).to_dict())
        if not isinstance(result, dict):
            raise ValueError("Sync result must be a JSON object")
        if args.command == "sync":
            write_json(args.json, result)
        else:
            from .verify import plot_verification
            plot_verification(track, telemetry, result, args.output, args.frames or [], args.videos)
        if result.get("status") == "ambiguous":
            print("ambiguous: " + (result.get("message") or "No unique alignment; inspect confidence."), file=sys.stderr)
            return 2
        if result.get("status") not in {"ok", "success"}:
            print("error: " + (result.get("message") or str(result.get("status"))), file=sys.stderr)
            return 1
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, ImportError, EOFError,
            zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail += ": " + (exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr).strip()
        print(f"error: {detail}", file=sys.stderr)
        return 1
