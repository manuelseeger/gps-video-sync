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

The bundled test recordings are from a **GoPro HERO11 Black**. Other models
below have compatible metadata according to GoPro's documentation, but have not
been tested with this package. Metadata availability is not an end-to-end test
of extraction, alignment, or frame verification.

The built-in reader needs a timed GPMF (`gpmd`) track in the original video:

- **GPS alignment:** `GPS9` supplies UTC and fix status directly. The older
  `GPS5` format needs `GPSU` (UTC) and `GPSF` (fix status). Only valid 3D fixes
  are accepted. `GPS9` takes priority when both formats occur in a packet.
- **Motion alignment:** `GYRO` and `ACCL` supply the full set of motion features.
  This path can work without camera GPS, subject to the overlap and motion
  limits below.
- **Additional telemetry:** `GRAV`, `CORI`, and `IORI` are extracted when present.
  They are not required for alignment. The matcher derives its gravity estimate
  from `ACCL`, not `GRAV`.

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
| **HERO11 Black** | **`GPS9`, plus legacy `GPS5`** | **Yes** | **Yes** | **Tested with bundled recordings** |
| HERO12 Black | None | Yes | Yes | Motion only expected; untested |
| HERO13 Black | `GPS9` | Yes | Yes | GPS and motion expected; untested |
| Fusion | `GPS5` + `GPSU` + `GPSF` | Yes | Not documented | Metadata compatible; 360 workflow untested |
| MAX | `GPS5` + `GPSU` + `GPSF` | Yes | Yes | Metadata compatible; 360 workflow untested |
| MAX2 | `GPS9` | Yes | Yes | Metadata compatible; 360 workflow untested |
| MISSION 1 family | `GPS9` | Yes | Yes | Metadata compatible per GoPro; untested |

**Same extracted channel set as HERO11:** HERO8–10 Black and HERO13 Black
provide GPS UTC plus all five motion channels. HERO12 Black and HERO10 Black
Bones provide the five motion channels but no GPS UTC. MAX, MAX2, and MISSION 1
also document the complete channel set; their recording formats and workflows
need separate tests. HERO5–7 Black can supply both alignment paths without the
optional orientation channels.

Use original, normal-speed recordings. Enable GPS on cameras that have it and
wait for a satellite lock before recording. A GPS-capable model does not ensure
usable fixes in every clip. Edited, transcoded, or stitched exports can omit the
metadata track. For 360 cameras, this table describes sensor metadata only; it
does not promise support for stitching, projection, or frame extraction from
native 360 files. Supply chapters in recording order and check each recording
with `gps-video-sync info`.

Model names matter: the Black rows do not cover Silver, White, Mini, or the
entry-level HERO models. Those variants, HERO4 and earlier cameras, external
GPS accessories, and special recording modes are not confirmed by this matrix.
Do not assume compatibility from a model number or the presence of a GPS chip.

Sources: [GoPro's model-by-model GPMF tables](https://github.com/gopro/gpmf-parser/blob/9a7150632892c7356c91145c889016f07b0ed48d/README.md#where-to-find-gpmf-data)
(documented firmware includes HERO7 v1.8, HERO8 v2.5, and MAX v2.0), and
[Gyroflow's GoPro motion-data notes](https://github.com/gyroflow/gyroflow-docs/blob/main/docs/cams/gopro.md).
Firmware and recording mode can affect the available streams. The implementation
uses the keys above, not a camera-model allowlist.

## Align a recording

Pass video chapters in recording order:

```sh
gps-video-sync sync GOPR0001.MP4 GP010001.MP4 --track flight.igc --json sync.json
```

To inspect telemetry or check the saved alignment:

```sh
gps-video-sync info GOPR0001.MP4 GP010001.MP4
gps-video-sync telemetry GOPR0001.MP4 GP010001.MP4 --out telemetry.npz
gps-video-sync verify GOPR0001.MP4 GP010001.MP4 --track flight.igc \
  --result sync.json --output alignment.png --frames 10 120
```

All video times use the concatenated chapter timeline. Run
`gps-video-sync COMMAND --help` for command options.

Results, `info` output, telemetry exports, and the cache contain absolute
chapter paths. Remove the `chapters` data before you share these files.

Exit codes are `0` for success, `2` for ambiguous alignment, and `1` for input or
external-tool errors. An ambiguous result has no usable offset.

## Use the Python API

```python
from gps_video_sync import read_track, read_video, synchronize

track = read_track("flight.igc")
telemetry = read_video(["GOPR0001.MP4", "GP010001.MP4"], hz=10)
result = synchronize(track, telemetry)
if result.status == "ok":
    print(result.to_dict())
```

The result maps video seconds to UTC seconds from the track date's midnight:

```text
utc_seconds = video_seconds * clockRate + offset_seconds
```

Motion matching needs at least 90 seconds of usable overlap, and at least 65% of
the shorter feature sequence must overlap. Repeated turns,
data gaps, or low motion can produce ambiguous or false matches. Check the plot
and distinctive events before you use an alignment. Short clips cannot establish
clock stability.

## Run the tests

```sh
pip install -e '.[dev,verify]'
python -m pytest
```

The integration tests use [bundled fixtures](https://github.com/manuelseeger/gps-video-sync/tree/main/tests/fixtures).
The fixtures contain flight coordinates, times, and media. Release archives
exclude tests and fixtures. Run the full test suite from a repository checkout.
Extraction and alignment tests need no external programs. Optional parity tests
use `ffprobe` 9 or later and have the `ffprobe` marker. Frame tests require `ffmpeg` and
Matplotlib. To run only unit tests, use `python -m pytest -m 'not integration'`.

## Releases

See the [release procedure](https://github.com/manuelseeger/gps-video-sync/blob/main/docs/releasing.md)
for archive checks, TestPyPI, release approval, and Trusted Publishing.

## License

[MIT](https://github.com/manuelseeger/gps-video-sync/blob/main/LICENSE).
