# API and format reference

## Recording identity

`read_recording_identity(path)` returns immutable per-file recording evidence.
It accepts a text path or `os.PathLike[str]`. Invalid path types raise `TypeError`.
An embedded null character in the path raises `ValueError`.
Media and I/O problems return a typed failure. This API is independent of
`read_video`, synchronization, and resampling.

| Result | Public fields | Meaning |
| --- | --- | --- |
| `RecordingIdentity` | `key`, `contract` | Sufficient supported evidence. The key is a lowercase SHA-256 hex string. |
| `IdentityUnavailable` | `reason`, `contract` | Readable media without sufficient supported evidence. |
| `IdentityFailed` | `code`, `contract` | Extraction could not determine whether evidence exists. The consumer can retry. |

`IdentityResult` is the union of these types. Only `RecordingIdentity` has a key.
Results contain no paths, raw serials, coordinates, creation times, or telemetry.
Failure codes contain no exception text.

### Comparison and persistence

`IDENTITY_CONTRACT` is `gopro-recording-identity/1.0.0`.
`compare_identity(a, b)` returns one of these strings:

- `same` means both results are available under this exact supported contract
  and their keys match.
- `different` means both results are available under this exact supported
  contract and their keys differ.
- `incomparable` means either result lacks evidence or uses any other contract.

Direct result equality uses the same rule for `same`. An unavailable, failed,
or unsupported-contract result never equals another result, including itself.
The result types are not hashable. Use `compare_identity` for identity decisions.

Every result has `to_dict()`. `identity_from_dict(value)` validates a stored
dictionary and returns the corresponding result. Missing or extra fields,
invalid keys, invalid contract syntax, and unknown reason or code strings raise
`ValueError`. The stored forms are:

```json
{"state": "available", "contract": "gopro-recording-identity/1.0.0", "key": "6325f85402d4590a91d89ed1c177fcde8ff17fbf5d7fdd866d128e23bb4732b5"}
```

```json
{"state": "unavailable", "contract": "gopro-recording-identity/1.0.0", "reason": "no_telemetry"}
```

```json
{"state": "failed", "contract": "gopro-recording-identity/1.0.0", "code": "read_error"}
```

The available example is the synthetic golden test key. Store the full contract,
not only the key. The parser preserves syntactically valid unsupported versions
when their result fields are valid. Comparison still returns `incomparable`.
A change to evidence policy or canonical encoding requires a new contract.
Even a patch-version difference is not silently comparable. Re-extract when the
stored contract differs from `IDENTITY_CONTRACT`.

```python
import json
from gps_video_sync import IDENTITY_CONTRACT, identity_from_dict, read_recording_identity

result = read_recording_identity("recording.MP4")
stored_json = json.dumps(result.to_dict())
restored = identity_from_dict(json.loads(stored_json))
needs_refresh = restored.contract != IDENTITY_CONTRACT
```

A source-file fingerprint and a recording identity have separate purposes.
The application owns file fingerprints, persistence, retries, and database
updates. File size and modification time can detect some source changes but do
not establish recording identity. Two duplicate originals can have the same
key. Candidate selection, pair uniqueness, playback compatibility, and duration
tolerance remain application decisions. Identity does not join chapters or
establish UTC alignment.

### Supported evidence in version 1.0.0

The extractor requires all of these conditions:

1. The container has exactly one `gpmd` track with at least one sample.
2. `mvhd` has a positive creation integer. Zero and the all-ones sentinel of
   its native width mean missing context.
3. The first packet in native decode order has exactly one ACCL stream and
   exactly one GYRO stream.
4. Each motion record has three signed integer axes with GPMF type `s` or `l`.
   Each stream contains at least two distinct native row values. Constant
   rows are insufficient.
5. Each stream has one exact unsigned `STMP` and `TSMP` integer of type `B`,
   `S`, `L`, or `J`. `TSMP` must be at least the row count. Values above
   `2**53` retain their exact integer value.
6. Present scale, axis, matrix, and unit metadata has a supported interpretation.
   Duplicate selected fields are unsupported.

`SCAL`, when present, contains one or three finite, nonzero numeric values.
An absent scale means native units. `MTRX`, when present, contains nine finite
numeric values. Both accept the simple numeric types supported by GPMF.
`ORIN` and `ORIO` are character strings with one of each X, Y, and Z axis,
in either case. `SIUN` and `UNIT` are nonempty printable character strings.
They accept UTF-8 or native GoPro single-byte text. Native text permits printable
ASCII and bytes `B0`, `B2`, `B3`, and `B5`, for example `m/s\xb2`.
Trailing null characters are permitted. The original bytes enter the key unchanged.

Creation time supplies context, not uniqueness. Exact varied native sensor rows
and their absolute clocks and counters supply additional recording evidence.
Adjacent chapters with continuing clocks have different native starts. This
profile is conservative evidence, not a guarantee that evidence cannot collide
or be copied. Byte-identical evidence intentionally matches.

GPS, serial numbers, `MUID`, `GUMI`, `CDAT`, filenames, directories, video
encoding, duration, synchronization bounds, and unrelated GPMF fields do not
enter the key. Edits and presentation offsets do not select the first packet.
No GPS fallback exists. A file without GPS or a serial can still have evidence.
The first-packet contract does not certify later telemetry or media integrity.

Canonical bytes use this exact order:

1. The ASCII contract string followed by one null byte.
2. Movie creation as one unsigned big-endian 64-bit integer.
3. ACCL, then GYRO. For each sensor, append its four ASCII key bytes, its raw
   record encoding, then `STMP` and `TSMP` as unsigned big-endian 64-bit integers.
4. Within each sensor, append metadata in this fixed order: `SCAL`, `ORIN`,
   `ORIO`, `MTRX`, `SIUN`, `UNIT`. Each field starts with a zero byte for absence
   or a one byte followed by its raw record encoding for presence.

A raw record encoding is its one-byte type, one-byte row size, unsigned
big-endian 16-bit repeat count, and exactly `size * count` data bytes.
It excludes padding and the record key. Motion keys are explicit. Optional
metadata keys have fixed positions. These lengths and presence tags make the
encoding unambiguous. SHA-256 of these bytes produces `key`.

Counters and movie creation normalize to exact unsigned 64-bit values. Motion
rows and optional metadata retain their native type, size, count, and bytes.
A changed anchor, first packet, row encoding, or metadata presence can cause a
missed match even when the files depict the same recording. Remuxed or stripped
files can become unavailable. No fuzzy comparison or rational normalization runs.

### Unavailable reasons and failure codes

| Unavailable reason | Meaning |
| --- | --- |
| `no_telemetry` | No `gpmd` track, or its sample table is empty. |
| `no_recording_context` | Movie creation is absent or a sentinel. |
| `no_motion` | The first packet lacks ACCL or GYRO. |
| `unsupported_layout` | Multiple telemetry tracks, ambiguous motion fields, or unsupported motion or interpretation metadata. |
| `missing_native_counters` | Missing or unsupported STMP or TSMP, or TSMP below the row count. |
| `insufficient_sensor_witness` | A required sensor has no varied native rows. |

When several conditions fail, the reported reason is not a complete inventory.

| Failure code | Meaning |
| --- | --- |
| `read_error` | Opening, reading, or inspecting the source failed with an I/O error. |
| `malformed_container` | Container framing or required tables are invalid, a first sample is outside `mdat`, or the container layout cannot be interpreted. |
| `malformed_telemetry` | First-packet GPMF framing or numeric payload is invalid. |
| `limit_exceeded` | A parser resource bound prevented extraction. This does not mean evidence is absent. |
| `source_changed` | File metadata on the open descriptor changed during extraction. |

Fragmented MP4 and external sample data references return `malformed_container`.
No video track is required. A valid video duration is not required. Required
telemetry sample tables must still be structurally valid.

### Privacy, cost, and coverage

The API opens one read-only file handle. It reads top-level box headers, the
`moov` metadata, and the first telemetry payload. It seeks past video data and
decodes no video. Work depends on metadata and table size, not video byte size.
It inspects the same descriptor before and after extraction to detect ordinary
concurrent changes. This check is not a transactional snapshot or full-file
integrity check.

Current parser limits are 64 MiB for `moov`, 16 MiB for the first payload,
100,000 visited boxes, 128 tracks, 10,000,000 samples or entries per table, and
GPMF container depth 8. These are input bounds, not a total process-memory bound.
Table validation can allocate additional Python objects. A resource limit is
always a failure, never unavailable evidence.

Identity extraction reads no cache and writes no files. It retains no telemetry
or source paths in results. It has no `hz` or cache setting. Repeated unchanged
input has the same comparable evidence under the same contract on supported
platforms. Cold or warm synchronization caches and resampling frequency cannot
change the identity. `read_video` retains its separate default cache behavior,
which can persist telemetry and paths. Use `read_recording_identity` alone for
private no-cache extraction.

A key is linkable. Anyone with a candidate file can compute its key and perform
a known-input lookup. The digest is not anonymization or proof of authenticity.

GoPro HERO11 Black with firmware H22.01.02.32.00 has verified MP4 and LRV
matches and distinct adjacent chapters. The verified pairs include no-GPS and
no-serial first packets. Other cameras and firmware are not verified.
Synthetic actual-MP4 tests cover GPS and no-GPS duration differences at
30.0167 and 30.05 seconds,
chapters, duplicate originals, optional metadata, exact large counters, errors,
resource bounds, persistence, privacy, and cache independence.

### Check private recording pairs

From a repository checkout, supply explicit positive pairs with arbitrary
filenames. Supply expected different recordings or chapters with `--different`.
Both options are repeatable.

```sh
uv run python scripts/check_recording_identity.py \
    --pair /media/original.MP4 /media/preview.LRV \
    --different /media/first-chapter.LRV /media/second-chapter.LRV
```

Add `--gopro-folder /media/camera` for an optional direct-folder survey.
The survey uses GoPro filenames only to select candidate positive pairs,
chapter negatives, and cross-recording negatives. Those filename expectations
are hints, not identity evidence. Verify that the folder has no renamed
cross-recording duplicates before you use these expectations as a test oracle.

The script prints aggregate JSON counts only. It prints no paths, keys, raw
fields, serials, or coordinates. It checks repeated reads, persistence,
unchanged source metadata, and renamed and moved symbolic-link aliases.
Temporary aliases need no video copies and are removed after the check.
Exit code 0 means every expectation passed. Code 1 means a comparison or check
failed. Code 2 means invalid arguments or an input or check error. An unavailable
expected positive never passes. An unavailable expected negative also fails.
A survey folder with no positive filename hints returns code 2.

## Data contracts

`read_track(path, *, format="igc", altitude="auto")` returns a `Track`.
`read_video(path, *, source="gopro", hz=10, cache_dir=None)` reads one recording
and returns a `Telemetry`. A value that is not one `str` or `Path`, for example
a list of paths, raises `ValueError`.
`synchronize(track, telemetry, *, gps_policy=GpsTimingPolicy())` returns one `SyncResult`.
`match_videos(track, paths, *, source="gopro", hz=10, cache_dir=None, gps_policy=GpsTimingPolicy())` returns a
list of file-local `SyncResult` values in input order.

- `Track(time, latitude, longitude, altitude, date=None, altitude_source="gps")`
  contains NumPy arrays. Time increases in UTC seconds from midnight of the
  track date. Coordinates are in degrees. Altitude is in metres.
- `Telemetry(time, channels, video=None, duration=None, gps_time=[], gps_utc=[], media_offset_seconds=0, clock=None, camera_start=None)`
  contains increasing, nonnegative, file-local video seconds and NumPy channel
  arrays. `gps_time` and `gps_utc` default to empty arrays. Each channel has one
  row per time sample. `video` identifies the recording. The GoPro source uses
  its canonical absolute path. `duration` bounds the half-open recording
  interval `[0, duration)`. `gps_time` contains local video seconds for camera
  GPS observations. `gps_utc` contains the related Unix UTC seconds.
  `clock` is optional validated `RecordingClock` continuity evidence.
  `camera_start` is optional `CameraStart` evidence for session placement.
- `RecordingClock(origin_seconds, reach_seconds, sample_counters, camera=None)`
  contains a finite sensor-clock origin at local video time zero and a finite,
  nonnegative origin error bound. `sample_counters` maps stream names to exact
  integer `(start, end)` cumulative counter endpoints, with `0 <= start < end`.
  `camera` is an optional nonempty serial number. The sensor clock is not UTC.
- `CameraStart(seconds, bound_seconds, camera=None)` contains camera-clock time
  at local video zero, not UTC. `seconds` and the nonnegative error bound are
  finite. `camera` is an optional nonempty serial number.
- `Telemetry.slice(start, end)` selects the half-open interval `[start, end)`
  and subtracts `start` from video and GPS times. The bounds must be finite,
  with `0 <= start < end`. When the duration is known, `end` must not exceed it.
  The slice keeps the recording identity, sets the duration to `end - start`,
  and adds `start` to `media_offset_seconds`. Nested slices add their media
  offsets. Frame extraction adds this offset to slice-local seconds before it
  seeks the original recording. The synchronization offset of a slice refers
  to the start of the slice. Every slice clears `clock` and `camera_start`,
  including a slice of the full duration. Counter endpoints cannot be reused
  for a crop.
- `SyncResult(outcome, ...)` stores `Accepted(TimeMapping)`, `Ambiguous`, or
  `Failed` in `outcome`. Only `Accepted` has a mapping. `status`,
  `offset_seconds`, `uncertainty_seconds`, `method`, and `message` are derived.
  The result also contains the recording identity in `video`, confidence,
  per-feature data, drift diagnostics, and date. `media_offset_seconds` and
  `duration` identify the local timeline used for matching. They copy the current telemetry values, also for
  ambiguous results and for failures with valid telemetry. The media offset is
  `null` when extraction or telemetry validation fails. The duration is `null`
  when unknown. Batch results also contain `input_file`, the canonical input
  path. For NPZ input, `video` identifies the original recording and
  `input_file` identifies the archive. `to_dict()` uses schema `3.0.0`.
  Direct array-based callers can omit recording identity and duration.
  Frame extraction needs both.
- `TimeMapping(offset_seconds, uncertainty_seconds, clock_rate, source)` owns
  the whole-clip mapping. `source` is `OwnGps`, `OwnMotion`, `JoinedShot`, or
  `CameraSession`. Drift diagnostics do not set the mapping rate.
- `Ambiguous(independent, session=None)` keeps the independent rejection text.
  `session` is `SessionInsufficient`, `SessionContradiction`, or `None` when
  no batch placement ran. `Failed(message)` has no mapping.

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
cannot be promoted. An independent rejected GPS evaluation with `blocks_transfer`
also prevents a shared shot mapping. Chapter creation times must agree within 1 second. A larger difference
is a contradiction. A group result that is not `ok` retains all independent
results. Unsafe GPS timing reported on the shot also retains them, even when
the shot has an accepted motion fit. The same `gps_policy` evaluates individual
recordings and the whole shot. The projected
mapping must agree with every independent `ok` mapping at both ends of that
member, within the sum of the two uncertainty bounds. A contradiction retains
all independent results. Unexpected matcher errors propagate.

Each projected member keeps its recording identity, media offset, duration,
and date. Offsets use the left fold `O_next = O + clockRate * duration` with
no rounding, so each end equals the next start as a floating-point value.
Input permutations give the same result per recording. A subset of a
continuous shot can give a different result. Session placement does not change
accepted shot projections.

Every result has `shot.status`. `single` means no candidate continuity group.
`joined` means projection was applied. `ambiguous` means a candidate group or
branch has no approved common mapping. Session placement can still accept a
member when the candidate has no accepted or failed members and no conflict. Group metadata
also has `members` in chronological order, `index`, `start_seconds` on the
candidate shot timeline, and `reason`. Branch metadata names the whole candidate
group in sensor-clock and recording-identity order.
Joined results label `shot.diagnostics_coordinates` as `shot`. Their
`gps`, `confidence`, `per_feature`, and `drift` diagnostics, including candidate
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

### Camera-session placement

After chapter matching, `match_videos` places unresolved clips from accepted
recordings. It partitions `CameraStart` evidence by serial. Each known serial
has its own group. Missing serials form one group and never join a known serial.

An anchor is an accepted GPS, motion, or joined-shot mapping with camera-start
evidence and a known duration. A joined shot counts once, at its earliest
available chapter. Session results never become anchors. At least two anchor
units are required. Placement does not use the target's correlation peaks.

For an anchor, the camera offset is its mapping offset minus its camera start.
Its bound is the mapping uncertainty plus the camera-start bound. Every pair
must agree within the sum of those bounds plus `0.0002 * abs(camera separation)`.
This test is independent of the target. A disagreement makes every unresolved
member of that camera group contradictory. Camera intervals that overlap by
more than the sum of their camera-start bounds also contradict the group.

For each target, every anchor predicts an offset at the target's camera start.
The prediction bound is the anchor bound plus `0.0002 * abs(camera separation)`.
The intersection of these intervals gives the allowed offsets. Its midpoint is
the offset. Its half width, plus the target's camera-start bound, plus
`0.002 * duration`, is the whole-clip uncertainty. An empty intersection is a
contradiction. An uncertainty above 10 seconds is insufficient evidence.
Session mappings use media clock rate 1. Camera-clock drift does not set the
media rate.

Placement keeps accepted results and failed results unchanged. Joined chapters
keep their projected offsets and rates. Rejected unsafe GPS timing blocks both
promotion and use as a session anchor, including after a successful motion
fallback. An accepted bounded GPS discontinuity can be an anchor. An
unapproved continuity candidate cannot promote other members when it retains
an accepted member or contains a failed member. Branches and conflicting
serials also block placement. Chapter creation times that differ by more than
1 second are contradictory.

A session mapping covers the whole clip. Partial or zero track overlap does
not prevent placement. `source.track_overlap_seconds` reports coverage only.

Files without serials can come from different cameras. Coincident camera
clocks can then pass the session checks and cause a false placement. Two-camera
negative media are not available. Verification plots remain necessary.

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
report drift and uncertainty. Ambiguous and failed results have no offset,
uncertainty, or `clockRate`.
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

The reader parses `mvhd` creation seconds in version 0 and version 1 headers.
Zero means missing. With a validated recording clock, GoPro extraction builds
`CameraStart` as creation seconds plus `RecordingClock.origin_seconds`.
Its bound is 1 second for creation-time quantization plus the recording-clock
reach. Chapters use the recording creation time and their own sensor-clock
origin.

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
nanosecond modification time, sample rate, and cache version `4.0.0`.
The cache stores creation seconds and the validated recording clock.
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

`info` writes schema `3.0.0` JSON to stdout. It contains `video`, `input_file`,
`duration`, `samples`, `start_seconds`, `end_seconds`, `channels` with array
shapes, and `gps_samples`.
`telemetry --out` accepts a path that ends in `.npz` or `.json`.

### Sync results

`sync` writes a JSON object with `schema_version: "3.0.0"` and a `videos` list,
also for one input. Each entry is a serialized `SyncResult`. Entries are in
input order. `--json PATH` writes the document only to that file. Otherwise
the document goes to stdout. `sync` writes all per-file results before it
returns a failure exit code.

`source` is `null` without an accepted mapping. An accepted source has one of
these `kind` values:

- `gps` is the recording's own GPS evidence.
- `motion` is the recording's own motion evidence.
- `shot` is a joined-shot projection. It includes `solved_by`, `members`,
  `index`, `start_seconds`, and `supporting`. The supports exclude the current
  input. `method` is the shot's `solved_by`, either `gps` or `motion`.
- `session` is camera-session placement. It includes a nonempty `supporting`
  list of anchor input paths, `camera_start`, `independent_rejection`, and
  `track_overlap_seconds`. `camera_start` has `seconds`, `bound_seconds`, and
  `camera`. Supports name all chapters of a supporting joined shot.
  `method` is `session`.

An ambiguous batch result has a `session` object with `verdict`, `reason`, and
`message`. The verdict types have different meanings:

- `insufficient` means that the evidence cannot constrain placement. Reasons
  are `no_camera_start`, `too_few_anchors`, or `loose_bound`.
  For example, `too_few_anchors` reports
  "Fewer than two accepted recordings from this camera".
- `contradictory` means that the timing evidence conflicts. Reasons are
  `anchors_disagree`, `recordings_overlap`, `chapter_creation`, `gps_step`, or
  `continuity_conflict`. Each reason names the affected `recordings`.
  For example, `anchors_disagree` reports
  "Accepted recordings disagree on the camera clock offset".

Both verdicts keep status `ambiguous` and provide no offset. The top-level
`message` keeps the independent rejection. A session verdict adds its message as a second sentence.
An independent `synchronize` call has no session verdict.

Exit codes are 0 when every match is `ok`, 2 when a match is `ambiguous` and no
match is `failed`, and 1 when a match is `failed` or a run error occurs.
For each result that is not `ok`, a line on stderr identifies the input file,
the status, and the reason. Expected input and external-tool errors have no
Python traceback.

### Verification and frames

`verify` reads `--result` or synchronizes the single recording.
A saved document must use schema `3.0.0` and contain a nonempty `videos` list.
`verify` checks every entry, not only the selected entry. Each entry needs
schema `3.0.0`, a nonempty `video`, a valid status, and timeline metadata.
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

Exports use schema `3.0.0` and keep the local arrays. NPZ reserves `time`,
`gps_time`, `gps_utc`, `schema_version`, and `recording`. An NPZ export rejects a
channel with one of these names or the name `chapters`. `recording` is a scalar
JSON object with `video`, `duration`, and `media_offset_seconds`, plus optional
`clock` and `camera_start`. NPZ import keeps these fields. `clock` contains
`origin_seconds`, `reach_seconds`, `sample_counters`, and optional `camera`. Counter endpoints
are JSON integer pairs. Missing or malformed optional clocks leave ordinary
telemetry and matching usable. `camera_start` contains `seconds`,
`bound_seconds`, and optional `camera`. Missing or malformed camera-start
metadata does not prevent independent matching. Import keeps camera-start
evidence only with a valid recording clock. A nonzero media offset discards
both kinds of timing evidence. The source rejects archives with `chapters` data
or another schema version.

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
fallback when GPS time is absent or rejected. A rejected GPS candidate is never
the selected mapping. Motion fallback can also fail to establish an alignment.

### GPS timing policy

`GpsTimingPolicy` is frozen and keyword-only. Its options are:

| Option | Default | Meaning |
| --- | --- | --- |
| `max_uncertainty_seconds` | `2.0` | Maximum estimated error over the entire interval `[0, duration]`, in seconds. |
| `allow_bounded_discontinuities` | `False` | Permit detected discontinuities only when all hard checks and the budget pass. |

The budget must be a finite positive real number. Booleans are invalid budgets.
The discontinuity option must be a boolean. Invalid policies raise `ValueError`.
Budgets below 0.5 seconds and above 2 seconds are valid. The uncertainty floor
is 0.5 seconds, so a smaller budget rejects GPS. A larger budget does not relax
the fixed quality, rate, excursion, or duration checks.

`synchronize` and `match_videos` accept `gps_policy`. The exact
policy also evaluates a concatenated shot. CLI `sync` and fresh `verify` accept
`--gps-max-uncertainty-seconds` and
`--gps-allow-bounded-discontinuities`. Explicit policy options with
`verify --result` are input errors. Saved-result verification uses the saved
mapping and diagnostics without a new GPS decision.

### GPS evaluation and hard checks

Every `SyncResult.gps` is a frozen `GpsEvaluation`. Its `verdict` is
`accepted`, `rejected`, or `unavailable`. `reason_codes` is empty only for
acceptance. `fit` is a bounded diagnostic candidate or `None` when no finite
whole-video estimate exists. Only an accepted evaluation supplies the GPS
outcome mapping.

The fixed checks require:

- At least three finite supported observations and at least 80 percent fit
  inliers. The robust seed connects medians of the first and last time thirds,
  or observation thirds when a time third has fewer than three samples.
  Inliers have seed residuals within the larger of 0.5 seconds and six median
  absolute residuals.
- At least 10 seconds of inlier support.
- Strictly increasing video times. Inlier UTC must increase overall and cannot
  decrease between adjacent inliers by more than 0.1 seconds.
- A fitted rate within `0.002 + 3*stderr + 0.2/span` of one.
- Known actual recording duration. Finite observations must lie in
  `[0, duration)`. The last motion sample does not replace duration.
- Safety residual standard deviation below 0.5 seconds and track overlap.
- Finite, identifiable within-run rate evidence.
- Non-affine excursion, including the 0.1-second noise margin, at most
  0.5 seconds.
- No incompatible local rate regimes supported by fixed-noise local fits.
- Whole-video uncertainty within the selected budget.

Unsupported Unix UTC values outside years 1970 to 9998 are ignored.
The selected mapping uses the fitted rate only when its difference from one
exceeds three standard errors and accumulates more than 0.5 seconds over the
actual duration. Otherwise the returned rate is one and the offset is the
median UTC minus video time. All residual metrics describe this returned
mapping, including unselected drift.

Detection is independent of policy. `window_step_detected` uses all finite
pre-clipping observations. It detrends with the rate between the first and
last observation-third medians. Windows of 3, 5, 10, 20, and 30 samples compare
adjacent medians, with shorter windows at endpoints. A difference must exceed
0.1 seconds after the detrending-error allowance and the white-noise allowance.
The latter is capped at 0.15 seconds. Isolated interior spikes do not normally
trigger detection. Endpoint deviations remain visible.

A gap exceeds both 2 seconds and five median sample intervals. Each run has a
separate level. `gap_rate_mismatch_detected` compares the global rate with
the within-run rate using the original fixed-noise bounds and three combined
standard errors. Neither detector reads the policy.
`discontinuity_detected` is the OR of these two flags.

`rate_regime_change_detected` is a separate hard check. Local window widths
start at each run's full span and halve down to a final width of 10 seconds.
Each width uses half-width strides and windows anchored at both run endpoints.
Scales too short to contain six samples are skipped. Window bounds use binary
search in the ordered run, without a full-data mask for each window.

A local fit needs at least six samples and 10 seconds of observed span.
The timestamp allowance `R` is the maximum absolute float spacing of finite,
supported raw `gps_utc`, plus the maximum absolute float spacing of the
corresponding raw `gps_time`, plus a `1e-8`-second numerical floor. The
calculation precedes UTC origin subtraction. It uses no policy value.
The effective noise radius is `N = 0.05 + R` seconds.
A window supplies rate evidence when an affine line fits every observation
within ±`N` seconds. An ordinary least-squares residual range at most
`2*N` establishes feasibility. Otherwise, a centered and scaled
linear feasibility problem with two variables checks the window. It starts
with endpoint and extreme-value constraints, adds violated constraints, and
checks each candidate line against every original observation within `N`.
The LP reserves the `1e-8`-second floor by using radius `N - 1e-8` in its
constraints, so numerical error in a boundary solution fits within `N`.
A solver failure or an unverifiable candidate rejects GPS with
`clock_model_unbounded`.

Infeasible windows, including windows across offset steps, supply no rate
evidence. Rates in disjoint eligible windows must agree within their fixed
bounded-error intervals. For centered times `z_i`, each interval is centered
on the ordinary least-squares rate and has radius
`N * sum(abs(z_i)) / sum(z_i**2)`.
Every affine rate that fits the window within the noise allowance lies in
this interval. The intervals do not expand with pooled residuals or permitted
offset steps. Short endpoint regimes supply evidence when their span and
rate difference exceed these bounds. Sparse evidence does not establish a
rate change.

Safety observations exclude only clipped, isolated interior points whose
immediate neighbors are inliers and agree after detrending within 0.1 seconds.
Neither adjacent points nor endpoints nor points next to gaps qualify.
Sustained deviations remain in the envelope even if the robust fit trims them.

### Conditional whole-video envelope

Let `M(t) = a + b*t` be the selected mapping, `b_w` the within-run rate,
and `s_w` its standard error. UTC arithmetic uses a common subtracted origin.
For each safety run `j`, with mean video time `c_j`:

```text
z_i = t_i - c_j
L_j = median(u_i - b_w*t_i)
A_j = max(abs(u_i - L_j - b_w*t_i))
W = sum_j sum_i z_i**2
B = sum_j ((N + A_j) * sum_i abs(z_i)) / W
E = abs(b - b_w) + B + 3*s_w
V_j(t) = abs(M(t) - L_j - b_w*t) + 2*N + A_j + E*abs(t-c_j)
H = range(u_i - b_w*t_i) + 2*N
```

`W <= 0` is unconstrained. `H > 0.5` rejects the model independently of
the caller budget. Each endpoint error is `max_j V_j(t)`, evaluated at
`t=0` and at the actual duration. The whole-video estimate is the maximum
of both endpoint errors, 0.5 seconds, the maximum absolute safety residual,
and three safety residual standard deviations. Convex affine endpoint
envelopes cover gap interiors under the common-rate assumption. Gaps supply
no cross-gap rate evidence and receive no extra interpolated rate evidence.

The envelope uses the same `N` as the local-rate check. Its level-difference
margins use `2*N`, including twice the timestamp allowance. Strict step and
gap detectors retain their fixed thresholds and bounds.
`B` includes observed run residual radius as a bounded-error allowance.
It does not shrink like independent measurement noise merely because
correlated observations become denser. Late GPS lock, unsampled tails, and
short runs can increase the envelope beyond the budget.

This is a conditional timing-consistency estimate. It assumes a common rate
and bounded residual error between observations. It does not establish
physical UTC accuracy. Constant GPS bias, shared reconstruction bias, and
hidden corrections that reverse inside unsampled intervals are not excluded.
The observations alone do not identify a clock correction versus timestamp
reconstruction error. The 0.5-second excursion ceiling is a conservative
safety limit, not a calibrated physical accuracy claim.

### Structured diagnostics and fallback

Result schema `3.0.0` includes `gps` and `selection` objects.
`gps.policy` gives the effective policy. `gps.hard_limits` gives fixed
limits. `gps.timestamp_rounding_seconds` exposes `R`, including its numerical
floor. `gps.effective_noise_radius_seconds` exposes `N`.
Counts distinguish finite observations, fit inliers, and isolated
safety exclusions. Support times and `fit.envelope.runs` and `gaps` use
local video seconds, or whole-shot seconds when
`gps.diagnostics_coordinates == "shot"`. Each gap contains its start and end times.
Joined members retain the whole-shot envelope and conservative uncertainty;
`shot.start_seconds` locates the member in that coordinate system.

`fit.envelope` exposes `start_seconds` and `end_seconds` as endpoint
error bounds, not observation times. It also exposes `whole_video_seconds`,
`residual_max_seconds`, `residual_std_seconds`, `within_run_rate`,
`correlated_rate_bound`, `rate_stderr`, `possible_rate_error`, and
`non_affine_excursion_seconds`. Unknown estimates are null, never NaN or
infinity.

Rejection codes include `insufficient_observations`, `low_inlier_fraction`,
`span_too_short`, `invalid_observation_time`, `non_increasing_clock`,
`rate_out_of_range`, `duration_unknown`, `observations_outside_duration`,
`discontinuity_not_allowed`, `non_affine_excursion_too_large`,
`rate_unconstrained`, `clock_model_unbounded`, `rate_regime_inconsistent`,
`residual_scatter_too_large`, `no_track_overlap`,
`whole_video_uncertainty_exceeded`, and `non_finite_estimate`.
Unavailable codes are `no_observations`, `date_unresolved`, and
`not_evaluated`. Analysis collects all computable checks, so strict step
rejection can still expose the candidate uncertainty.

`gps.blocks_transfer` centrally blocks shot or session transfer for rejected
detector hits, excessive excursion, inconsistent rates, or an unconstrained
clock. Accepted bounded discontinuities do not block transfer.
`confidence.gps_rejected` remains a human summary. Production decisions do
not parse it.

`selection.motion_attempted` records fallback attempts.
`selection.selected_method` derives from the final mapping source and is
`gps`, `motion`, `shot`, `session`, or `none`. Failed or ambiguous
fallbacks select `none`.

### Timing-consistency analysis tool

`scripts/check_gps_timing.py` accepts `--video`, `--timing-npz`, or both,
optional `--cache-dir`, `--duration-seconds`, and
`--max-uncertainty-seconds` with default 1 second. A timing-only NPZ contains
`time`, `gps_time`, `gps_utc`, and optional scalar `duration`.
Exported telemetry NPZ is also supported.

```sh
uv run python scripts/check_gps_timing.py --video recording.MP4
uv run python scripts/check_gps_timing.py --timing-npz timing.npz --max-uncertainty-seconds 1
```

The tool exercises the public synchronizer against a synthetic overlapping
track derived from observation dates. It compares strict and bounded policies
at the same budget. Output contains relative video times and error metrics,
not paths, serials, dates, or absolute UTC. It is timing-consistency analysis,
not flight ground truth or motion validation. Exit code 1 means an input or
analysis error. A structured GPS rejection is a successful analysis.

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
