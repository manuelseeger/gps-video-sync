from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Sequence
import zipfile

import numpy as np
from scipy.ndimage import uniform_filter1d

from .gpmf import number, scaled, streams
from .models import Telemetry

KEYS = ("ACCL", "GYRO", "GRAV", "CORI", "IORI")
QUATERNIONS = ("CORI", "IORI")
CACHE_VERSION = "1.0.1"
GPS_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()


def _probe(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_packets",
             "-show_entries",
             "stream=index,codec_type,codec_tag_string,codec_tag,duration,duration_ts,time_base,start_time:"
             "packet=stream_index,pts_time,dts_time,duration_time,pos,size:format=duration",
             "-show_format", "-of", "json", str(path)],
            check=True, capture_output=True, text=True,
        )
        meta = json.loads(result.stdout)
        if not isinstance(meta, dict):
            raise ValueError("ffprobe did not return an object")
        return meta
    except FileNotFoundError as exc:
        raise ValueError("GoPro extraction requires ffprobe") from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot probe video {path.name}: invalid or truncated file") from exc


def _float(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _pts(packet: dict) -> float | None:
    value = _float(packet.get("pts_time"))
    return value if value is not None else _float(packet.get("dts_time"))


def _video_timing(meta: dict) -> tuple[float, float]:
    video = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("GoPro chapter has no video stream")
    packets = [p for p in meta.get("packets", [])
               if p.get("stream_index") == video.get("index") and _pts(p) is not None]
    start = _float(video.get("start_time"))
    if start is None:
        start = min((_pts(p) for p in packets), default=0.0)
    duration = _float(video.get("duration"))
    if duration is None and "duration_ts" in video and "time_base" in video:
        try:
            numerator, denominator = map(float, video["time_base"].split("/"))
            duration = float(video["duration_ts"]) * numerator / denominator
        except (ValueError, TypeError, ZeroDivisionError):
            pass
    if duration is None or duration <= 0 or not math.isfinite(duration):
        ends = [_pts(p) + (_float(p.get("duration_time")) or 0.0) for p in packets]
        duration = max(ends, default=start) - start
        if duration <= 0:
            duration = _float(meta.get("format", {}).get("duration"))
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Cannot determine video chapter duration")
    return start, duration


def _is_gpmd(stream: dict) -> bool:
    if stream.get("codec_tag_string") == "gpmd":
        return True
    try:
        tag = stream.get("codec_tag", "")
        return (int(tag, 0) if isinstance(tag, str) else int(tag)) == 0x646D7067
    except (ValueError, TypeError):
        return False


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
    if "GPSF" in fields:
        fix = number(fields["GPSF"]).ravel()
        if len(fix) not in (1, len(values)):
            raise ValueError("GPSF does not match GPS samples")
        good &= fix >= 3
    good &= np.isfinite(values).all(axis=1) & np.isfinite(utc)
    good &= (np.abs(values[:, 0]) <= 90) & (np.abs(values[:, 1]) <= 180)
    return times[good], utc[good]


def _chapter(path: Path, base: float) -> tuple[dict, dict, list, list]:
    meta = _probe(path)
    start, duration = _video_timing(meta)
    indices = {s["index"] for s in meta.get("streams", []) if _is_gpmd(s) and "index" in s}
    packets = [p for p in meta.get("packets", []) if p.get("stream_index") in indices]
    if any(_pts(p) is None for p in packets):
        raise ValueError("GPMF packet has no timestamp")
    packets.sort(key=_pts)
    data = {key: [] for key in KEYS}
    gps5, gps9 = [], []
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        for i, packet in enumerate(packets):
            pts = _pts(packet) - start
            interval = _float(packet.get("duration_time"))
            if interval is None or interval <= 0:
                next_pts = _pts(packets[i + 1]) - start if i + 1 < len(packets) else duration
                interval = next_pts - pts
            if interval <= 0:
                raise ValueError("GPMF packet has no positive duration")
            try:
                pos, size = int(packet["pos"]), int(packet["size"])
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError("GPMF packet has no valid file position or size") from exc
            if pos < 0 or size <= 0 or pos + size > file_size:
                raise ValueError("Truncated GPMF packet in video file")
            handle.seek(pos)
            buf = handle.read(size)
            if len(buf) != size:
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
            if has_gps9:
                gps9.extend(packet_gps9)
            else:
                gps5.extend(packet_gps5)
    chapter = {"file": str(path), "start": base, "duration": duration, "packets": len(packets)}
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
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class GoProSource:

    def read(self, paths: Sequence[str | Path], *, hz: float = 10,
             cache_dir: str | Path | None = None) -> Telemetry:
        hz = float(hz)
        if not math.isfinite(hz) or hz <= 0:
            raise ValueError("hz must be finite and positive")
        if isinstance(paths, (str, Path)) or not paths:
            raise ValueError("Supply a nonempty sequence of chapter paths")
        files = [Path(p).expanduser().resolve() for p in paths]
        directory = Path(cache_dir).expanduser() if cache_dir is not None else Path.home() / ".cache" / "gps-video-sync"
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
