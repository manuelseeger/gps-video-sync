from pathlib import Path
import json

import numpy as np

from .models import Telemetry, RecordingClock
from .sync import validate_telemetry


class NPZSource:
    def read(self, path: str | Path, *, hz: float = 10, cache_dir=None) -> Telemetry:
        path = Path(path).expanduser().resolve()
        data = np.load(path, allow_pickle=False)
        if not isinstance(data, np.lib.npyio.NpzFile):
            raise ValueError(f"{path} is not an NPZ archive")
        with data:
            if "chapters" in data:
                raise ValueError("Legacy concatenated chapter metadata is not supported")
            if "schema_version" in data and str(data["schema_version"].item()) != "2.0.0":
                raise ValueError("Expected telemetry schema_version 2.0.0")
            if "time" not in data:
                raise ValueError("NPZ needs a time array")
            time = np.asarray(data["time"], dtype=float)
            reserved = {"time", "gps_time", "gps_utc", "recording", "schema_version"}
            channels = {k: np.asarray(data[k], dtype=float) for k in data.files if k not in reserved}
            metadata = json.loads(str(data["recording"].item())) if "recording" in data else {}
            if not isinstance(metadata, dict):
                raise ValueError("NPZ recording metadata must be an object")
            if "recording" in data and set(metadata) - {"clock"} != {"video", "duration", "media_offset_seconds"}:
                raise ValueError("NPZ recording needs video, duration and media_offset_seconds")
            result = Telemetry(time, channels, metadata.get("video", str(path)),
                               metadata.get("duration"),
                               np.asarray(data["gps_time"], dtype=float) if "gps_time" in data else np.empty(0),
                               np.asarray(data["gps_utc"], dtype=float) if "gps_utc" in data else np.empty(0),
                               metadata.get("media_offset_seconds", 0.),
                               RecordingClock.from_dict(metadata.get("clock"))
                               if metadata.get("media_offset_seconds", 0.) == 0 else None)
        validate_telemetry(result)
        return result
