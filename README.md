# gps-video-sync

Align action-camera telemetry with an IGC flight track. The tool calculates a
video-to-UTC offset, checks clock drift, and plots the alignment. It uses camera
GPS time when available and falls back to motion matching. In a batch, two
accepted recordings from the same camera can place clips with weak evidence.

My GoPro clock often has the wrong date or time. I use this tool to align the
video with the GPS track after recording.

## Install

Requires Python 3.10 or later. Tests cover Python 3.10 to 3.15.

```sh
pip install gps-video-sync
pip install "gps-video-sync[verify]"
```

From a source checkout, run `pip install .` or `pip install ".[verify]"`.

The `verify` extra installs Matplotlib for plots. Frame extraction also needs
`ffmpeg` on `PATH`. GoPro MP4 and LRV extraction needs no external program.

## Align recordings

Match one or more recordings against one flight track:

```sh
gps-video-sync sync takeoff.MP4 ridge.MP4 landing.MP4 --track flight.igc --json sync.json
```

Recordings can have gaps, overlap, or come from different cameras. You can
give them in any order. Session placement keeps accepted results unchanged.

The tool detects continuous chapters from sensor-clock junctions and exact
cumulative sample counters inside the files. Names do not establish continuity.
Confirmed chapters share one alignment, so a short chapter can use motion or
GPS evidence from its neighbours. Each chapter keeps its own result and local
timeline. Adjacent chapter boundaries are exactly equal, including clock drift.
A subset of a continuous recording can give a different alignment.

The tool first matches each file independently. A branch, a failed group solve,
a GPS time step, or a contradiction with an accepted independent mapping
keeps the independent results for that candidate shot. Each result has a `shot`
object with `single`, `joined`, or `ambiguous` status. Only `joined` means that
a shared shot alignment was applied.

After chapter matching, session placement uses container creation times and
validated recording clocks. It needs two accepted GPS, motion, or joined-shot
anchors from the same camera. One joined shot counts as one anchor. A short
clip can use these anchors without its own motion match. A session mapping
covers the whole clip, even outside the track. Its clock rate is 1, and its
uncertainty must not exceed 10 seconds.

Known camera serials form separate groups. Files without serials form one
separate group. Different cameras with matching clocks and no serials are a
remaining false-placement risk. Check the verification plots before use.

The output uses schema `3.0.0` with a `videos` list, even for one input. The
list has one entry per input, in input order. Each entry has its own status,
offset, drift, and confidence data. A failed or ambiguous recording does not
stop the others. `source.kind` identifies `gps`, `motion`, `shot`, or `session`
evidence. `method` is `gps`, `motion`, or `session`. For a joined shot, `method`
names the evidence that solved the shot. A session source names its supporting
inputs and keeps the independent rejection reason. An ambiguous batch result
has a session verdict of `insufficient` or `contradictory`, with a separate
message. Neither verdict supplies an offset.

Give each recording only once. A repeated path, including a symbolic link to
the same file, fails at each later position. An input with the same recording
identity and timeline as an earlier `ok` or `ambiguous` result also fails as a
duplicate. For example, two exports of the same telemetry are duplicates.

To inspect one recording or check its saved alignment:

```sh
gps-video-sync info ridge.MP4
gps-video-sync telemetry ridge.MP4 --out ridge.npz
gps-video-sync verify ridge.MP4 --track flight.igc \
  --result sync.json --output ridge.png --frames 10 120
```

`info`, `telemetry`, and `verify` each accept exactly one recording. Their times
are local to that file and start at zero. `--frames` values must be 0 or more
and less than the recording duration. `verify` selects the entry in the saved
result that has the same recording identity and local timeline. It draws a plot
only for an `ok` result. Run `gps-video-sync COMMAND --help` for all options.

Exit codes for `sync` and `verify`:

- `0`: all matches are `ok`.
- `2`: at least one match is `ambiguous` and no match is `failed`. An ambiguous
  result has no usable offset.
- `1`: at least one match is `failed`, or an input or run error occurs. Invalid
  command-line arguments also give `1`.

`sync` writes all per-file results before it returns a failure exit code.

Results, `info` output, telemetry exports, and cache files contain absolute
recording paths. Paths can show user names and directory names. Exports and
cache files can also contain camera GPS times and serial numbers. Results
contain recording dates and times. Remove this data before you share these files.

## Use the Python API

```python
from gps_video_sync import match_videos, read_track, read_video, synchronize

track = read_track("flight.igc")
results = match_videos(track, ["takeoff.MP4", "ridge.MP4", "landing.MP4"])
for result in results:
    print(result.video, result.status, result.offset_seconds)

telemetry = read_video("ridge.MP4", hz=10)
result = synchronize(track, telemetry)
```

`match_videos` returns one result per input, in input order. It joins confirmed
continuous chapters and places unresolved clips from camera-session anchors.
A file error gives a `failed` result for that file only. `read_video` reads one
recording. `synchronize` matches one recording and also accepts array-based
`Telemetry`.

To match exported telemetry, use `source="npz"` in the API or `--source npz` in
the CLI. NPZ exports from `telemetry --out` keep the original recording
identity, duration, media offset, and optional recording-clock and camera-start
evidence. `Telemetry.slice` clears both kinds of timing evidence. In a batch
result, `input_file` identifies the NPZ archive.

The result maps video seconds to UTC seconds from midnight of the track date:

```text
utc_seconds = video_seconds * clockRate + offset_seconds
```

## Supported cameras

The bundled test recordings are from a GoPro HERO11 Black. They test motion
alignment but contain no GPS data. GPS extraction and alignment have synthetic
tests only. GoPro documents compatible metadata for the other models below, but
this package has no recordings from them. Their extraction, alignment, and frame
verification are untested.

The reader needs a timed GPMF track with sample format `gpmd` in the original
video. It uses these fields:

- `GPS9` supplies UTC and fix status for GPS alignment. The older `GPS5` format
  needs `GPSU` for UTC and `GPSF` for fix status. The reader accepts only valid
  3D fixes. `GPS9` takes priority when both formats occur in a packet.
- `GYRO` and `ACCL` supply the motion features. Motion alignment works without
  camera GPS, within the limits below.
- The reader also extracts `GRAV`, `CORI`, and `IORI` when present. Alignment
  does not need these fields. The matcher estimates gravity from `ACCL`.

| Camera | GPS metadata for UTC alignment | `GYRO` + `ACCL` | `GRAV` + `CORI` + `IORI` | Compatibility / test status |
| --- | --- | --- | --- | --- |
| HERO5 Black | `GPS5` + `GPSU` + `GPSF` | Yes | Not documented | GPS and motion expected; untested |
| HERO5 Session | None | Yes | Not documented | Motion only expected; untested |
| HERO6 Black | `GPS5` + `GPSU` + `GPSF` | Yes | Not documented | GPS and motion expected; untested |
| HERO7 Black | `GPS5` + `GPSU` + `GPSF` | Yes | Not documented | GPS and motion expected; untested |
| HERO8 Black | `GPS5` + `GPSU` + `GPSF` | Yes | Yes | GPS and motion expected; untested |
| HERO9 Black | `GPS5` + `GPSU` + `GPSF` | Yes | Yes | GPS and motion expected; untested |
| HERO10 Black | `GPS5` + `GPSU` + `GPSF` | Yes | Yes | GPS and motion expected; untested |
| HERO10 Black Bones | None | Yes | Yes | Motion only expected; untested |
| HERO11 Black | `GPS9`, plus legacy `GPS5` | Yes | Yes | Motion tested with bundled recordings; GPS untested |
| HERO12 Black | None | Yes | Yes | Motion only expected; untested |
| HERO13 Black | `GPS9` | Yes | Yes | GPS and motion expected; untested |
| Fusion | Not documented in the cited table | Yes | Not documented | Motion metadata compatible; 360 workflow untested |
| MAX | `GPS5` + `GPSU` + `GPSF` | Yes | Yes | Metadata compatible; 360 workflow untested |
| MAX2 | `GPS9` | Yes | Yes | Metadata compatible; 360 workflow untested |
| MISSION 1 family | `GPS9` | Yes | Yes | Metadata compatible per GoPro; untested |

Use original, normal-speed recordings. Enable GPS on cameras that have it and
wait for a satellite lock before you record. Some clips can still lack usable
fixes. Edited, transcoded, or stitched exports can omit the metadata track.
Check each recording with `gps-video-sync info`.

The 360 camera rows cover sensor metadata only. Stitching, projection, and frame
extraction from native 360 files are untested.

The table does not cover Silver, White, Mini, or entry-level HERO models, HERO4
and earlier cameras, external GPS accessories, or special recording modes. A
model number or GPS chip alone does not establish compatibility.

Sources:

- [GoPro's model-by-model GPMF tables](https://github.com/gopro/gpmf-parser/blob/9a7150632892c7356c91145c889016f07b0ed48d/README.md#where-to-find-gpmf-data).
  The documented firmware includes HERO7 v1.8, HERO8 v2.5, and MAX v2.0.
- [Gyroflow's GoPro motion-data notes](https://github.com/gyroflow/gyroflow-docs/blob/main/docs/cams/gopro.md).

Firmware and recording mode can change the available streams. The reader checks
for metadata keys, not camera model names.

## Limits

GoPro extraction allows at most 1,000,000 resampled samples per recording.
Lower `--hz` if the recording duration in seconds multiplied by `--hz` exceeds
this limit.

Motion matching needs at least 90 seconds of usable overlap. The overlap must
also cover at least 65% of the shorter feature sequence. Each independent file
or confirmed continuous group needs enough varying motion. Repeated turns,
data gaps, or low motion can produce ambiguous or false matches. Check the plot
and distinctive events before you use an alignment. Short clips cannot establish
clock stability. Session placement uses other accepted recordings to place such
clips. A GPS UTC step blocks placement. A continuity candidate with no approved
common mapping cannot use session placement when it retains an accepted member.

See the [API and format reference](https://github.com/manuelseeger/gps-video-sync/blob/main/docs/reference.md)
for data contracts, plugins, telemetry formats, and matching limits.

## Run the tests

```sh
pip install -e '.[dev,verify]'
python -m pytest
python scripts/check_independent_videos.py
python scripts/check_continuous_videos.py
```

Run the tests from a repository checkout. Release archives exclude tests and
fixtures. The [bundled fixtures](https://github.com/manuelseeger/gps-video-sync/tree/main/tests/fixtures)
contain flight coordinates, times, and media.

- Extraction and alignment tests need no external program.
- Parity tests use the `ffprobe` marker and need `ffprobe` 9 or later.
- Frame tests need `ffmpeg` and Matplotlib.
- To skip integration tests, run `python -m pytest -m 'not integration'`.

`scripts/check_continuous_videos.py` needs the `verify` extra and `ffmpeg`.
It runs the API and the CLI on the bundled recordings with cold and warm
caches. It checks exact boundaries, reordered inputs, subsets, weak chapters,
non-unit drift, gaps, overlaps, counters, contradictory fits, and failures.
It also checks cache rebuilds, NPZ and JSON exports, crops, saved-result
selection, plots, and frames against direct FFmpeg seeks.

`scripts/check_independent_videos.py` checks independent motion crops and GPS
recordings with gaps, overlaps, shuffled inputs, and isolated failures. It also
checks exports and file-local frame extraction on the bundled videos.

## License

[MIT](https://github.com/manuelseeger/gps-video-sync/blob/main/LICENSE).
