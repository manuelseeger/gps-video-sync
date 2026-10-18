from pathlib import Path
import math
from typing import Protocol
from .models import Track, Telemetry

class TrackReader(Protocol):
    def read(self, path: str | Path, *, altitude: str = "auto") -> Track: ...

class VideoSource(Protocol):
    def read(self, path: str | Path, *, hz: float = 10,
             cache_dir: str | Path | None = None) -> Telemetry: ...

TRACK_READERS: dict[str, TrackReader] = {}
VIDEO_SOURCES: dict[str, VideoSource] = {}

def register_track(name: str, reader: TrackReader) -> None:
    TRACK_READERS[name] = reader

def register_video(name: str, source: VideoSource) -> None:
    VIDEO_SOURCES[name] = source

def read_track(path: str | Path, *, format: str = "igc", altitude: str = "auto") -> Track:
    return TRACK_READERS[format].read(path, altitude=altitude)

def read_video(path: str | Path, *, source: str = "gopro", hz: float = 10,
               cache_dir: str | Path | None = None) -> Telemetry:
    if not isinstance(path, (str, Path)):
        raise ValueError("Supply one video path")
    if not math.isfinite(hz) or hz <= 0:
        raise ValueError("hz must be finite and positive")
    if source not in VIDEO_SOURCES:
        raise ValueError(f"Unknown or unavailable video source: {source}")
    return VIDEO_SOURCES[source].read(path, hz=hz, cache_dir=cache_dir)
