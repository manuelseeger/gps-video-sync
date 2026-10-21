# gps-video-sync

Find each video's clock offset relative to an IGC GPS flight track. The offset
maps timestamps in the video to UTC times on the track, even when the camera's
date or time is wrong.

The tool uses camera GPS time when available and falls back to motion matching.
It also checks clock drift and can plot the alignment. In a batch, two accepted
recordings from the same camera can place clips which have otherwise weak evidence.

## Install

Requires Python 3.10 or later. Tests cover Python 3.10 to 3.15.

```sh
uv tool install gps-video-sync
```
or with pip

```sh
pip install gps-video-sync
```

## Align recordings

Match your recordings against an IGC flight track and save the results:

```sh
gps-video-sync sync takeoff.MP4 ridge.MP4 landing.MP4 --track flight.igc --json sync.json
```

Use one or more recordings, each listed once. Include all recordings from the
flight to help align short clips. File order does not matter.

Each recording gets a result in `sync.json`. `ok` means it has an alignment.
`ambiguous` means there is not enough reliable evidence. `failed` means the
recording could not be processed. Other recordings still get their results.

By default, `sync` does not change your recordings. To store accepted UTC start
dates in the MP4 headers, preview the changes, then apply them:

```sh
gps-video-sync sync takeoff.MP4 ridge.MP4 landing.MP4 --track flight.igc --in-place --dry-run
gps-video-sync sync takeoff.MP4 ridge.MP4 landing.MP4 --track flight.igc --in-place
```

This changes creation dates, not playback speed or media. Dates round to the
nearest whole second, with half seconds rounded toward the later second.
Ambiguous and failed alignments stay untouched. The command supports direct
local MP4 files with the default `gopro` source, not NPZ inputs, symlinks, or
hard-linked files. Each file update is atomic and creates no backup. The
[reference](docs/reference.md#in-place-utc-dates) describes write reports and
original camera-date preservation.

Run `gps-video-sync sync --help` for more options.
Results contain file paths and recording dates. Remove private data before sharing.

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
uv sync --extra dev --extra verify
uv run pytest
uv run python scripts/check_independent_videos.py
uv run python scripts/check_continuous_videos.py
```

Run the tests from a repository checkout. Release archives exclude tests and
fixtures. The [bundled fixtures](https://github.com/manuelseeger/gps-video-sync/tree/main/tests/fixtures)
contain flight coordinates, times, and media.

- Extraction and alignment tests need no external program.
- Parity tests use the `ffprobe` marker and need `ffprobe` 9 or later.
- Frame tests need `ffmpeg` and Matplotlib.
- To skip integration tests, run `uv run pytest -m 'not integration'`.

`scripts/check_continuous_videos.py` needs the `verify` extra and `ffmpeg`.
It runs the API and the CLI on the bundled recordings with cold and warm
caches. It checks exact boundaries, reordered inputs, subsets, weak chapters,
non-unit drift, gaps, overlaps, counters, contradictory fits, and failures.
It also checks cache rebuilds, NPZ and JSON exports, crops, saved-result
selection, plots, and frames against direct FFmpeg seeks.

`scripts/check_independent_videos.py` checks independent motion crops and GPS
recordings with gaps, overlaps, shuffled inputs, and isolated failures. It also
checks exports and file-local frame extraction on the bundled videos.

### Check an alignment visually

Check an alignment with a plot:

```sh
gps-video-sync verify ridge.MP4 --track flight.igc --result sync.json --output ridge.png
```

Plot output requires the `verify` extra. Install it with
`uv tool install 'gps-video-sync[verify]'`.

Run `uv run python scripts/check_in_place.py` to check UTC date updates on
temporary copies of the bundled clips. It checks header dates, unchanged media
and telemetry, original camera evidence, and repeat runs.

## License

[MIT](https://github.com/manuelseeger/gps-video-sync/blob/main/LICENSE).
