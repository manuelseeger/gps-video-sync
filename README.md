# igc-video-sync

Align action-camera telemetry with an IGC flight track. Use the Python API or CLI
to calculate a video-to-UTC offset, check clock drift, and plot the alignment.
Camera GPS time is the primary reference. Motion matching is the fallback.

## Install

Requires Python 3.10 or later. From the repository root:

```sh
pip install .
```

GoPro video input requires [ffprobe](https://ffmpeg.org/ffprobe.html) at runtime.
Install it through your operating system's FFmpeg package and make sure that
`ffprobe` is on `PATH`. The Python package does not install it. Core matching
with existing telemetry or NPZ input does not require `ffprobe`.

To create verification plots, install `'.[verify]'`. Frame extraction also
requires `ffmpeg` on `PATH`.

## Align a recording

Pass video chapters in recording order:

```sh
igc-video-sync sync GOPR0001.MP4 GP010001.MP4 --track flight.igc --json sync.json
```

To inspect telemetry or check the saved alignment:

```sh
igc-video-sync info GOPR0001.MP4 GP010001.MP4
igc-video-sync telemetry GOPR0001.MP4 GP010001.MP4 --out telemetry.npz
igc-video-sync verify GOPR0001.MP4 GP010001.MP4 --track flight.igc \
  --result sync.json --output alignment.png --frames 10 120
```

All video times use the concatenated chapter timeline. Run
`igc-video-sync COMMAND --help` for command options.

Exit codes are `0` for success, `2` for ambiguous alignment, and `1` for input or
external-tool errors. An ambiguous result has no usable offset.

## Use the Python API

```python
from igc_video_sync import read_track, read_video, synchronize

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

Motion matching needs at least 90 seconds of usable overlap. Repeated turns,
data gaps, or low motion can produce ambiguous or false matches. Check the plot
and distinctive events before you use an alignment. Short clips cannot establish
clock stability.

See the [API and format reference](docs/reference.md) for data contracts,
plugins, telemetry formats, and matching limits.

## Run the tests

```sh
pip install -e '.[dev,verify]'
python -m pytest
```

The integration tests use [bundled fixtures](tests/fixtures/README.md).
They require `ffprobe`. Frame tests also require `ffmpeg` and Matplotlib.
To run only unit tests, use `python -m pytest -m 'not integration'`.

## License

[MIT](LICENSE).
