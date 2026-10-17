# igc-video-sync

Python library and CLI to align action-camera telemetry with a GPS flight track.
Python 3.10 or later; core dependencies are NumPy and SciPy.

## Setup and tests

```sh
uv sync --extra dev --extra verify
uv run pytest
uv run python -m igc_video_sync --help
```

For a pip installation, use `pip install '.[dev,verify]'` in a virtual environment.
GoPro extraction needs `ffprobe` on `PATH`. Verification frame extraction also
needs `ffmpeg`. Install these external tools with your operating-system package
manager. Matplotlib is optional: install the `verify` extra for plots. NPZ input
and core matching do not need these external tools.

## CLI

Pass chapter files in recording order. All video times refer to the concatenated
video timeline, not an individual chapter. Put common options after the command.

```sh
uv run igc-video-sync info GOPR0001.MP4 GP010001.MP4
uv run igc-video-sync telemetry GOPR0001.MP4 GP010001.MP4 --out telemetry.npz --hz 10
uv run igc-video-sync sync GOPR0001.MP4 GP010001.MP4 --track flight.igc --json sync.json
uv run igc-video-sync verify GOPR0001.MP4 GP010001.MP4 --track flight.igc \
  --result sync.json --output alignment.png --frames 10 120
```

`python -m igc_video_sync` has the same commands. `--version` prints the package
version. `info` writes JSON with sample bounds, channel shapes, GPS sample count,
and chapter metadata. `telemetry --out` accepts `.npz` or `.json`. `sync --json`
writes the versioned `SyncResult.to_dict()` result, including ambiguous results.
`verify` uses a saved `--result`, or runs synchronization if no result is supplied.
Its plot shows track altitude and raw telemetry on the mapped UTC axis. Vector
channels appear as norms. The plot is a visual check, not a confidence score;
channels can have different physical units. Frame PNGs are saved beside the plot
as `alignment-frame-000-10s.png`, etc. Chapter-local seek times are calculated
from the concatenated timeline. Frames need a video file path and bounds for
each chapter. A result without an offset cannot be plotted.

Common options:

- `--source NAME`: registered video source; default `gopro`. `npz` is a CLI-only
  adapter for one exported NPZ file; it does not change the plugin registry.
- `--cache-dir PATH`: pass an explicit cache directory to the source plugin.
  Cache behavior and invalidation belong to the source. NPZ input ignores this
  option. Cache files contain telemetry; store them in a suitable local location.
- `--hz NUMBER`: positive telemetry sample rate; default 10. NPZ retains its
  stored sample times rather than resampling.
- `--track-format NAME`: track-reader registry key; default `igc` (sync/verify).
- `--altitude auto|gps|pressure`: IGC altitude selection (sync/verify). Auto uses
  varying, nonzero pressure altitude if available, otherwise GPS altitude.

Exit codes: **0** success, **2** ambiguous alignment, **1** input or external-tool
failure. Argparse also uses **2** for command syntax errors. Expected input and
external-tool errors are printed to stderr without a Python traceback.

### Portable synthetic telemetry

A pickle-free NPZ lets tests and other tools use the CLI without camera files:

```python
import numpy as np
np.savez("input.npz", time=np.arange(100) / 10,
         gyro=np.zeros((100, 3)), accel=np.zeros((100, 3)))
```

```sh
uv run python -m igc_video_sync info input.npz --source npz
uv run python -m igc_video_sync sync input.npz --source npz --track flight.igc --json sync.json
```

NPZ fields are `time` (finite, strictly increasing video seconds), channel arrays
under their channel names, optional `gps_time` and `gps_utc` (matching 1-D arrays),
and optional `chapters` (a scalar JSON string containing a list of objects).
Each channel has one row per time sample. `schema_version` is reserved metadata;
exports use `1.0.0`. NPZ loading disables pickle. JSON telemetry exports contain
`schema_version`, `time`, `channels`, `gps_time`, `gps_utc`, and `chapters`; they
are output interchange data, not a CLI input adapter. Synthetic zero-motion data
is useful for format tests, not evidence of a valid synchronization.

## Python API and plugin contracts

```python
from igc_video_sync import read_track, read_video, synchronize

track = read_track("flight.igc", format="igc", altitude="auto")
telemetry = read_video(["GOPR0001.MP4", "GP010001.MP4"], source="gopro", hz=10,
                       cache_dir=".telemetry-cache")
result = synchronize(track, telemetry)
stable_json_object = result.to_dict()
```

Public dataclasses:

- `Track(time, latitude, longitude, altitude, date=None, altitude_source="gps")`:
  NumPy arrays with increasing UTC seconds from the track date's midnight,
  geographic coordinates in degrees, and altitude in metres.
- `Telemetry(time, channels, chapters=[], gps_time=[], gps_utc=[])`: video seconds,
  a mapping of channel names to aligned NumPy arrays, and chapter metadata.
  Gyroscope and acceleration vector channels use rows of three components.
  `gps_time` holds video seconds for absolute GPS observations; `gps_utc` holds
  the corresponding Unix UTC seconds. Only actual camera GPS observations belong
  here, not file creation timestamps. `slice(start, end)` crops the half-open
  interval and rebases video and GPS observation times; it clears chapter metadata.
  An offset calculated for a slice refers to that slice's rebased start.
- `SyncResult`: status, offset and uncertainty in seconds, method, confidence,
  per-feature diagnostics, drift diagnostics, chapters, date, and message.
  Use `to_dict()` for JSON, not direct dataclass serialization.

Register an instance of a reader or source before use:

```python
from igc_video_sync import register_track, register_video

# reader.read(path, *, altitude="auto") -> Track
register_track("custom-track", reader)
# source.read(paths, *, hz=10, cache_dir=None) -> Telemetry
register_video("custom-camera", source)
```

The `TrackReader` and `VideoSource` protocols define these signatures. Registries
use explicit names and accept replacement registrations. Importing the package
registers the IGC reader and, when available, the GoPro implementation. A custom
source owns extraction, resampling, chapter concatenation, and cache behavior;
it must supply coherent time and channel arrays. A custom track reader owns UTC
unwrapping and altitude selection. The matcher consumes the data contracts rather
than a specific file format. Standard matcher feature names are `yaw`,
`gyro_norm`, `vertical`, and `gload`; custom channel layouts need compatible
feature extraction. CLI frame extraction accepts chapter `path`/`file`,
`start_seconds`/`start`/`video_start`, and `end_seconds`/`end`/`video_end` (or
`duration` with a start). Intervals are half-open; a boundary selects the next
chapter. A source without chapter metadata can extract frames from one video.

## Time mapping, confidence, and limits

Result JSON has `schema_version: "1.0.0"` and the mapping:

```text
utc_seconds = video_seconds * clockRate + offset_seconds
```

UTC seconds are measured from the track date's midnight, not the Unix epoch.
`videoStartUtcSeconds` aliases the offset. `video_start_utc` is an ISO timestamp
when a track date and offset exist; `videoStartUtc` and `dateUtc` give its time
and date components. `clockDriftDetected`, `clockRate`, and
`estimatedAbsoluteUncertaintySeconds` expose drift and uncertainty. Preserve the
full result, including `confidence`, `per_feature`, `drift`, `method`, and `message`.

The IGC reader excludes invalid fixes and unwraps a large reverse clock jump as
midnight rollover. Times can exceed 86400 seconds. Adding the offset to the track
date can produce a different calendar date. Without a track date, an offset does
not establish an absolute calendar timestamp. A video creation timestamp alone
is not a reliable UTC reference.

Camera GPS UTC observations are the primary absolute reference when usable.
Motion matching is the fallback: turns, angular motion, vertical motion, and load
variation can constrain an offset. Confidence and uncertainty describe the
available evidence; they do not guarantee a correct match. Review ambiguous
results rather than treating the strongest candidate as certain. Repeated turns,
straight flight, low motion, data gaps, sensor noise, different sensor mounting,
altitude noise, and short overlap can cause weak or false matches. Clock drift
can make a constant offset inadequate. Inspect drift diagnostics and the mapped
plot; verify distinctive events or extracted frames before downstream use.

No measured integration accuracy is asserted here. Unit tests use synthetic data;
camera-specific validation needs suitable real telemetry and an independent time
reference.

## License

License selection requires the repository owner's decision. No license grant is
included.
