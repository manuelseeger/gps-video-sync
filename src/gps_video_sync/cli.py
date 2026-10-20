from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import numpy as np

from . import __version__, read_track, read_video, synchronize, match_videos
from .sync import validate_telemetry


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Cannot encode {type(value).__name__}")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=_json_default,
                                    allow_nan=False) + "\n")


def _telemetry(args):
    return read_video(args.videos[0], source=args.source, hz=args.hz, cache_dir=args.cache_dir)


def _export(telemetry, path):
    validate_telemetry(telemetry)
    recording = dict(video=telemetry.video, duration=telemetry.duration,
                     media_offset_seconds=telemetry.media_offset_seconds)
    if telemetry.clock is not None:
        recording["clock"] = asdict(telemetry.clock)
    if Path(path).suffix.lower() == ".npz":
        reserved = {"time", "gps_time", "gps_utc", "recording", "chapters", "schema_version"}
        if reserved.intersection(telemetry.channels):
            raise ValueError("Telemetry channel name conflicts with NPZ metadata")
        np.savez_compressed(path, time=telemetry.time, **telemetry.channels,
                            gps_time=telemetry.gps_time, gps_utc=telemetry.gps_utc,
                            recording=json.dumps(recording, default=_json_default),
                            schema_version="2.0.0")
    elif Path(path).suffix.lower() == ".json":
        def nullable(values):
            values = np.asarray(values)
            return np.where(np.isfinite(values), values, None).tolist()
        write_json(path, dict(schema_version="2.0.0", time=nullable(telemetry.time),
                             channels={k: nullable(v) for k, v in telemetry.channels.items()},
                             recording=recording, gps_time=nullable(telemetry.gps_time),
                             gps_utc=nullable(telemetry.gps_utc)))
    else:
        raise ValueError("--out must end in .npz or .json")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _parser():
    parser = _Parser(description="Synchronize video telemetry with a GPS track")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("sync", "telemetry", "verify", "info"):
        sub = commands.add_parser(command)
        sub.add_argument("videos", metavar="VIDEO", nargs="+" if command == "sync" else 1)
        sub.add_argument("--source", default="gopro", help="video source plugin, or npz")
        sub.add_argument("--cache-dir", type=Path)
        sub.add_argument("--hz", type=float, default=10, help="telemetry sample rate (default: 10)")
        if command in {"sync", "verify"}:
            sub.add_argument("--track", required=True, type=Path)
            sub.add_argument("--track-format", default="igc")
            sub.add_argument("--altitude", choices=("auto", "gps", "pressure"), default="auto")
        if command == "sync":
            sub.add_argument("--json", type=Path, help="write result here instead of stdout")
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
        if args.command in {"telemetry", "info"}:
            telemetry = _telemetry(args)
            if args.command == "telemetry":
                _export(telemetry, args.out)
            else:
                print(json.dumps(dict(schema_version="2.0.0", video=telemetry.video,
                                      input_file=str(Path(args.videos[0]).expanduser().resolve()),
                                      duration=telemetry.duration, samples=len(telemetry.time),
                                      start_seconds=float(telemetry.time[0]) if len(telemetry.time) else None,
                                      end_seconds=float(telemetry.time[-1]) if len(telemetry.time) else None,
                                      channels={k: list(v.shape) for k, v in telemetry.channels.items()},
                                      gps_samples=len(telemetry.gps_time)),
                                 indent=2, default=_json_default, allow_nan=False))
            return 0
        try:
            track = read_track(args.track, format=args.track_format, altitude=args.altitude)
        except KeyError as exc:
            if exc.args == (args.track_format,):
                raise ValueError(f"Unknown track format: {args.track_format}") from exc
            raise
        if args.command == "sync":
            results = [r.to_dict() for r in match_videos(
                track, args.videos, source=args.source, hz=args.hz, cache_dir=args.cache_dir)]
            document = dict(schema_version="2.0.0", videos=results)
            if args.json:
                write_json(args.json, document)
            else:
                print(json.dumps(document, indent=2, default=_json_default, allow_nan=False))
        else:
            from .verify import plot_verification, select_result
            telemetry = _telemetry(args)
            result = (select_result(json.loads(args.result.read_text()), telemetry)
                      if args.result else synchronize(track, telemetry).to_dict())
            results = [result]
            if result["status"] == "ok":
                plot_verification(track, telemetry, result, args.output, args.frames or [])
        for result in results:
            if result["status"] != "ok":
                print(f"{result.get('input_file') or result.get('video')}: {result['status']}: "
                      + (result.get("message") or "No unique alignment; inspect confidence."), file=sys.stderr)
        if any(r["status"] == "failed" for r in results):
            return 1
        return 2 if any(r["status"] == "ambiguous" for r in results) else 0
    except (OSError, ValueError, KeyError, RuntimeError, ImportError, EOFError,
            zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail += ": " + (exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr).strip()
        print(f"error: {detail}", file=sys.stderr)
        return 1
