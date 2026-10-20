# API and format reference

## Data contracts

`read_track(path, *, format="igc", altitude="auto")` returns a `Track`.
`read_video(path, *, source="gopro", hz=10, cache_dir=None)` reads one recording
and returns a `Telemetry`. A value that is not one `str` or `Path`, for example
a list of paths, raises `ValueError`.
`synchronize(track, telemetry)` returns one `SyncResult`.
`match_videos(track, paths, *, source="gopro", hz=10, cache_dir=None)` returns a
list of file-local `SyncResult` values in input order.

- `Track(time, latitude, longitude, altitude, date=None, altitude_source="gps")`
  contains NumPy arrays. Time increases in UTC seconds from midnight of the
  track date. Coordinates are in degrees. Altitude is in metres.
- `Telemetry(time, channels, video=None, duration=None, gps_time=[], gps_utc=[], media_offset_seconds=0, clock=None)`
  contains increasing, nonnegative, file-local video seconds and NumPy channel
  arrays. `gps_time` and `gps_utc` default to empty arrays. Each channel has one
  row per time sample. `video` identifies the recording. The GoPro source uses
  its canonical absolute path. `duration` bounds the half-open recording
  interval `[0, duration)`. `gps_time` contains local video seconds for camera
  GPS observations. `gps_utc` contains the related Unix UTC seconds.
  `clock` is optional validated `RecordingClock` continuity evidence.
- `RecordingClock(origin_seconds, reach_seconds, sample_counters, camera=None)`
  contains a finite sensor-clock origin at local video time zero and a finite,
  nonnegative origin error bound. `sample_counters` maps stream names to exact
  integer `(start, end)` cumulative counter endpoints, with `0 <= start < end`.
  `camera` is an optional nonempty serial number. The sensor clock is not UTC.
- `Telemetry.slice(start, end)` selects the half-open interval `[start, end)`
  and subtracts `start` from video and GPS times. The bounds must be finite,
  with `0 <= start < end`. When the duration is known, `end` must not exceed it.
  The slice keeps the recording identity, sets the duration to `end - start`,
  and adds `start` to `media_offset_seconds`. Nested slices add their media
  offsets. Frame extraction adds this offset to slice-local seconds before it
  seeks the original recording. The synchronization offset of a slice refers
  to the start of the slice. Every slice clears `clock`, including a slice of
  the full duration. Counter endpoints cannot be reused for a crop.
- `SyncResult` contains the recording identity in `video`, status, offset,
  uncertainty, method, confidence, per-feature data, drift data, date, and
  message. `media_offset_seconds` and `duration` identify the local timeline
  used for matching. They copy the current telemetry values, also for
  ambiguous results and for failures with valid telemetry. The media offset is
  `null` when extraction or telemetry validation fails. The duration is `null`
  when unknown. Batch results also contain `input_file`, the canonical input
  path. For NPZ input, `video` identifies the original recording and
  `input_file` identifies the archive. `to_dict()` uses schema `2.0.0`.
  Direct array-based callers can omit recording identity and duration.
  Frame extraction needs both.

`synchronize` raises `ValueError` when the track or telemetry is not valid. It
returns a `failed` result when the telemetry has fewer than two samples.

### Batch matching and continuous chapters

`match_videos` checks the track, source, sample rate, and path sequence before
it reads a file. An invalid value, an empty sequence, or a single path in place
of a sequence raises `ValueError`.

Each file first matches the track independently. The function then detects
continuous chapters from `RecordingClock` evidence. An edge from A to B needs
all of these conditions:

- The absolute difference between B's clock origin and A's origin plus A's
  duration, plus both error bounds, is at most 0.05 seconds.
- At least one stream has cumulative counters in both files. Every shared
  stream has exactly equal A-end and B-start counters.
- Both durations are known, both media offsets are zero, and B's origin is
  later than A's origin. Available camera serials do not conflict.

Names and input order do not establish edges. A chain becomes a `Shot` with
chronologically ordered membership. A branch keeps each member independent
with ambiguous shot metadata. Conflicting serials anywhere in a chain also
veto grouping, including a chain with an intermediate file without a serial.
Missing evidence leaves a file independent. Sensor clocks and counters do not
identify a recording session. Files from separate cameras with coincident clocks
and counters can satisfy these checks. Check the verification plots before use.

A continuous shot concatenates local telemetry on cumulative file durations
and runs the existing single-recording matcher. A failed independent member
cannot be promoted. A group result that is not `ok` retains all independent
results. A GPS step reported on the shot but on no independent member also
retains them, even when the shot has an accepted motion fit. The projected
mapping must agree with every independent `ok` mapping at both ends of that
member, within the sum of the two uncertainty bounds. A contradiction retains
all independent results. Unexpected matcher errors propagate.

Each projected member keeps its recording identity, media offset, duration,
and date. Offsets use the left fold `O_next = O + clockRate * duration` with
no rounding, so each end equals the next start as a floating-point value.
Input permutations give the same result per recording. A subset of a
continuous shot can give a different result. Unrelated inputs retain their
independent results.

Every result has `shot.status`. `single` means independent matching without
a candidate group. `joined` means projection was applied. `ambiguous` means
a candidate group or branch retained independent results. Group metadata
also has `members` in chronological order, `index`, `start_seconds` on the
candidate shot timeline, and `reason`. Branch members have singleton metadata.
Joined results label `shot.diagnostics_coordinates` as `shot`. Their
`confidence`, `per_feature`, and `drift` diagnostics, including candidate
offsets, window midpoints, and drift intercepts, refer to the shot timeline.
The top-level offset and the identity fields remain file-local.

Expected extraction errors and invalid telemetry give a `failed` result for
that file. These errors are `OSError`, `ValueError`, `EOFError`, and
`zipfile.BadZipFile`. Other errors, for example programming errors, propagate.
Insufficient motion evidence gives an `ambiguous` result with a reason.

A repeated canonical input path fails before extraction. A symbolic link to an
earlier input is a repeated path. When a timeline has an `ok` or `ambiguous`
result, a later input with the same `video`, `media_offset_seconds`, and
`duration` gets a duplicate failure. A failed result does not reserve its
timeline. Duplicate failures use the canonical input path as `video` and have
`null` media offset and duration. Different crops of one recording are
separate timelines.

### Time mapping

The result uses this mapping:

```text
utc_seconds = video_seconds * clockRate + offset_seconds
```

`videoStartUtcSeconds` is an alias of the offset. `video_start_utc` is an ISO
timestamp when a track date and an offset exist. `videoStartUtc` is its clock
time. `utc_origin_date` is the track date. It is the origin of `offset_seconds`
and of all track times. `dateUtc` is the UTC calendar date of the video start.
The two dates differ when the video starts on a different calendar day. For
example, an offset of 86410 seconds gives `utc_origin_date` 2026-01-01 and
`dateUtc` 2026-01-02. Without a date, `dateUtc` and `utc_origin_date` are
`null`. A GPS result can get its date from camera GPS when the track has no
date.

`clockDriftDetected`, `clockRate`, and `estimatedAbsoluteUncertaintySeconds`
report drift and uncertainty. An ambiguous result has no offset.
`confidence.candidate_offset_seconds` is diagnostic data, not an accepted
alignment.

## Track parsing

The IGC reader uses the first `HFDTE` or `HODTE` date. It excludes invalid fixes
and fixes that do not increase in time. It unwraps a large reverse clock jump
as midnight rollover, so track times can exceed 86400 seconds. An offset
without a track date does not establish an absolute calendar timestamp.

`altitude="auto"` uses varying, nonzero pressure altitude when available and GPS
altitude otherwise. `"gps"` and `"pressure"` select the altitude explicitly.

Motion matching interpolates the track to whole seconds. It fills gaps only up
to five seconds or three median fix intervals, whichever is greater. It
excludes track features where the ground speed is below 3 m/s.

## GoPro extraction

The extractor reads one LRV or MP4 recording. The native MP4 reader inspects
`moov` sample tables without reading media payloads. It supports
`stts`, `ctts`, `stsz`, `stz2`, `stsc`, `stco`, and `co64`. The extractor reads
only the selected `gpmd` payloads at their validated positions inside `mdat`.
The source file does not change. Extraction needs no external programs.

The recording duration comes from the video edit-list total when the video
track has a populated edit list. Otherwise it is the first positive value from
the known `mdhd` duration, the `stts` duration total, or the known `mvhd`
duration. The edit-list total includes leading empty edits. All-ones header
durations mean unknown.
Observations and resampled arrays use the local timeline of that file.

Sample times follow ISO edit mapping without a snap to packet boundaries:

```text
sample_time = leading_empty_seconds + (composition_ticks - media_time) / track_scale - video_origin
```

GoPro resampling permits at most 1,000,000 grid samples per recording.
The source raises `ValueError` before grid allocation when `duration * hz` is
not finite or exceeds this limit. The limit also applies to cached telemetry.

A video with no `gpmd` samples raises `ValueError`.

`ctts` version 0 offsets are unsigned. Version 1 offsets are signed.
The video origin is zero with a populated video edit list. Otherwise it is
the minimum video presentation time, or zero for an empty video table.
All tracks use that common origin.

The reader supports leading empty edits followed by one unit-rate media edit.
It rejects multiple media edits, trailing empty edits, dwell edits, other rates,
and fragmented MP4 files that contain `moof` or `mvex`. It also rejects GPMF
edits that end before the video timeline, partial GPMF samples across a leading
empty edit, mixed sample descriptions, and external media references. It
ignores ordinary audio, `tmcd`, and `fdsc` tracks. It supports self-contained
URL and QuickTime alias data references.

A partial sample keeps its complete duration and can have a negative start.
Observations keep their uniform positions in that original interval.
The extractor then selects observations in `[0, recording_duration)`.
For example, a 1.001-second sample with media time 0.300 starts at -0.300.
Four observations occur at -0.300, -0.04975, 0.2005, and 0.45075 seconds.
Only the last two remain. This ISO rule places the GPMF samples of the
`chapter-01.mp4` fixture 0.701 seconds later than the FFmpeg packet-boundary
edit rule. Fixture sample-table tests check these positions.
The reader omits samples that end at or before the start of the media edit.

The reader keeps at most 64 MiB of metadata and visits at most 100000 boxes.
It permits at most 128 tracks, 1024 edits per selected track, and 10000000
samples across selected tracks. The reader returns at most 100000 GPMF samples,
with at most 16 MiB per payload. GPMF containers can nest at most eight levels.
A limit violation raises `ValueError`.

Available channels are `GYRO` in rad/s, `ACCL` in m/s², `GRAV` as native unit
vectors, and `CORI` and `IORI` as native unit quaternions. The extractor omits
missing channels. Sensor gaps are NaN.

The extractor averages vector data before resampling. It makes quaternions
sign-continuous, then interpolates and normalizes them. It places samples
uniformly within the PTS and duration of each packet. These times support
second-level alignment, not exact exposure timing. GYRO and ACCL keep the
native ZXY axes. GRAV uses a different native frame. The yaw proxy uses
low-pass ACCL in the GYRO axes.

GPS9 UTC days and seconds, or GPS5 with GPSU timestamps, supply absolute time.
GPS9 takes priority. The extractor uses only 3D fixes. File creation dates and
the STMP sensor clock are not UTC references.

### Continuity evidence

The reader extracts sensor `STMP` microseconds and cumulative integer `TSMP`
counters before resampling. For each motion stream, every packet needs both
fields. At least three packets are required. Timestamps must strictly increase,
and `STMP / 1e6 - packet_pts` must span at most 0.02 seconds. Counter increments
must equal the current packet's sample count. The endpoints are
`(first_TSMP - first_count, last_TSMP)`. Unsupported or incomplete optional
clock evidence clears the clock without removing ordinary telemetry.

The origin is the median of `STMP / 1e6 - packet_pts` in the first available
stream in `CORI`, `IORI`, `GRAV`, `ACCL`, `GYRO` order. Its error bound is the
maximum absolute deviation from the median, plus 0.002 times the larger
unobserved interval at the video ends. Valid endpoints from every motion stream
are retained. `CASN`, when present, supplies a serial-number veto. Unsupported,
malformed, or conflicting serials clear the continuity evidence.

The bundled HERO11 chapters test continuity without serial numbers. A
synchronized pair of different cameras with coincident clocks and counters is
a remaining false-join risk. Two-camera negative media are not available.

### Per-file cache

The default cache directory is `~/.cache/gps-video-sync`. Each recording has
its own key. The key contains the canonical absolute path, file size,
nanosecond modification time, sample rate, and cache version `3.0.0`.
Reordering or removing other files reuses the cache of a recording.
Size or modification-time changes invalidate the key.
Cache writes are atomic. The extractor rebuilds corrupt caches. An unavailable
or read-only cache does not stop extraction.

Cache files and exports contain absolute recording paths and can contain camera
GPS UTC times and serial numbers. Paths can show user names and directory names.
Remove or replace this data before you share these files. Frame extraction needs the
original recording path to exist as a local file.

## Plugins

`register_track(name, reader)` registers a `TrackReader` instance with
`read(path, *, altitude="auto") -> Track`.
`register_video(name, source)` registers a `VideoSource` instance with
`read(path, *, hz=10, cache_dir=None) -> Telemetry`.
Registrations can replace existing names.
Package import registers `igc`, `gopro`, and `npz`.

A track reader unwraps UTC times and selects altitude. A video source extracts
and resamples telemetry for one recording. When a source returns no `video`,
`read_video` sets it to the canonical input path.

Motion matching uses uppercase `GYRO` and `ACCL` channels with three components
and the units above. GPS-only sources can supply GPS observations without
motion channels. The motion features are `yaw`, `gyro_norm`, `vertical`, and
`gload`.

## CLI and telemetry formats

`python -m gps_video_sync` and `gps-video-sync` supply the same commands.
`sync` accepts one or more recordings. `info`, `telemetry`, and `verify` each
accept exactly one recording, so exports, plots, and frame times stay local to
that file.

Common options follow the command name:

- `--source NAME` selects a video source. The default is `gopro`.
- `--cache-dir PATH` passes a cache directory to the source plugin.
- `--hz NUMBER` sets a positive, finite telemetry sample rate. The default is 10.
- `--track PATH` is necessary for `sync` and `verify`.
- `--track-format NAME` selects a track reader for `sync` and `verify`.
- `--altitude auto|gps|pressure` selects IGC altitude for `sync` and `verify`.

Invalid command-line arguments give exit code 1 and a usage message.

`info` writes schema `2.0.0` JSON to stdout. It contains `video`, `input_file`,
`duration`, `samples`, `start_seconds`, `end_seconds`, `channels` with array
shapes, and `gps_samples`.
`telemetry --out` accepts a path that ends in `.npz` or `.json`.

### Sync results

`sync` writes a JSON object with `schema_version: "2.0.0"` and a `videos` list,
also for one input. Each entry is a serialized `SyncResult`. Entries are in
input order. `--json PATH` writes the document only to that file. Otherwise
the document goes to stdout. `sync` writes all per-file results before it
returns a failure exit code.

Exit codes are 0 when every match is `ok`, 2 when a match is `ambiguous` and no
match is `failed`, and 1 when a match is `failed` or a run error occurs.
For each result that is not `ok`, a line on stderr identifies the input file,
the status, and the reason. Expected input and external-tool errors have no
Python traceback.

### Verification and frames

`verify` reads `--result` or synchronizes the single recording.
A saved document must use schema `2.0.0` and contain a nonempty `videos` list.
`verify` checks every entry, not only the selected entry. Each entry needs
schema `2.0.0`, a nonempty `video`, a valid status, and timeline metadata.
`confidence`, `per_feature`, and `drift` must be objects or `null`. Offsets
must be finite. Uncertainties must be finite and nonnegative. An `ok` entry
needs a valid alignment.

Selection uses the `video`, `media_offset_seconds`, and `duration` values of
the current telemetry. When a matching entry has status `ok` or `ambiguous`,
selection ignores matching `failed` entries. Exactly one entry must remain.
Different crops of the same recording can share a document. Unrelated
timelines do not have to be unique. An NPZ input selects by original recording
and local timeline, not by archive path. `verify` rejects missing metadata,
duplicate matches for the current timeline, malformed entries, and mismatches.
Direct `plot_verification` calls apply the same identity and timeline check.

`media_offset_seconds` must be finite and nonnegative. Only failed entries can
use `null` for an unknown timeline, and then `duration` must also be `null`.
Otherwise, `duration` must be positive and finite, or `null` when unknown.

A successful entry needs a finite `offset_seconds` or `videoStartUtcSeconds`.
`clockRate` must be positive or `null`. When `clockRate` is `null`, `verify`
uses `drift.clock_rate`. When both are absent or `null`, the clock rate is 1.
Invalid values give exit code 1.

`verify` draws a plot only when the selected or computed result is `ok`. An
`ambiguous` result gives exit code 2 and a `failed` result gives exit code 1,
with no plot. `--output` must end in `.png`. The plot compares normalized track
and video motion features on the mapped UTC axis. Without `GYRO` and `ACCL`,
the plot shows track altitude and raw channel norms. The title identifies the
recording, status, and offset. The plot is a visual check, not a confidence
score.

`--frames` takes local recording seconds in `[0, duration)`. The duration is
outside the interval. Frame extraction needs a known duration and a local
original recording. `verify` checks all frame times before it writes the plot.
Frame paths and bounds come from the current telemetry, not from paths in the
saved result. Sliced telemetry adds its media offset when it seeks.

`verify` writes frame PNGs in the directory of the plot. A frame name contains
the plot name, the recording file name without extension, the frame index,
and the local time. FFmpeg receives an existing local `file:` path, with only
the `file` protocol allowed. Seek times use fixed-point seconds with six
decimals.

### Telemetry archives

`npz` is an API and CLI source. It reads one NPZ archive without pickle
support. A raw archive needs a 1-D `time` array that is finite, nonnegative,
and strictly increasing. Channel arrays use their channel names and must match
the length of `time`. Optional `gps_time` and `gps_utc` arrays must be 1-D
arrays of the same length. Without recording metadata, the identity is the
canonical archive path and the duration is unknown. Such an archive supports
matching but not frame extraction. The source keeps stored sample times. The
sample rate and cache options do not resample NPZ input, but `--hz` must still
be positive and finite.

Exports use schema `2.0.0` and keep the local arrays. NPZ reserves `time`,
`gps_time`, `gps_utc`, `schema_version`, and `recording`. An NPZ export rejects a
channel with one of these names or the name `chapters`. `recording` is a scalar
JSON object with `video`, `duration`, and `media_offset_seconds`, plus optional
`clock`. NPZ import keeps these fields. `clock` contains `origin_seconds`,
`reach_seconds`, `sample_counters`, and optional `camera`. Counter endpoints
are JSON integer pairs. Missing or malformed optional clocks leave ordinary
telemetry and matching usable. A nonzero media offset discards the clock.
The source rejects archives with `chapters` data or another schema version.

JSON telemetry contains `schema_version`, `time`, `channels`, `gps_time`,
`gps_utc`, and `recording` with the same identity and timeline fields.
Non-finite array values become `null`.
JSON telemetry is an output format, not a video source.

The repository check `python scripts/check_continuous_videos.py` runs the API
and the actual CLI with bundled media. It covers exact drift-aware boundaries,
input permutations, subsets, cold and warm caches, cache rebuilds, exports,
crops, saved-result selection, plots, and direct FFmpeg frame comparisons.
Synthetic recordings test non-unit drift, weak-chapter rescue, missing middle
files, gaps, overlaps, missing and mismatched counters, branching, and failed
members. Real motion with synthetic GPS tests contradictory strong fits and
GPS steps. The script needs the `verify` extra and `ffmpeg`.

## Matching limits

Camera GPS UTC is the primary reference when usable. Motion matching is the
fallback when GPS time is absent or not usable. GPS time is usable when all of
these conditions are true:

- A track date exists, or the camera GPS establishes one.
- At least three finite observations exist, and at least 80% are inliers of a
  robust line fit. The line connects the medians of the first and last thirds
  of the GPS time span, or of the observations when a time third has fewer than
  three observations.
- The inliers span at least 10 seconds of video time.
- Video time strictly increases across the inliers. GPS UTC increases from the
  first to the last inlier and never decreases by more than 0.1 seconds.
- The fitted clock rate differs from 1 by at most 0.002, plus three standard
  errors of the fitted rate, plus 0.2 seconds divided by the GPS span.
  A rate difference of 0.002 is 7.2 seconds per hour. The last term allows for
  slow wander of up to 0.1 seconds over a short span. 0.002 is more than ten
  times the usual tolerance of camera clock crystals. Larger rates come from
  frozen, stepped, or wrong UTC values.
- The residual standard deviation is less than 0.5 seconds.
- No UTC step occurs. The check uses all finite observations, before outlier
  removal. It removes the rate between the medians of the first and last thirds
  of the observations. For window sizes of 3, 5, 10, 20, and 30 observations,
  up to half the observations, and at each position, it compares the median of
  the preceding window with the median of the following window. Near the start
  and end, a window shrinks to the available observations, down to one. The
  check first reduces the difference by the possible detrending rate error
  times the time from the first to the last observation of the two windows.
  The rate error is 0.1 seconds, plus the rate times the time ranges of both
  thirds, divided by the distance between the third medians. A reduced
  difference of more than 0.1 seconds, and of more than six standard errors of
  the white noise between adjacent observations, is a step. The standard-error
  term is at most 0.15 seconds. Noise within ±0.05 seconds cannot cause a step.
  A single outlier inside the interval is not a step. A deviating first or last
  observation is a step.
- The GPS interval overlaps the track.
- The estimated uncertainty is at most 2 seconds over the whole video timeline.

Synchronization ignores GPS UTC values outside the years 1970 to 9998. It uses
the fitted clock rate when the rate is significant and changes the mapping by
more than 0.5 seconds over the combined interval of the video timeline and the
GPS observations. Otherwise `clockRate` is 1 and the offset is the median at
the GPS observations.

A gap in the GPS observations is longer than 2 seconds and five median sample
intervals. Gaps divide the observations into runs. The within-run rate is the
least-squares rate with a separate offset for each run, so a step at a gap does
not change it. Its bound is the largest rate error that noise within ±0.05
seconds can cause. When the overall fitted rate differs from the within-run
rate by more than both bounds and three combined standard errors, the GPS UTC
has a step at a gap.

The uncertainty is 0.1 seconds at the center of the GPS observations, plus the
possible rate error times the distance to the farther video end. The possible
rate error is the difference between the used rate and the within-run rate,
plus the within-run bound and three standard errors. The uncertainty also
covers the clock of each run. The clock of a run uses the median offset of the
run at the within-run rate. At both video ends, the uncertainty covers the
difference from that clock, plus 0.1 seconds, plus the within-run bound and
three standard errors times the distance to the center of the run. The reported
uncertainty is at least 0.5 seconds and at least three residual standard
deviations. A short GPS interval far from the video start or short runs between
gaps increase the uncertainty. A step at a gap that the runs cannot separate
from a rate change also increases it. This uncertainty can prevent a GPS
result.

When GPS time is present but not usable, `confidence.gps_rejected` gives the
reason. A motion result that is not accepted also states that reason in
`message`.

The motion matcher filters each valid run separately. It searches with masked,
normalized FFT correlations, then refines the offset locally in 0.1-second
steps within 3 seconds.

Each feature needs an overlap of at least 90 seconds and at least 65% of the
shorter finite feature sequence. Finite samples exclude gaps, filter edges, and
track samples slower than 3 m/s. Camera motion before launch, for example on
the ground, is still finite. Thus a 900-second clip with 540 seconds of flight
motion and 360 seconds of pre-launch time does not meet the 65% rule and is
ambiguous. Trim the video telemetry to the flight with `Telemetry.slice` when
most of a clip is before launch or after landing. A track or video that spans
less than two whole seconds is ambiguous.

Acceptance needs all of these conditions:

- The weighted score is at least 0.38.
- The margin to the best alternative is at least 0.08.
- The ratio of the best score to the best alternative is at least 1.2.
  Alternative offsets exclude a 25-second neighborhood around the best peak.
- The local refinement does not stop at the edge of its 3-second range.
- Two feature groups agree with the refined offset within 8 seconds, each with
  a score above 0.3. The turn group is `yaw`, `gyro_norm`, and `gload`. The
  other group is `vertical`. As an alternative, a weighted score above 0.7 with
  a margin above 0.25 is sufficient.

These thresholds are heuristics, not calibrated probabilities. Accepted motion
results include a two-second systematic uncertainty allowance.

Drift data comes from fits in non-overlapping 400-second windows. Detection
needs at least four usable windows, with at least 80% of them consistent with
a robust line. It also needs a slope above three standard errors and an
accumulated change above 0.5 seconds. `clockRate` stays at 1 when the matcher
detects no drift. `measured_clock_rate`, seconds per hour, and the standard
error stay separate diagnostic values. No detected drift does not prove clock
stability.

Repeated turns, low motion, sensor noise, mounting differences, altitude noise,
data gaps, and short overlap can weaken a match or produce a false match.
Native camera axes and quaternion frames are not geographic heading.
Motion matching has real-media coverage for one flight and mount type.
Camera GPS parsing has synthetic test coverage.
