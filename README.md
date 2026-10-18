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

## Align a recording

Pass video chapters in any argument order:

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

The GoPro reader resolves recording order from usable GPS UTC or one common
STMP sensor clock. Filenames and file modification times do not establish order.
Duplicate inputs, overlapping chapters, unexplained gaps, and uncertain clock
evidence give an input error. Multiple chapters without a common usable clock
also give an error. Motion matching does not recover chapter order.

A single chapter needs no ordering clock and can use motion matching without GPS.
STMP is relative camera time, not UTC or proof of camera identity. Separate
recordings with indistinguishable continuous clocks cannot be separated.
Gaps below the clock tolerance can also be indistinguishable from chapter splits.
See [chapter order limits](docs/reference.md#chapter-order) for the thresholds.

All video times use the resolved concatenated chapter timeline. The API, CLI,
cache, NPZ and JSON exports, plots, and frame extraction use the same order.
Frame times refer to this timeline, not argument positions. Run
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

See the [API and format reference](https://github.com/manuelseeger/gps-video-sync/blob/main/docs/reference.md) for data contracts,
plugins, telemetry formats, and matching limits.

## Run the tests

```sh
pip install -e '.[dev,verify]'
python -m pytest
python scripts/check_chapter_order.py
```

The integration tests use [bundled fixtures](https://github.com/manuelseeger/gps-video-sync/tree/main/tests/fixtures).
The fixtures contain flight coordinates, times, and media. Release archives
exclude tests and fixtures. Run the full test suite from a repository checkout.
Extraction and alignment tests need no external programs. Optional parity tests
use `ffprobe` 9 or later and have the `ffprobe` marker. Frame tests require `ffmpeg` and
Matplotlib. To run only unit tests, use `python -m pytest -m 'not integration'`.
The chapter-order check requires the verification dependencies and `ffmpeg`.
It checks the real no-GPS fixtures in both orders, cold and shared caches,
renamed files, reversed modification times, exports, plots, and boundary frames.

## License

[MIT](https://github.com/manuelseeger/gps-video-sync/blob/main/LICENSE).
