# Test fixtures

`flight.igc` and the two MP4 files provide a real-media regression case for
GoPro extraction, motion matching, CLI output, and frame extraction across a
chapter boundary. Tests read these files without changes and use temporary caches.

## Media

The clips contain consecutive excerpts from two GoPro LRV chapters:

| File | Source start | Duration |
| --- | ---: | ---: |
| `chapter-01.mp4` | 1814.112 s in the first chapter | 299.999700 s |
| `chapter-02.mp4` | 0 s in the next chapter | 300.033067 s |

Video is re-encoded as 96 × 54 H.264 at CRF 38. GPMF packets are copied without
changes. Audio, timecode, and container metadata are omitted. The clips contain
GYRO, ACCL, GRAV, CORI, and IORI channels, with no camera GPS observations.
The media totals about 4.4 MiB.

## Track

The IGC file retains the flight's fix times and motion. Identifying headers are
removed, the date is set to 2020-01-01, and longitude is shifted ten degrees west.
The longitude shift preserves relative geometry without publishing the recorded
location. The file is test data, not a navigation record.

## Expected results

The first clip starts at track-relative UTC second 36160.112, with a two-second
alignment tolerance. Each chapter and the combined recording produce accepted
motion matches. Thirty-second crops are ambiguous. The three-minute crop at
video second 240 also has competing matches and returns no offset.

The fixture does not establish general accuracy across cameras or mount types.
Its duration is too short to test clock drift detection. Synthetic tests cover
camera GPS timestamps and longer clock drift cases.
