# igc-video-sync

Python library and CLI to align action-camera telemetry with a GPS flight track.
Python 3.10 or later; core dependencies are NumPy and SciPy.

## Setup and tests

```sh
uv venv
uv pip install --python .venv/bin/python -e '.[dev,verify]'
.venv/bin/python -m pytest -m 'not integration'
IGC_VIDEO_SYNC_FIXTURES=/path/to/fixtures .venv/bin/python -m pytest -m integration -s
.venv/bin/python -m igc_video_sync --help
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
and chapter metadata. `telemetry --out` accepts `.npz` or `.json`. `sync` writes JSON to stdout;
`sync --json PATH` writes the versioned `SyncResult.to_dict()` result to a file,
including ambiguous results.
`verify` uses a saved `--result`, or runs synchronization if no result is supplied.
Its plot compares normalized track and video motion features on the mapped UTC
axis. The yaw sign is selected for the mount convention. Sources without standard
motion channels show altitude and raw telemetry instead. The plot is a visual
check, not a confidence score. Frame PNGs are saved beside the plot
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
failure (including command syntax errors). Expected input and external-tool
errors are printed to stderr without a Python traceback.

### Portable synthetic telemetry

A pickle-free NPZ lets tests and other tools use the CLI without camera files:

```python
import numpy as np
np.savez("input.npz", time=np.arange(100) / 10,
         GYRO=np.zeros((100, 3)), ACCL=np.tile([0., 0., 9.81], (100, 1))))
```

```sh
uv run python -m igc_video_sync info input.npz --source npz
uv run python -m igc_video_sync sync input.npz --source npz --track flight.igc --json sync.json
```

NPZ fields are `time` (finite, strictly increasing video seconds), channel arrays
under their channel names, optional `gps_time` and `gps_utc` (matching 1-D arrays),
and optional `chapters` (a scalar JSON string containing a list of objects).
Each channel has one row per time sample. `schema_version` is reserved metadata;
exports use `1.0.0`. NPZ loading disables pickle. JSON telemetry exports use
`null` for missing/non-finite samples and contain `schema_version`, `time`,
`channels`, `gps_time`, `gps_utc`, and `chapters`; they
are output interchange data, not a CLI input adapter. Synthetic zero-motion data
is useful for format tests, not evidence of a valid synchronization.

## GoPro telemetry

LRV and MP4 chapters use the actual video-stream durations. The extractor locates
metadata by the `gpmd` codec tag, not by a fixed stream index. One `ffprobe` call
per chapter returns stream metadata and packet locations; GPMF bytes are then read
directly from the video. Big-endian values, 4-byte record padding, scalar/vector
`SCAL`, and complex `TYPE` records are decoded.

Available channels are `GYRO` (rad/s), `ACCL` (m/s²), `GRAV` (native unit vector),
and `CORI`/`IORI` (native unit quaternions). Vector data is averaged before
resampling. Quaternions are sign-continuous, interpolated, and normalized. Missing
channels are omitted; unavailable chapter data and sensor gaps are NaN. Sample
placement is uniform within each packet's actual PTS/duration. It is suitable
for second-level alignment, not exact sensor/exposure timing. GYRO and ACCL
retain native ZXY axes; GRAV has a different native frame. The yaw proxy uses
low-pass ACCL in the same axes as GYRO, not a direct dot product with GRAV.

GPS9 UTC days/seconds, or GPS5 plus GPSU UTC timestamps, provide the primary
alignment. GPS9 is preferred when both appear. Only 3D fixes are used. Motion
matching is used if GPS time is unavailable or inconsistent with the track.
File creation dates and the sensor STMP clock are not used as UTC anchors.

The default cache is `~/.cache/igc-video-sync`. The key includes the ordered
absolute chapter paths, sizes, nanosecond modification times, sample rate, and
extractor cache version. Cached NPZ files include all channels, GPS observations,
and chapter metadata. Writes are atomic; an unavailable cache does not prevent
extraction. Source media is opened read-only. Cached telemetry can contain GPS
coordinates/timing; treat it as private data. Deleting cache files is safe.

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
registers the IGC reader and GoPro source. A custom
source owns extraction, resampling, chapter concatenation, and cache behavior;
it must supply coherent time and channel arrays. A custom track reader owns UTC
unwrapping and altitude selection. The matcher consumes the data contracts rather
than a specific file format. Camera plugins must supply canonical uppercase GYRO and
ACCL channel names, with rad/s and m/s² units, for motion matching. GPS-only
sources can supply GPS time observations without motion channels. Standard
matcher feature names are `yaw`,
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
when a track date and offset exist; `videoStartUtc` gives its clock time.
`dateUtc` and `utc_origin_date` are the track date (the mapping's UTC origin),
which can differ from the video-start calendar date across midnight. `clockDriftDetected`, `clockRate`, and
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

The minimum usable motion overlap is 90 seconds. Acceptance requires a best
weighted score of at least 0.38, a margin of at least 0.08, and a best/alternative
ratio of at least 1.2. Alternative offsets exclude a 25-second neighborhood of
the best peak. At least two features must agree within 8 seconds, unless one
feature is exceptionally strong and unique. These are conservative heuristics,
not calibrated probabilities. `confidence.candidate_offset_seconds` is diagnostic;
never use it as an alignment when status is `ambiguous`.

Search uses masked, normalized FFT correlations over the full track, followed
by a local 0.1-second refinement. Search grid precision is not absolute accuracy;
accepted motion results have a 2-second systematic allowance. Drift diagnostics
fit non-overlapping 400-second windows. Detection requires at least four usable
windows, a slope greater than three standard errors, and over 0.5 seconds of
accumulated change. `clockRate` remains 1 when drift is not detected; inspect
`measured_clock_rate`, seconds/hour, and stderr separately. Short clips generally
cannot constrain drift. No drift detection does not prove a perfect clock.

IGC interpolation leaves gaps larger than the greater of 5 seconds and three
median fix intervals unfilled. Filtering operates on separate valid runs. GPS
course below 3 m/s is excluded. Sampling slower than motion features reduces
accuracy. Native GoPro vector axes and quaternion frames are not geographic
heading; AR overlays need mount and coordinate-frame calibration.

## Fixture validation

The integration suite uses external, read-only fixtures from
`IGC_VIDEO_SYNC_FIXTURES` (default `~/dev/fliew/fixtures`). It skips absent media
and ignores incomplete chapter transfers. No large media is packaged. The suite passes on Python 3.10.22 and 3.15.0:
65 unit/CLI tests and 11 integration tests.

Reference flight: 2026-10-16. Three complete chapters have durations 2114.112,
2114.112, and 115.081633 seconds. The 10 Hz export has 43,434 samples, all five
motion/orientation channels, and no camera GPS observations.

| Input | Accepted | Offset / error against reference |
| --- | --- | --- |
| Full three-chapter recording | yes | 34345.9 s; reference 34346 ±2 s |
| First two chapters | yes | 34345.9 s |
| 10-minute crops, starts 300–3600 s every 300 s | 12/12 | maximum absolute error 0.2 s |
| 3-minute crops, starts 300–3900 s every 300 s | 13/13 | maximum absolute error 0.2 s |
| 30-second crops, same 13 starts | 0/13 | all ambiguous; no offset returned |

The full recording's weighted score is 0.835; best alternative 0.304, ratio
2.74. Yaw correlation is 0.960 (absolute); vertical correlation is 0.817. Window
regression measures −0.213 ±0.053 s/hour (one standard error). The accumulated
change is below the detector's 0.5-second threshold, so no material clock drift
is reported and `clockRate` is 1. These numbers describe this fixture, not general
accuracy. Reported absolute uncertainty remains 2 seconds, not the finer search
grid or crop residuals. CLI sync, telemetry export, info, plots, and cross-chapter
frame extraction are exercised with the real recording.

Only one real flight and mount type is available. Other flights/mounts must be
validated before motion matching is considered stable. Camera GPS parsing is
covered by synthetic tests, not a real GPS-enabled recording. No viewer files
are changed; the JSON output supplies its sync-input keys.

## License

License selection requires the repository owner's decision. No license grant is
included.
