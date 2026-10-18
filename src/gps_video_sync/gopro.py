"""GoPro motion and GPS UTC on one recording's local timeline.

MP4 sample tables give GPMF timing and file positions. Motion retains native
camera axes; GPS times are Unix UTC seconds.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from dataclasses import dataclass
import zipfile

import numpy as np
from scipy.ndimage import uniform_filter1d

from . import mp4
from .gpmf import number, scaled, streams
from .models import Telemetry
from .sync import validate_telemetry

KEYS = ("ACCL", "GYRO", "GRAV", "CORI", "IORI")
QUATERNIONS = ("CORI", "IORI")
CACHE_VERSION = "2.0.0"
GPS_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()


def _gps(fields: dict, times: np.ndarray, key: str) -> tuple[np.ndarray, np.ndarray]:
    values = scaled(fields[key], fields)
    if key == "GPS9":
        if values.shape[1] != 9:
            raise ValueError("GPS9 requires nine columns")
        utc = GPS_EPOCH + values[:, 5] * 86400 + values[:, 6]
        good = ((values[:, 8] >= 3) & (values[:, 5] >= 0)
                & (values[:, 6] >= 0) & (values[:, 6] < 86401))
    else:
        if values.shape[1] != 5:
            raise ValueError("GPS5 requires five columns")
        if "GPSU" not in fields or "GPSF" not in fields:
            return np.empty(0), np.empty(0)
        try:
            stamp = fields["GPSU"].data.decode("ascii").rstrip("\0")
            fmt = "%y%m%d%H%M%S.%f" if "." in stamp else "%y%m%d%H%M%S"
            epoch = datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc).timestamp()
        except (UnicodeDecodeError, ValueError):
            return np.empty(0), np.empty(0)
        utc = epoch + (times - times[0])
        good = np.ones(len(values), dtype=bool)
    # GPSF, when present, must also report a 3D fix. GPS9 carries its own fix.
    if "GPSF" in fields:
        fix = number(fields["GPSF"]).ravel()
        if len(fix) not in (1, len(values)):
            raise ValueError("GPSF does not match GPS samples")
        good &= fix >= 3
    good &= np.isfinite(values).all(axis=1) & np.isfinite(utc)
    good &= (np.abs(values[:, 0]) <= 90) & (np.abs(values[:, 1]) <= 180)
    return times[good], utc[good]


@dataclass
class Recording:
    duration: float
    packets: int
    motion: dict
    gps: list


def _recording(path: Path) -> Recording:
    timeline = mp4.read_timeline(path)
    duration = timeline.duration
    data = {key: [] for key in KEYS}
    gps5, gps9 = [], []
    with path.open("rb") as handle:
        for sample in timeline.samples:
            pts, interval = sample.time, sample.duration
            handle.seek(sample.offset)
            buf = handle.read(sample.size)
            if len(buf) != sample.size:
                raise ValueError("Truncated GPMF packet in video file")
            packet_gps5, packet_gps9 = [], []
            has_gps9 = False
            for stream in streams(buf):
                fields = {r.key: r for r in stream}
                for key in KEYS:
                    if key not in fields:
                        continue
                    values = scaled(fields[key], fields)
                    expected = 4 if key in QUATERNIONS else 3
                    if values.shape[1] != expected:
                        raise ValueError(f"{key} requires {expected} columns")
                    if not len(values):
                        continue
                    t = pts + np.arange(len(values)) * interval / len(values)
                    mask = (t >= 0) & (t < duration)
                    data[key].append(np.column_stack((t[mask], values[mask])))
                for key, target in (("GPS9", packet_gps9), ("GPS5", packet_gps5)):
                    if key not in fields:
                        continue
                    has_gps9 |= key == "GPS9"
                    count = fields[key].count
                    if not count:
                        continue
                    t = pts + np.arange(count) * interval / count
                    gt, utc = _gps(fields, t, key)
                    mask = (gt >= 0) & (gt < duration)
                    if mask.any():
                        target.append((gt[mask], utc[mask]))
            # Do not replace rejected GPS9 fixes with a second GPS5 observation.
            if has_gps9:
                gps9.extend(packet_gps9)
            else:
                gps5.extend(packet_gps5)
    return Recording(duration, len(timeline.samples), data, gps5 + gps9)


def _resample(blocks: list, grid: np.ndarray, key: str, hz: float) -> np.ndarray | None:
    if not blocks or not len(grid):
        return None
    raw = np.concatenate(blocks)
    raw = raw[np.isfinite(raw).all(axis=1)]
    if key in QUATERNIONS:
        raw = raw[np.linalg.norm(raw[:, 1:], axis=1) > 0]
    if not len(raw):
        return None
    raw = raw[np.argsort(raw[:, 0], kind="stable")]
    raw = raw[np.r_[np.diff(raw[:, 0]) > 0, True]]
    if key in QUATERNIONS:
        values = raw[:, 1:]
        values /= np.linalg.norm(values, axis=1)[:, None]
        flip = np.r_[1, np.cumprod(np.where(np.sum(values[1:] * values[:-1], axis=1) < 0, -1, 1))]
        raw[:, 1:] = values * flip[:, None]
    elif len(raw) > 1:
        width = max(1, min(len(raw), round(1 / np.median(np.diff(raw[:, 0])) / hz)))
        raw[:, 1:] = uniform_filter1d(raw[:, 1:], width, axis=0, mode="nearest")
    result = np.column_stack([np.interp(grid, raw[:, 0], raw[:, i]) for i in range(1, raw.shape[1])])
    if key in QUATERNIONS:
        result /= np.linalg.norm(result, axis=1)[:, None]
    if len(raw) > 1:
        step = float(np.median(np.diff(raw[:, 0])))
        right = np.searchsorted(raw[:, 0], grid).clip(1, len(raw)-1)
        gap = (raw[right, 0]-raw[right-1, 0]) > max(.5, 3*step)
        outside = (grid < raw[0, 0]) | (grid > raw[-1, 0]+step)
        result[gap | outside] = np.nan
    return result


def _cache_path(path: Path, hz: float, directory: Path) -> Path:
    stat = path.stat()
    identity = {"version": CACHE_VERSION, "hz": hz, "file":
                [str(path), stat.st_size, stat.st_mtime_ns]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return directory / f"gopro-{digest}.npz"


def _load_cache(path: Path) -> Telemetry | None:
    try:
        with np.load(path, allow_pickle=False) as saved:
            meta = json.loads(str(saved["metadata"].item()))
            if meta["version"] != CACHE_VERSION or any(k not in KEYS for k in meta["channels"]):
                return None
            result = Telemetry(saved["time"], {k: saved[k] for k in meta["channels"]},
                               meta["video"], meta["duration"], saved["gps_time"], saved["gps_utc"])
            if result.time.ndim != 1 or result.gps_time.ndim != 1 or result.gps_utc.shape != result.gps_time.shape:
                return None
            if any(v.shape != (len(result.time), 4 if k in QUATERNIONS else 3)
                   for k, v in result.channels.items()):
                return None
            validate_telemetry(result)
            return result
    except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile):
        return None


def _save_cache(path: Path, result: Telemetry) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".npz", delete=False) as handle:
            temporary = handle.name
            metadata = {"version": CACHE_VERSION, "channels": list(result.channels),
                        "video": result.video, "duration": result.duration}
            np.savez_compressed(handle, metadata=json.dumps(metadata), time=result.time,
                                gps_time=result.gps_time, gps_utc=result.gps_utc, **result.channels)
        os.replace(temporary, path)
    except OSError:
        # A read-only cache must not make extraction fail.
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class GoProSource:
    """Extract one MP4 recording with file-local sample times."""

    def read(self, path: str | Path, *, hz: float = 10,
             cache_dir: str | Path | None = None) -> Telemetry:
        hz = float(hz)
        if not math.isfinite(hz) or hz <= 0:
            raise ValueError("hz must be finite and positive")
        if not isinstance(path, (str, Path)):
            raise ValueError("Supply one video path")
        path = Path(path).expanduser().resolve()
        directory = Path(cache_dir).expanduser() if cache_dir is not None else Path.home() / ".cache" / "gps-video-sync"
        cache = _cache_path(path, hz, directory)
        saved = _load_cache(cache)
        if saved is not None and saved.video == str(path) and saved.duration is not None:
            return saved
        local = _recording(path)
        if not local.packets:
            raise ValueError(f"No GoPro telemetry (gpmd) track found in: {path.name}")
        grid = np.arange(math.ceil(local.duration * hz)) / hz
        grid = grid[grid < local.duration]
        channels = {}
        for key in KEYS:
            values = _resample(local.motion[key], grid, key, hz)
            if values is not None:
                channels[key] = values
        if local.gps:
            pairs = np.concatenate([np.column_stack(pair) for pair in local.gps])
            pairs = pairs[np.argsort(pairs[:, 0], kind="stable")]
            pairs = pairs[np.r_[True, np.diff(pairs[:, 0]) > 0]]
            gps_time, gps_utc = pairs.T
        else:
            gps_time, gps_utc = np.empty(0), np.empty(0)
        result = Telemetry(grid, channels, str(path), local.duration, gps_time, gps_utc)
        _save_cache(cache, result)
        return result
