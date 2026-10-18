from pathlib import Path
from typing import Protocol, Sequence
from .models import Track, Telemetry

class TrackReader(Protocol):
    def read(self, path: str | Path, *, altitude: str = "auto") -> Track: ...

class VideoSource(Protocol):
    def read(self, paths: Sequence[str | Path], *, hz: float = 10,
             cache_dir: str | Path | None = None) -> Telemetry: ...

TRACK_READERS: dict[str, TrackReader] = {}
VIDEO_SOURCES: dict[str, VideoSource] = {}

def register_track(name: str, reader: TrackReader) -> None:
    TRACK_READERS[name] = reader

def register_video(name: str, source: VideoSource) -> None:
    VIDEO_SOURCES[name] = source

def read_track(path: str | Path, *, format: str = "igc", altitude: str = "auto") -> Track:
    return TRACK_READERS[format].read(path, altitude=altitude)

def read_video(paths: Sequence[str | Path], *, source: str = "gopro", hz: float = 10,
               cache_dir: str | Path | None = None) -> Telemetry:
    return VIDEO_SOURCES[source].read(paths, hz=hz, cache_dir=cache_dir)
