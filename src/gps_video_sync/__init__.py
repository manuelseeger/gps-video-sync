from .models import Track, Telemetry, SyncResult
from .plugins import (TrackReader, VideoSource, register_track, register_video,
                      read_track, read_video)
from .igc import IGCReader

__version__ = "0.1.0"
register_track("igc", IGCReader())
from .gopro import GoProSource
register_video("gopro", GoProSource())
from .npz import NPZSource
register_video("npz", NPZSource())

def synchronize(track: Track, telemetry: Telemetry) -> SyncResult:
    from .sync import synchronize as run
    return run(track, telemetry)


def match_videos(track: Track, paths, *, source: str = "gopro", hz: float = 10,
                 cache_dir=None) -> list[SyncResult]:
    """Match each recording independently, retaining input order and failures."""
    import math
    from pathlib import Path
    import zipfile
    from .plugins import VIDEO_SOURCES
    from .sync import validate_track, validate_telemetry, _synchronize

    validate_track(track)
    if source not in VIDEO_SOURCES:
        raise ValueError(f"Unknown or unavailable video source: {source}")
    if not math.isfinite(hz) or hz <= 0:
        raise ValueError("hz must be finite and positive")
    if isinstance(paths, (str, Path)):
        raise ValueError("Supply a nonempty sequence of video paths")
    paths = list(paths)
    if not paths or any(not isinstance(p, (str, Path)) for p in paths):
        raise ValueError("Supply a nonempty sequence of video paths")
    results = []
    seen_paths = set()
    accepted_timelines = set()
    for path in paths:
        identity = str(path)
        try:
            try:
                identity = str(Path(path).expanduser().resolve())
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
            if identity in seen_paths:
                raise ValueError("Duplicate input path")
            seen_paths.add(identity)
            telemetry = read_video(path, source=source, hz=hz, cache_dir=cache_dir)
            validate_telemetry(telemetry)
            timeline = (telemetry.video, telemetry.media_offset_seconds, telemetry.duration)
            if timeline in accepted_timelines:
                raise ValueError("Duplicate recording timeline")
        except (OSError, ValueError, EOFError, zipfile.BadZipFile) as exc:
            result = SyncResult("failed", video=identity, message=str(exc), date=track.date)
        else:
            result = _synchronize(track, telemetry)
            if result.status != "failed":
                accepted_timelines.add(timeline)
        result.input_file = identity
        results.append(result)
    return results
