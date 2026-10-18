# gps-video-sync

Align action-camera telemetry with an IGC flight track. The tool calculates a
video-to-UTC offset, checks clock drift, and plots the alignment. It uses camera
GPS time when available and falls back to motion matching.

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

Match separate recordings against one flight track:

```sh
gps-video-sync sync takeoff.MP4 ridge.MP4 landing.MP4 --track flight.igc --json sync.json
```

The output uses schema `2.0.0` with a `videos` list, even for one input. Each
recording matches independently and has its own status, offset, drift, and
confidence diagnostics. Input order does not change a match between distinct
recording timelines. Split files are not joined. A failed or uncertain recording
does not block the others.

Supply each recording timeline once. Repeated paths or exports of the same
timeline receive a per-file duplicate failure after the first accepted input.

Inspect one recording or check its saved alignment:

```sh
gps-video-sync info ridge.MP4
gps-video-sync telemetry ridge.MP4 --out ridge.npz
gps-video-sync verify ridge.MP4 --track flight.igc \
  --result sync.json --output ridge.png --frames 10 120
```

`info`, `telemetry`, and `verify` accept exactly one recording. Times are local
to that file, and `--frames` values must be less than its duration. `verify`
selects the saved result by recording identity and local timeline.

Exit codes:

- `0`: all matches succeed.
- `2`: at least one match is ambiguous and none fail. An ambiguous result has no usable offset.
- `1`: at least one match fails, or a run error occurs.

The command writes all per-file results before it returns a match failure.

Results, telemetry exports, and caches contain recording paths. They can also
contain GPS locations and times. Remove identifying data before you share them.

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

`read_video` reads one recording. `synchronize` also accepts array-based
`Telemetry`. `match_videos` returns results in input order.

To match exported telemetry, use `source="npz"` in the API or `--source npz` in
the CLI. Exports keep the original recording identity and duration. The batch
result's `input_file` identifies the NPZ archive.

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
- `GRAV`, `CORI`, and `IORI` are extracted when present. Alignment does not need
  them. The matcher estimates gravity from `ACCL`.

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
also cover at least 65% of the shorter feature sequence. Each file needs enough
varying motion. Repeated turns, data gaps, or low motion can produce ambiguous
or false matches. Check the plot and distinctive events before you use an
alignment. Short clips cannot establish clock stability.

See the [API and format reference](https://github.com/manuelseeger/gps-video-sync/blob/main/docs/reference.md)
for data contracts, plugins, telemetry formats, and matching limits.

## Run the tests

```sh
pip install -e '.[dev,verify]'
python -m pytest
python scripts/check_independent_videos.py
```

Run the tests from a repository checkout. Release archives exclude tests and
fixtures. The [bundled fixtures](https://github.com/manuelseeger/gps-video-sync/tree/main/tests/fixtures)
contain flight coordinates, times, and media.

- Extraction and alignment tests need no external program.
- Parity tests (marker `ffprobe`) need `ffprobe` 9 or later.
- Frame tests need `ffmpeg` and Matplotlib.
- To skip integration tests, run `python -m pytest -m 'not integration'`.

`scripts/check_independent_videos.py` needs the `verify` extra and `ffmpeg`. It
compares per-file results with direct per-file runs across input permutations
and subsets, cold and warm caches, exports, plots, and frames. Cropped motion
recordings and synthetic GPS recordings cover gaps, overlaps, and mixed failures.

## License

[MIT](https://github.com/manuelseeger/gps-video-sync/blob/main/LICENSE).
