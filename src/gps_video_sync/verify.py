from pathlib import Path
import subprocess

import numpy as np


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not np.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _local_video(path):
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"Frame extraction needs a local video file: {path}")
    return path.resolve()


def _seconds(value):
    text = f"{value:.6f}"
    return "0.000000" if text == "-0.000000" else text


def _timeline(result):
    if "media_offset_seconds" not in result or "duration" not in result:
        raise ValueError("Result needs media_offset_seconds and duration timeline metadata")
    offset, duration = result["media_offset_seconds"], result["duration"]
    if offset is None:
        if result.get("status") != "failed" or duration is not None:
            raise ValueError("Only failed results can have an unknown timeline")
        return None, None
    offset = _number(offset, "Result media offset")
    if offset < 0:
        raise ValueError("Result media offset must be nonnegative")
    if duration is not None:
        duration = _number(duration, "Result duration")
        if duration <= 0:
            raise ValueError("Result duration must be positive")
    return offset, duration


def select_result(document, telemetry):
    if not isinstance(document, dict) or document.get("schema_version") != "3.0.0":
        raise ValueError("Expected a schema_version 3.0.0 sync document")
    entries = document.get("videos")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Sync document needs a nonempty videos list")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("schema_version") != "3.0.0":
            raise ValueError("Malformed saved-result entry")
        video = entry.get("video")
        if not isinstance(video, str) or not video:
            raise ValueError("Saved-result recording identities must be nonempty")
        if entry.get("status") not in ("ok", "ambiguous", "failed"):
            raise ValueError("Invalid saved-result status")
        _timeline(entry)
        for name in ("confidence", "per_feature", "drift"):
            if entry.get(name) is not None and not isinstance(entry[name], dict):
                raise ValueError(f"Result {name} must be an object")
        if entry.get("offset_seconds") is not None:
            _number(entry["offset_seconds"], "Result offset")
        if entry.get("uncertainty_seconds") is not None:
            if _number(entry["uncertainty_seconds"], "Result uncertainty") < 0:
                raise ValueError("Result uncertainty must be nonnegative")
        if entry["status"] == "ok":
            _alignment(entry)
    current = (telemetry.media_offset_seconds, telemetry.duration)
    matches = [entry for entry in entries
               if entry["video"] == telemetry.video and _timeline(entry) == current]
    matches = [entry for entry in matches if entry["status"] != "failed"] or matches
    if len(matches) != 1:
        raise ValueError(f"Saved result has no unique entry for recording timeline {telemetry.video}")
    return matches[0]


def _recording_frame(telemetry, seconds):
    if not np.isfinite(seconds) or seconds < 0:
        raise ValueError("Frame times must be finite, nonnegative video seconds")
    if telemetry.duration is None or seconds >= telemetry.duration:
        raise ValueError(f"Frame time {seconds} is outside the recording duration")
    if not telemetry.video or Path(telemetry.video).suffix.lower() == ".npz":
        raise ValueError("Frame extraction requires a local recording path")
    return _local_video(telemetry.video), seconds + telemetry.media_offset_seconds


def _alignment(result):
    offset = result.get("offset_seconds")
    if offset is None:
        offset = result.get("videoStartUtcSeconds")
    if offset is None:
        raise ValueError("Result has no alignment offset; cannot draw a verification plot")
    rate = result.get("clockRate")
    if rate is None:
        drift = result.get("drift")
        if drift is not None and not isinstance(drift, dict):
            raise ValueError("Result drift must be an object")
        rate = (drift or {}).get("clock_rate")
    rate = 1.0 if rate is None else _number(rate, "Result clock rate")
    offset = _number(offset, "Result offset")
    if rate <= 0:
        raise ValueError("Result clock rate must be positive")
    return offset, rate


def plot_verification(track, telemetry, result, output, frames=()):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("Verification plots require: pip install 'gps-video-sync[verify]'") from exc
    if not isinstance(result, dict) or result.get("schema_version") != "3.0.0":
        raise ValueError("Expected a schema_version 3.0.0 sync result")
    if result.get("video") != telemetry.video:
        raise ValueError("Result recording identity does not match current telemetry")
    if _timeline(result) != (telemetry.media_offset_seconds, telemetry.duration):
        raise ValueError("Result local timeline does not match current telemetry")
    offset, rate = _alignment(result)
    requests = [_recording_frame(telemetry, seconds) for seconds in frames]
    output = Path(output)
    if output.suffix.lower() != ".png":
        raise ValueError("--output must end in .png")
    from .sync import build_features
    common = []
    if "ACCL" in telemetry.channels or "GYRO" in telemetry.channels:
        utc, time, track_features, video_features = build_features(track, telemetry)
        common = sorted(track_features.keys() & video_features.keys())
    fig, axes = plt.subplots(max(2, len(common)), 1, sharex=True, figsize=(12, 3*max(2, len(common))))
    try:
        if common:
            def normalized(a):
                finite = np.isfinite(a)
                if not finite.any():
                    return a
                return (a-np.nanmean(a))/max(float(np.nanstd(a)), 1e-12)
            for ax, key in zip(axes, common):
                a, b = normalized(track_features[key]), normalized(video_features[key])
                mapped_time = time*rate+offset
                overlap = np.isfinite(a) & (utc >= mapped_time[0]) & (utc <= mapped_time[-1])
                if key == "yaw" and overlap.sum() > 2:
                    vb = np.interp(utc[overlap], mapped_time, b)
                    good = np.isfinite(vb)
                    if good.sum() > 2 and np.sum(a[overlap][good]*vb[good]) < 0:
                        b = -b
                ax.plot(utc/3600, a, label=f"track {key}")
                ax.plot(mapped_time/3600, b, alpha=.7, label=f"video {key}")
                ax.set_ylabel("Standard deviations")
                ax.legend()
        else:
            axes[0].plot(track.time / 3600, track.altitude, label=f"Track altitude ({track.altitude_source})")
            axes[0].set_ylabel("Altitude (m)")
            axes[0].legend()
            mapped = (telemetry.time * rate + offset) / 3600
            for name, values in telemetry.channels.items():
                values = np.asarray(values)
                if values.ndim == 1:
                    axes[1].plot(mapped, values, label=name)
                elif values.ndim == 2:
                    axes[1].plot(mapped, np.linalg.norm(values, axis=1), label=f"{name} norm")
            axes[1].set_ylabel("Raw telemetry (source units)")
            if axes[1].lines:
                axes[1].legend()
        axes[-1].set_xlabel("UTC hours from track date midnight (may exceed 24)")
        for ax in axes:
            ax.grid(alpha=0.3)
        fig.suptitle(f"{telemetry.video or 'Array telemetry'} | {result.get('status', 'unknown')} | offset {offset:.3f} s")
        fig.tight_layout()
        fig.savefig(output)
    finally:
        plt.close(fig)
    for index, (seconds, (path, local_time)) in enumerate(zip(frames, requests)):
        destination = output.with_name(f"{output.stem}-{path.stem}-frame-{index:03d}-{_seconds(seconds)}s.png")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", _seconds(local_time),
                        "-protocol_whitelist", "file", "-i", f"file:{path}",
                        "-frames:v", "1", str(destination)],
                       check=True, capture_output=True, text=True)
        if not destination.is_file():
            raise RuntimeError(f"ffmpeg produced no frame at video time {seconds}")
