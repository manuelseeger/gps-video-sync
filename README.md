# gps-video-sync

Align action-camera telemetry with an IGC flight track. Use the Python API or CLI
to calculate a video-to-UTC offset, check clock drift, and plot the alignment.
Camera GPS time is the primary reference. Motion matching is the fallback.

## Install

Requires Python 3.10 or later. Supported versions: 3.10–3.15.

```sh
pip install gps-video-sync
pip install "gps-video-sync[verify]"
```

For a source installation, run
`pip install .` or `pip install ".[verify]"` from the repository root.

GoPro MP4 and LRV extraction uses only the package's Python dependencies.
The native reader gets timing and file positions from MP4 sample tables.

To create verification plots, install the `verify` extra. Frame extraction also
requires `ffmpeg` on `PATH`.

## Supported cameras

The bundled test recordings are from a GoPro HERO11 Black. They test motion
alignment but contain no GPS data. Camera GPS extraction and alignment have
synthetic tests only. GoPro documents compatible metadata for the other models
below. This package has no test recordings from those models, so extraction,
alignment, and frame verification remain untested.

The reader needs a timed GPMF track with the sample format `gpmd` in the original
video. It uses these fields:

- `GPS9` supplies UTC and fix status for GPS alignment. The older `GPS5` format
  needs `GPSU` for UTC and `GPSF` for fix status. The reader accepts only valid
  3D fixes. `GPS9` takes priority when both formats occur in a packet.
- `GYRO` and `ACCL` supply the full set of motion features. Motion alignment can
  work without camera GPS. The overlap and motion limits below still apply.
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

GoPro documents GPS UTC and all five motion channels for HERO8, HERO9,
HERO10, HERO11, and HERO13 Black. The GPS fields differ by model, as listed above.
HERO12 Black and HERO10 Black Bones provide the five motion channels but no GPS UTC.
GoPro also documents the complete set for MAX, MAX2, and MISSION 1. Their
recording formats need separate tests. HERO5, HERO6, and HERO7 Black can supply
both alignment methods without the optional orientation channels.

Use original, normal-speed recordings. Enable GPS on cameras that have it and
wait for a satellite lock before recording. Some clips may still lack usable
fixes. Edited, transcoded, or stitched exports can omit the metadata track.
Check each recording with `gps-video-sync info`.

The 360 camera rows cover sensor metadata only. Stitching, projection, and
frame extraction from native 360 files remain untested.

The Black rows do not cover Silver, White, Mini, or entry-level HERO models.
This matrix does not confirm those variants, HERO4 and earlier cameras,
external GPS accessories, or special recording modes. A model number or GPS
chip alone does not establish compatibility.

The matrix uses these sources:

- [GoPro's model-by-model GPMF tables](https://github.com/gopro/gpmf-parser/blob/9a7150632892c7356c91145c889016f07b0ed48d/README.md#where-to-find-gpmf-data).
  The documented firmware includes HERO7 v1.8, HERO8 v2.5, and MAX v2.0.
- [Gyroflow's GoPro motion-data notes](https://github.com/gyroflow/gyroflow-docs/blob/main/docs/cams/gopro.md).

Firmware and recording mode can affect the available streams. The reader
checks for metadata keys, not camera model names.

## Align recordings

Match separate recordings against the same flight track:

```sh
gps-video-sync sync takeoff.MP4 ridge.MP4 landing.MP4 --track flight.igc --json sync.json
```

Each recording gets its own status, offset, drift, and confidence diagnostics.
The output uses schema `2.0.0` with a `videos` list, even for one input.
Input order does not change an individual match.
Recordings can have gaps, overlaps, and different camera clocks.
A failed or uncertain recording does not block results for other files.

To inspect one recording or check its saved alignment:

```sh
gps-video-sync info ridge.MP4
gps-video-sync telemetry ridge.MP4 --out ridge.npz
gps-video-sync verify ridge.MP4 --track flight.igc \
  --result sync.json --output ridge.png --frames 10 120
```

`info`, `telemetry`, and `verify` accept exactly one recording.
All video times are local to that file.
Plots and frame names identify the recording.
Frame times must be less than its duration.
Saved-result verification selects exactly one result by recording identity.

Each file matches independently. Split files are not joined automatically.
Motion matching needs enough varying motion in each file.

Results, telemetry exports, and caches contain recording paths.
They can also contain GPS locations and times.
Remove identifying data before you share these files.

Exit codes are `0` when all matches succeed, `2` when any is ambiguous and none
fail, and `1` when any fails or a run error occurs.
The command writes all per-file results before returning a match failure.
An ambiguous result has no usable offset.

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
Use `source="npz"` in the API or `--source npz` in the CLI to match exported
telemetry. Exports retain original recording identity and duration.
The batch result's `input_file` identifies the NPZ archive separately.

The result maps video seconds to UTC seconds from the track date's midnight:

```text
utc_seconds = video_seconds * clockRate + offset_seconds
```

Motion matching needs at least 90 seconds of usable overlap, and at least 65% of
the shorter feature sequence must overlap. Repeated turns,
data gaps, or low motion can produce ambiguous or false matches. Check the plot
and distinctive events before you use an alignment. Short clips cannot establish
clock stability.

See the [API and format reference](https://github.com/manuelseeger/gps-video-sync/blob/main/docs/reference.md) for data contracts,
plugins, telemetry formats, and matching limits.

## Run the tests

```sh
pip install -e '.[dev,verify]'
python -m pytest
python scripts/check_independent_videos.py
```

The integration tests use [bundled fixtures](https://github.com/manuelseeger/gps-video-sync/tree/main/tests/fixtures).
The fixtures contain flight coordinates, times, and media. Release archives
exclude tests and fixtures. Run the full test suite from a repository checkout.
Extraction and alignment tests need no external programs. Optional parity tests
use `ffprobe` 9 or later and have the `ffprobe` marker. Frame tests require `ffmpeg` and
Matplotlib. To run only unit tests, use `python -m pytest -m 'not integration'`.
The independent-video check requires the verification dependencies and `ffmpeg`.
It checks full per-file results for permutations and subsets, cold and warm
caches, exports, plots, and frames against direct per-file seeks.
Cropped motion and synthetic GPS recordings cover gaps, overlaps, and mixed failures.

## License

[MIT](https://github.com/manuelseeger/gps-video-sync/blob/main/LICENSE).
