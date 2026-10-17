"""Optional raw-signal alignment plot and chapter-aware frame extraction."""
from pathlib import Path
import subprocess

import numpy as np


def _chapter_frame(telemetry, videos, seconds):
    if not np.isfinite(seconds) or seconds < 0:
        raise ValueError("Frame times must be finite, nonnegative video seconds")
    chapters = telemetry.chapters
    if not chapters:
        if len(videos) != 1 or str(videos[0]).lower().endswith(".npz"):
            raise ValueError("Frame extraction requires chapter paths and timeline bounds")
        if len(telemetry.time) and seconds > telemetry.time[-1]:
            raise ValueError(f"Frame time {seconds} is outside the video timeline")
        return videos[0], seconds
    for index, chapter in enumerate(chapters):
        start = chapter.get("start_seconds", chapter.get("start", chapter.get("video_start")))
        end = chapter.get("end_seconds", chapter.get("end", chapter.get("video_end")))
        if start is None:
            raise ValueError("Chapter metadata needs start_seconds or start for frame extraction")
        if end is None and "duration" in chapter:
            end = float(start) + float(chapter["duration"])
        if end is None:
            raise ValueError("Chapter metadata needs end_seconds, end, or duration")
        if float(start) <= seconds < float(end):
            path = chapter.get("path", chapter.get("file"))
            if path is None and index < len(videos) and not str(videos[index]).lower().endswith(".npz"):
                path = videos[index]
            if not path:
                raise ValueError("Chapter metadata needs a video path for frame extraction")
            return path, seconds - float(start)
    raise ValueError(f"Frame time {seconds} is outside the chapter timeline")


def plot_verification(track, telemetry, result, output, frames=(), videos=()):
    """Plot raw signals, not matcher scores. Frames are separate sibling PNGs."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("Verification plots require: pip install 'igc-video-sync[verify]'") from exc
    if not isinstance(result, dict) or result.get("schema_version") != "1.0.0":
        raise ValueError("Expected a schema_version 1.0.0 sync result")
    offset = result.get("offset_seconds")
    if offset is None:
        offset = result.get("videoStartUtcSeconds")
    if offset is None:
        raise ValueError("Result has no alignment offset; cannot draw a verification plot")
    rate = float(result.get("clockRate", result.get("drift", {}).get("clock_rate", 1.0)))
    offset = float(offset)
    if not np.isfinite(offset) or not np.isfinite(rate) or rate <= 0:
        raise ValueError("Result offset and clock rate must be finite; clock rate must be positive")
    output = Path(output)
    if output.suffix.lower() != ".png":
        raise ValueError("--output must end in .png")
    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(12, 7))
    try:
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
        axes[1].set_xlabel("UTC hours from track date midnight (may exceed 24)")
        if axes[1].lines:
            axes[1].legend()
        for ax in axes:
            ax.grid(alpha=0.3)
        fig.suptitle(f"Raw alignment check — {result.get('status', 'unknown')}; offset {offset:.3f} s")
        fig.tight_layout()
        fig.savefig(output)
    finally:
        plt.close(fig)
    for index, seconds in enumerate(frames):
        path, local_time = _chapter_frame(telemetry, videos, seconds)
        destination = output.with_name(f"{output.stem}-frame-{index:03d}-{seconds:g}s.png")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(local_time),
                        "-i", str(path), "-frames:v", "1", str(destination)],
                       check=True, capture_output=True, text=True)
        if not destination.is_file():
            raise RuntimeError(f"ffmpeg produced no frame at video time {seconds}")
