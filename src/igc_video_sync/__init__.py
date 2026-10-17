"""Action camera to GPS track synchronization."""
from .models import Track, Telemetry, SyncResult
from .plugins import (TrackReader, VideoSource, register_track, register_video,
                      read_track, read_video)
from .igc import IGCReader

__version__ = "0.1.0"
register_track("igc", IGCReader())
# GoPro registration is loaded when its implementation is installed.
try:
    from .gopro import GoProSource
except ImportError:
    pass
else:
    register_video("gopro", GoProSource())

def synchronize(track: Track, telemetry: Telemetry) -> SyncResult:
    from .sync import synchronize as run
    return run(track, telemetry)
