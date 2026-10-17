# API and format reference

## Data contracts

`read_track(path, format="igc", altitude="auto")` returns a `Track`.
`read_video(paths, source="gopro", hz=10, cache_dir=None)` returns `Telemetry`.
`synchronize(track, telemetry)` returns a `SyncResult`.

- `Track(time, latitude, longitude, altitude, date=None, altitude_source="gps")`
  contains NumPy arrays. Time increases in UTC seconds from the track date's
  midnight. Coordinates are in degrees. Altitude is in metres.
- `Telemetry(time, channels, chapters=[], gps_time=[], gps_utc=[])` contains
  increasing video seconds and a mapping of channel names to NumPy arrays.
  Each channel has one row per time sample. `gps_time` contains video seconds
  for camera GPS observations. `gps_utc` contains the corresponding Unix UTC seconds.
- `Telemetry.slice(start, end)` selects a half-open interval, rebases video and
  GPS times to zero, and clears chapter metadata. A slice's offset refers to
  the slice's start.
- `SyncResult` contains status, offset, uncertainty, method, confidence,
  per-feature diagnostics, drift diagnostics, chapters, date, and message.
  `to_dict()` returns the JSON format with `schema_version: "1.0.0"`.

The result uses this mapping:

```text
utc_seconds = video_seconds * clockRate + offset_seconds
```

`videoStartUtcSeconds` aliases the offset. `video_start_utc` is an ISO timestamp
when a track date and offset exist. `videoStartUtc` is its clock time.
`dateUtc` and `utc_origin_date` identify the track date. A video start across
midnight can have a different calendar date.

`clockDriftDetected`, `clockRate`, and `estimatedAbsoluteUncertaintySeconds`
report drift and uncertainty. An ambiguous result has no offset.
`confidence.candidate_offset_seconds` is diagnostic, not an accepted alignment.

## Track parsing

The IGC reader excludes invalid fixes and unwraps a large reverse clock jump as
midnight rollover. Track times can exceed 86400 seconds. An offset without a
track date does not establish an absolute calendar timestamp.

`altitude="auto"` uses varying, nonzero pressure altitude when available and GPS
altitude otherwise. `"gps"` and `"pressure"` select the altitude explicitly.
Interpolation leaves gaps larger than the greater of five seconds and three
median fix intervals unfilled. GPS course below 3 m/s is excluded.

## GoPro extraction

The extractor accepts LRV and MP4 chapters in recording order. The native MP4
reader inspects `moov` sample tables without reading media payloads. It supports
`stts`, `ctts`, `stsz`, `stz2`, `stsc`, `stco`, and `co64`. The extractor reads
only the selected `gpmd` payloads at their validated positions inside `mdat`.
The source file does not change. Extraction needs no external programs.

Chapter duration uses the first positive value from the video edit-list total,
known `mdhd` duration, `stts` duration total, or known `mvhd` duration.
The edit-list total includes leading empty edits. All-ones header durations
mean unknown. Chapter offsets use these durations in recording order.

Sample times follow ISO edit mapping without a snap to packet boundaries:

```text
sample_time = leading_empty_seconds + (composition_ticks - media_time) / track_scale - video_origin
```

`ctts` version 0 offsets are unsigned. Version 1 offsets are signed.
The video origin is zero with a populated video edit list. Otherwise it is
the minimum video presentation time, or zero for an empty video table.
All tracks use that common origin.

The reader supports leading empty edits followed by one unit-rate media edit.
It rejects multiple media edits, trailing empty edits, dwell edits, other rates,
and fragmented MP4 files containing `moof` or `mvex`. GPMF edits that end before
the video timeline are unsupported. A partial GPMF sample across a leading
empty edit is also unsupported. Mixed sample descriptions and external media
references are rejected. Ordinary audio, `tmcd`, and `fdsc` tracks are ignored.
Self-contained URL and QuickTime alias data references are supported.

A partial sample keeps its complete duration and can have a negative start.
Observations retain their uniform positions in that original interval.
The extractor then selects observations in `[0, chapter_duration)`.
For example, a 1.001-second sample with media time 0.300 starts at -0.300.
Four observations occur at -0.300, -0.04975, 0.2005, and 0.45075 seconds.
Only the last two remain. This ISO rule places chapter-01 fixture GPMF samples
0.701 seconds later than FFmpeg's packet-boundary edit rule. The bundled
STMP camera-clock continuity test checks this rule across the chapter boundary.

Chapter metadata `packets` counts returned GPMF samples, including partial
boundary samples and samples outside the chapter observation interval.
Samples that end at or before the media edit starts are omitted.
The reader retains at most 64 MiB of metadata and visits at most 100000 boxes.
It permits at most 128 tracks, 1024 edits per selected track, and 10000000
samples across selected tracks. GPMF output is limited to 100000 samples with
at most 16 MiB per payload. A limit violation raises `ValueError`.

Available channels are `GYRO` in rad/s, `ACCL` in m/s², `GRAV` as native unit
vectors, and `CORI` and `IORI` as native unit quaternions. Missing channels are
omitted. Sensor gaps and unavailable chapter data are NaN.

Vector data is averaged before resampling. Quaternions are sign-continuous,
interpolated, and normalized. Samples are placed uniformly within each packet's
PTS and duration. These times support second-level alignment, not exact exposure
timing. GYRO and ACCL retain native ZXY axes. GRAV uses a different native frame.
The yaw proxy uses low-pass ACCL in the GYRO axes.

GPS9 UTC days and seconds, or GPS5 with GPSU timestamps, provide absolute time.
GPS9 takes priority. Only 3D fixes are used. File creation dates and the STMP
sensor clock are not UTC references.

The default cache is `~/.cache/gps-video-sync`. Keys include ordered absolute
paths, file sizes, nanosecond modification times, sample rate, and extractor
cache version. Writes are atomic. An unavailable cache does not stop extraction.
Cache files can contain GPS coordinates and times.

## Plugins

`register_track(name, reader)` registers a `TrackReader` instance with
`read(path, *, altitude="auto") -> Track`.
`register_video(name, source)` registers a `VideoSource` instance with
`read(paths, *, hz=10, cache_dir=None) -> Telemetry`.
Registrations can replace existing names. Package import registers `igc` and `gopro`.

A track reader owns UTC unwrapping and altitude selection. A video source owns
extraction, resampling, chapter concatenation, and caching. Motion matching uses
uppercase `GYRO` and `ACCL` channels with three components and the units above.
GPS-only sources can supply GPS observations without motion channels.
The standard motion features are `yaw`, `gyro_norm`, `vertical`, and `gload`.

Frame extraction accepts chapter `path` or `file`, start fields `start_seconds`,
`start`, or `video_start`, and end fields `end_seconds`, `end`, or `video_end`.
A `duration` can replace an end field when a start exists. Intervals are
half-open. A boundary selects the next chapter. A single video needs no chapter metadata.

## CLI and telemetry formats

`python -m gps_video_sync` and `gps-video-sync` expose the same commands.
Common options follow the command name.

- `--source NAME` selects a video source. The default is `gopro`.
- `--cache-dir PATH` passes a cache directory to the source plugin.
- `--hz NUMBER` sets a positive telemetry sample rate. The default is 10.
- `--track-format NAME` selects a track reader for `sync` and `verify`.
- `--altitude auto|gps|pressure` selects IGC altitude for `sync` and `verify`.

`info` writes JSON with sample bounds, channel shapes, GPS sample count, and
chapter metadata. `telemetry --out` accepts `.npz` or `.json`.
`sync` writes result JSON to stdout. `--json PATH` also saves the result,
including ambiguous results. Expected input and tool errors go to stderr without
a Python traceback.

`verify` reads `--result` or runs synchronization. Its plot compares normalized
track and video motion features on the mapped UTC axis. The plot is a visual
check, not a confidence score. Frame PNGs are saved beside the plot.
A result without an offset cannot be plotted.

`--source npz` is a CLI-only adapter for one pickle-free NPZ file. The fields are
`time`, channel arrays under their channel names, optional matching 1-D arrays
`gps_time` and `gps_utc`, and optional `chapters` as a scalar JSON string.
`time` must be finite and strictly increasing. The adapter retains stored sample
times and ignores the sample rate and cache options.

NPZ exports reserve `schema_version` and use `1.0.0`. JSON telemetry exports
contain `schema_version`, `time`, `channels`, `gps_time`, `gps_utc`, and `chapters`.
Missing or non-finite values become `null`. JSON telemetry is an output format,
not a CLI input format.

## Motion matching limits

Camera GPS UTC is the primary reference when usable. Motion matching is the
fallback when GPS time is absent or inconsistent with the track.
Filtering operates on separate valid runs. Search uses masked, normalized FFT
correlations, then local 0.1-second refinement.

The minimum usable overlap is 90 seconds. Acceptance requires a weighted score
of at least 0.38, a margin of at least 0.08, and a best-to-alternative ratio of at
least 1.2. Alternative offsets exclude a 25-second neighborhood around the best
peak. At least two features must agree within eight seconds unless one feature
is exceptionally strong and unique. These thresholds are heuristics, not
calibrated probabilities. Accepted motion results include a two-second
systematic uncertainty allowance.

Drift diagnostics fit non-overlapping 400-second windows. Detection requires at
least four usable windows, a slope above three standard errors, and accumulated
change above 0.5 seconds. `clockRate` stays at 1 when drift is not detected.
`measured_clock_rate`, seconds per hour, and standard error remain separate
diagnostics. No detected drift does not prove clock stability.

Repeated turns, low motion, sensor noise, mounting differences, altitude noise,
data gaps, and short overlap can weaken a match or produce a false match.
Native camera axes and quaternion frames are not geographic heading.
Motion matching has real-media coverage for one flight and mount type.
Camera GPS parsing has synthetic test coverage.
