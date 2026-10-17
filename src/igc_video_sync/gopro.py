"""GoPro motion and GPS UTC on the concatenated video timeline.

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
from typing import Sequence
import zipfile

import numpy as np
from scipy.ndimage import uniform_filter1d

from . import mp4
from .gpmf import number, scaled, streams
from .models import Telemetry

KEYS = ("ACCL", "GYRO", "GRAV", "CORI", "IORI")
QUATERNIONS = ("CORI", "IORI")
CACHE_VERSION = "1.1.0"
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


def _chapter(path: Path, base: float) -> tuple[dict, dict, list, list]:
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
                    t = base + pts + np.arange(len(values)) * interval / len(values)
                    mask = (t >= base) & (t < base + duration)
                    data[key].append(np.column_stack((t[mask], values[mask])))
                for key, target in (("GPS9", packet_gps9), ("GPS5", packet_gps5)):
                    if key not in fields:
                        continue
                    has_gps9 |= key == "GPS9"
                    count = fields[key].count
                    if not count:
                        continue
                    t = base + pts + np.arange(count) * interval / count
                    gt, utc = _gps(fields, t, key)
                    mask = (gt >= base) & (gt < base + duration)
                    if mask.any():
                        target.append((gt[mask], utc[mask]))
            # Do not replace rejected GPS9 fixes with a second GPS5 observation.
            if has_gps9:
                gps9.extend(packet_gps9)
            else:
                gps5.extend(packet_gps5)
    chapter = {"file": str(path), "start": base, "duration": duration, "packets": len(timeline.samples)}
    return chapter, data, gps5, gps9


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


def _cache_path(paths: list[Path], hz: float, directory: Path) -> Path:
    identity = {"version": CACHE_VERSION, "hz": hz, "files": [
        [str(p), p.stat().st_size, p.stat().st_mtime_ns] for p in paths]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return directory / f"gopro-{digest}.npz"


def _load_cache(path: Path) -> Telemetry | None:
    try:
        with np.load(path, allow_pickle=False) as saved:
            meta = json.loads(str(saved["metadata"].item()))
            if meta["version"] != CACHE_VERSION or any(k not in KEYS for k in meta["channels"]):
                return None
            result = Telemetry(saved["time"], {k: saved[k] for k in meta["channels"]},
                               meta["chapters"], saved["gps_time"], saved["gps_utc"])
            if result.time.ndim != 1 or result.gps_time.ndim != 1 or result.gps_utc.shape != result.gps_time.shape:
                return None
            if any(v.shape != (len(result.time), 4 if k in QUATERNIONS else 3)
                   for k, v in result.channels.items()):
                return None
            return result
    except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile):
        return None


def _save_cache(path: Path, result: Telemetry) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".npz", delete=False) as handle:
            temporary = handle.name
            metadata = {"version": CACHE_VERSION, "channels": list(result.channels), "chapters": result.chapters}
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
    """Read ordered MP4 chapters; absent motion channels are omitted.

    Channels have shape (samples, 3), or (samples, 4) for native quaternions.
    A channel absent in one chapter has NaN rows in that chapter. GPS arrays
    retain valid native GPS fixes, independent of the motion sampling rate.
    """

    def read(self, paths: Sequence[str | Path], *, hz: float = 10,
             cache_dir: str | Path | None = None) -> Telemetry:
        hz = float(hz)
        if not math.isfinite(hz) or hz <= 0:
            raise ValueError("hz must be finite and positive")
        if isinstance(paths, (str, Path)) or not paths:
            raise ValueError("Supply a nonempty sequence of chapter paths")
        files = [Path(p).expanduser().resolve() for p in paths]
        directory = Path(cache_dir).expanduser() if cache_dir is not None else Path.home() / ".cache" / "igc-video-sync"
        cache = _cache_path(files, hz, directory)
        saved = _load_cache(cache)
        if saved is not None:
            return saved
        chapters, raw_chapters, gps = [], [], []
        base = 0.0
        for path in files:
            chapter, raw, gps5, gps9 = _chapter(path, base)
            chapters.append(chapter)
            raw_chapters.append(raw)
            gps.extend(gps5 + gps9)
            base += chapter["duration"]
        grid = np.arange(0, base, 1 / hz)
        channels = {}
        for chapter, raw in zip(chapters, raw_chapters):
            mask = (grid >= chapter["start"]) & (grid < chapter["start"] + chapter["duration"])
            for key in KEYS:
                values = _resample(raw[key], grid[mask], key, hz)
                if values is None:
                    continue
                if key not in channels:
                    channels[key] = np.full((len(grid), values.shape[1]), np.nan)
                channels[key][mask] = values
        # Keep quaternion signs continuous across chapter boundaries, too.
        for key in QUATERNIONS:
            if key in channels:
                mask = np.isfinite(channels[key]).all(axis=1)
                q = channels[key][mask]
                flip = np.r_[1, np.cumprod(np.where(np.sum(q[1:] * q[:-1], axis=1) < 0, -1, 1))]
                channels[key][mask] = q * flip[:, None]
        if gps:
            pairs = np.concatenate([np.column_stack(pair) for pair in gps])
            pairs = pairs[np.argsort(pairs[:, 0], kind="stable")]
            pairs = pairs[np.r_[True, np.diff(pairs[:, 0]) > 0]]
            gps_time, gps_utc = pairs.T
        else:
            gps_time, gps_utc = np.empty(0), np.empty(0)
        result = Telemetry(grid, channels, chapters, gps_time, gps_utc)
        _save_cache(cache, result)
        return result
