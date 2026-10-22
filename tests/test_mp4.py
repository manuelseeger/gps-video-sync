import io
import struct
import tracemalloc

import pytest

from gps_video_sync import mp4
from mp4_builder import Track, box, build, clock, full, table, track_box, video


def read(tmp_path, tracks=None, **kwargs):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(build([video(deltas=[3000]), Track()] if tracks is None else tracks, **kwargs))
    return mp4.read_timeline(path)


def test_timeline_limit_error_keeps_source_name(tmp_path, monkeypatch):
    path = tmp_path / "source.mp4"
    path.write_bytes(build([video(), Track()]))
    monkeypatch.setattr(mp4, "MAX_TRACKS", 0)
    with pytest.raises(mp4.LimitExceeded, match=r"^source\.mp4: track count limit exceeded$"):
        mp4.read_timeline(path)


def test_timeline_and_chunk_runs(tmp_path):
    result = read(tmp_path, [video(deltas=[2000]),
                            Track(deltas=[1001] * 5, payloads=[b"abc", b"12345", b"ab", b"1234", b"123456"],
                                  chunks=[2, 2, 1])])
    assert result.duration == 2
    assert [s.time for s in result.samples] == [0, 1.001, 2.002, 3.003, 4.004]
    assert [s.duration for s in result.samples] == [1.001] * 5
    assert [(s.offset, s.size) for s in result.samples] == [(9, 3), (12, 5), (17, 2), (19, 4), (23, 6)]
    with pytest.raises(AttributeError):
        result.duration = 3


@pytest.mark.parametrize("sizes,payloads", [
    ("stsz-fixed", [b"aa"] * 3), ("stsz", [b"a", b"bb", b"c" * 15]),
    ("stz2-4", [b"a", b"bb", b"c" * 15]), ("stz2-8", [b"a", b"b" * 255, b"ccc"]),
    ("stz2-16", [b"a", b"b" * 300, b"ccc"]),
])
def test_sizes(tmp_path, sizes, payloads):
    result = read(tmp_path, [video(), Track(deltas=[1000] * 3, sizes=sizes, payloads=payloads)])
    assert [s.size for s in result.samples] == [len(p) for p in payloads]
    assert result.samples[0].offset == 9
    assert result.samples[2].offset == 9 + len(payloads[0]) + len(payloads[1])


@pytest.mark.parametrize("kwargs", [{}, {"moov_first": True}, {"extended": True},
                                     {"to_end": "moov"}, {"to_end": "mdat", "moov_first": True}])
def test_box_forms(tmp_path, kwargs):
    result = read(tmp_path, **kwargs)
    assert result.duration == 3
    assert [(s.time, s.duration, s.size) for s in result.samples] == [(0, 1, 1)]


def test_sparse_co64(tmp_path):
    path = tmp_path / "sparse.mp4"
    offset = 5 << 30
    v = video(offsets=b"co64")
    g = Track(offsets=b"co64")
    moov = box(b"moov", clock(b"mvhd", 1000, 0) + track_box(v, [offset]) + track_box(g, [offset + 1]))
    with path.open("wb") as handle:
        handle.write(struct.pack(">I4sQ", 1, b"free", offset - 16))
        handle.seek(offset - 16)
        handle.write(struct.pack(">I4sQ", 1, b"mdat", 18) + b"xx" + moov)
    tracemalloc.start()
    result = mp4.read_timeline(path)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result.samples == (mp4.Sample(0, 1, offset + 1, 1),)
    assert peak < 16 << 20


@pytest.mark.parametrize("version", [0, 1])
def test_edits_inside_sample_and_versions(tmp_path, version):
    result = read(tmp_path, [video(deltas=[3000], version=version),
                            Track(deltas=[1001] * 3, edits=[(3000, 300)], version=version)], version=version)
    assert [s.time for s in result.samples] == [-.3, .701, 1.702]
    assert [s.duration for s in result.samples] == [1.001] * 3


def test_empty_edits_and_video_origin(tmp_path):
    result = read(tmp_path, [video(deltas=[1000], edits=[(500, -1), (1000, 0)]),
                            Track(deltas=[1000], edits=[(1000, -1), (2000, 0)])])
    assert result.duration == 1.5
    assert result.samples[0].time == 1
    result = read(tmp_path, [video(deltas=[100, 100, 100], ctts=[50, 150, -150], ctts_version=1), Track()])
    assert result.samples[0].time == -.05
    result = read(tmp_path, [video(deltas=[1000], ctts=[500], edits=[]), Track()])
    assert result.samples[0].time == -.5


def test_empty_edit_with_nonzero_media_start(tmp_path):
    result = read(tmp_path, [video(deltas=[1000]),
                            Track(deltas=[1000, 1000], edits=[(500, -1), (1000, 1000)])])
    assert result.samples == (mp4.Sample(.5, 1, 10, 1),)


def test_samples_wholly_before_media_edit_are_omitted(tmp_path):
    result = read(tmp_path, [video(deltas=[3000]),
                            Track(deltas=[1000] * 4, edits=[(3000, 1000)])])
    assert result.samples == (mp4.Sample(0, 1, 10, 1), mp4.Sample(1, 1, 11, 1),
                              mp4.Sample(2, 1, 12, 1))


def test_ctts_unsigned_and_reordering(tmp_path):
    result = read(tmp_path, [video(), Track(deltas=[10, 10, 20], ctts=[20, -10, -10], ctts_version=1)])
    assert [s.time for s in result.samples] == [0, .01, .02]
    assert [s.offset for s in result.samples] == [10, 11, 9]
    result = read(tmp_path, [video(), Track(ctts=[2**31 + 7])])
    assert result.samples[0].time == (2**31 + 7) / 1000


@pytest.mark.parametrize("v,kwargs,expected", [
    (video(deltas=[900], edits=[(500, -1), (1200, 200)]), {}, 1.7),
    (video(deltas=[900], duration=1200), {}, 1.2),
    (video(deltas=[900], duration=2**32 + 123, version=1), {"version": 1}, (2**32 + 123) / 1000),
    (video(deltas=[900], duration=0), {}, .9),
    (video(deltas=[900], duration=2**32 - 1), {}, .9),
    (video(deltas=[], duration=0), {"movie_duration": 1700}, 1.7),
    (video(deltas=[], duration=2**64 - 1, version=1), {"movie_duration": 2**32 + 1, "version": 1}, (2**32 + 1) / 1000),
])
def test_duration_fallback(tmp_path, v, kwargs, expected):
    assert read(tmp_path, [v], **kwargs).duration == expected


def test_different_scales(tmp_path):
    result = read(tmp_path, [video(scale=30000, deltas=[8999991], edits=[(8999991, 2002)]),
                            Track(edits=[(9000000, 300)])], movie_scale=30000)
    assert result.duration == 299.9997
    assert result.samples[0].time == -.3


def test_last_zero_delta_and_no_metadata(tmp_path):
    result = read(tmp_path, [video(deltas=[2000]), Track(deltas=[1000, 0], duration=1500)])
    assert [s.duration for s in result.samples] == [1, .5]
    result = read(tmp_path, [video()])
    assert result == mp4.Timeline(1, ())


def test_multiple_tracks_stable_order_and_unrelated(tmp_path):
    result = read(tmp_path, [video(), Track(payloads=[b"abc"]), Track(payloads=[b"de"]),
                            Track(handler=b"soun", format=b"mp4a", replacements={b"stts": full(b"stts", b"bad")}),
                            Track(handler=b"meta", format=b"fdsc")])
    assert [(s.time, s.offset, s.size) for s in result.samples] == [(0, 9, 3), (0, 12, 2)]


def test_metadata_read_does_not_touch_payload(tmp_path, monkeypatch):
    source = build([video(), Track(payloads=[b"secret"])], extra_top=box(b"free", b"unknown"))

    class Guard(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size < 64 << 10
            start = self.tell()
            assert not (start < 15 and start + size > 8)
            assert not (start < len(source) and start + size > len(source) - 7)
            return super().read(size)

    monkeypatch.setattr("pathlib.Path.open", lambda *args, **kwargs: Guard(source))
    result = mp4.read_timeline(tmp_path / "guarded.mp4")
    assert result == mp4.Timeline(1, (mp4.Sample(0, 1, 9, 6),))


@pytest.mark.parametrize("data,reason", [
    (b"", "no moov"), (b"123", "truncated"),
    (struct.pack(">I4s", 1, b"free") + b"short", "truncated"),
    (struct.pack(">I4s", 4, b"free"), "invalid box"),
    (box(b"moov", struct.pack(">I4s", 100, b"trak")), "truncated"),
    (box(b"moof") + build([video()]), "fragmented"),
    (build([video()]) + box(b"moof"), "fragmented"),
    (build([video()], extra_moov=box(b"mvex")), "fragmented"),
    (build([video()]) + box(b"moov"), "duplicate moov"),
    (build([Track()]), "no video"),
    (build([video(scale=0)]), "timescale"),
    (build([video(deltas=[], duration=0)]), "cannot determine"),
])
def test_structural_errors(tmp_path, data, reason):
    path = tmp_path / "broken.mp4"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=reason):
        mp4.read_timeline(path)


@pytest.mark.parametrize("replacement,reason", [
    ({b"stts": table(b"stts", [(2, 1000)], ">II")}, "stts sample count"),
    ({b"stts": table(b"stts", [(2**32 - 1, 1)], ">II")}, "stts sample count"),
    ({b"ctts": table(b"ctts", [(2**32 - 1, 1)], ">II")}, "ctts sample count"),
    ({b"stsz": full(b"stsz", struct.pack(">II", 1, 2**32 - 1))}, "sample count limit"),
    ({b"stsz": full(b"stsz", struct.pack(">II", 0, 2**31))}, "truncated"),
    ({b"stsc": table(b"stsc", [(2, 1, 1)], ">III")}, "first chunk"),
    ({b"stsc": table(b"stsc", [(1, 2, 1)], ">III")}, "chunk sample count"),
    ({b"stsc": table(b"stsc", [(1, 1, 2)], ">III")}, "description index"),
    ({b"stco": table(b"stco", [(0,)], ">I")}, "outside mdat"),
    ({b"stco": table(b"stco", [(9 + 1,)], ">I")}, "outside mdat"),
    ({b"stco": table(b"stco", [(2**32 - 1,)], ">I")}, "outside mdat"),
    ({b"co64": table(b"co64", [(9,)], ">Q")}, "one chunk"),
    ({b"stz2": full(b"stz2", struct.pack(">II", 12, 1)), b"stsz": None}, "stz2"),
    ({b"stz2": full(b"stz2", struct.pack(">II", 8, 1) + b"x")}, "one sample size"),
    ({b"stts": full(b"stts", struct.pack(">I", 1), version=2)}, "version"),
    ({b"stsd": None}, "missing stsd"),
])
def test_bad_tables(tmp_path, replacement, reason):
    with pytest.raises(ValueError, match=reason):
        read(tmp_path, [video(), Track(replacements=replacement)])


@pytest.mark.parametrize("edits", [[(500, 0), (500, 500)], [(1000, 0, 0, 0)],
                                   [(500, 0), (500, -1)], [(1000, -2)], [(1000, -1)]])
def test_unsupported_edits(tmp_path, edits):
    with pytest.raises(ValueError, match="unsupported edit"):
        read(tmp_path, [video(), Track(edits=edits)])


def test_short_visibility_and_partial_empty_edit_rejected(tmp_path):
    with pytest.raises(ValueError, match="differing edit visibility"):
        read(tmp_path, [video(deltas=[2000]), Track(edits=[(1000, 0)])])
    with pytest.raises(ValueError, match="partial sample across empty edit"):
        read(tmp_path, [video(), Track(edits=[(500, -1), (1000, 300)])])


def test_zero_duration_and_decreasing_chunk_runs(tmp_path):
    with pytest.raises(ValueError, match="positive duration"):
        read(tmp_path, [video(), Track(deltas=[1000, 0, 1000])])
    with pytest.raises(ValueError, match="stsc run"):
        read(tmp_path, [video(), Track(deltas=[1000] * 3, replacements={
            b"stsc": table(b"stsc", [(1, 1, 1), (3, 1, 1), (2, 1, 1)], ">III")})])
    with pytest.raises(ValueError, match="truncated"):
        read(tmp_path, [video(), Track(replacements={
            b"ctts": full(b"ctts", struct.pack(">I", 2**32 - 1))})])


def test_long_constant_video_run_does_not_expand(tmp_path):
    n = 9_000_000
    v = video(replacements={
        b"stts": table(b"stts", [(n, 1)], ">II"),
        b"stsz": full(b"stsz", struct.pack(">II", 1, n)),
        b"stsc": table(b"stsc", [(1, n, 1)], ">III"),
    }, duration=0)
    tracemalloc.start()
    result = read(tmp_path, [v, Track()])
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result.duration == 9000
    assert result.samples == (mp4.Sample(0, 1, 9, 1),)
    assert peak < 1 << 20


def test_multiple_mdat_and_crossing_payload_boundary(tmp_path):
    path = tmp_path / "multiple.mp4"
    metadata = box(b"moov", clock(b"mvhd", 1000, 0) +
                   track_box(video(offsets=b"stco"), [8]) +
                   track_box(Track(payloads=[b"abc"], offsets=b"stco"), [17]))
    path.write_bytes(box(b"mdat", b"x") + box(b"mdat", b"abc") + metadata)
    assert mp4.read_timeline(path).samples == (mp4.Sample(0, 1, 17, 3),)
    path.write_bytes(box(b"mdat", b"x") + box(b"mdat", b"ab") + metadata)
    with pytest.raises(ValueError, match="outside mdat"):
        mp4.read_timeline(path)


def test_repeated_padding_and_unknown_boxes_are_ignored(tmp_path):
    padding = box(b"free") + box(b"free") + box(b"zzzz") + box(b"zzzz")
    groups = box(b"sgpd") + box(b"sgpd")
    audio_groups = box(b"sbgp") + box(b"sbgp")
    result = read(tmp_path, [video(replacements={b"groups": groups}),
                            Track(replacements={b"padding": padding}),
                            Track(handler=b"soun", format=b"mp4a", replacements={b"groups": audio_groups})])
    assert result == mp4.Timeline(1, (mp4.Sample(0, 1, 9, 1),))


def test_duplicate_and_truncated_headers(tmp_path):
    path = tmp_path / "duplicate.mp4"
    for data, reason in [
        (build([video()], extra_moov=clock(b"mvhd", 1000, 0)), "one mvhd"),
        (build([video(replacements={b"stts": table(b"stts", [(1, 1000)], ">II") * 2})]), "duplicate stts"),
        (build([video()])[:-1], "truncated"),
        (box(b"moov", box(b"trak", b"123")), "one mvhd"),
        (build([video()]) + b"x", "truncated"),
    ]:
        path.write_bytes(data)
        with pytest.raises(ValueError, match=reason):
            mp4.read_timeline(path)


def test_descriptions_and_external_reference(tmp_path):
    with pytest.raises(ValueError, match="mixed sample descriptions"):
        read(tmp_path, [video(), Track(deltas=[1000, 1000], descriptions=[b"gpmd", b"abcd"],
                                      description_indices=[1, 2])])
    with pytest.raises(ValueError, match="external sample"):
        read(tmp_path, [video(), Track(reference=b"\0\0\0\0external")])
    with pytest.raises(ValueError, match="external sample"):
        read(tmp_path, [video(), Track(reference_kind=b"alis", reference=b"\0\0\0\0")])
    result = read(tmp_path, [video(reference_kind=b"alis"), Track(reference_kind=b"alis")])
    assert result.samples == (mp4.Sample(0, 1, 9, 1),)
    result = read(tmp_path, [video(), Track(descriptions=[b"abcd", b"gpmd"], description_indices=[2])])
    assert result.samples == (mp4.Sample(0, 1, 9, 1),)


@pytest.mark.parametrize("limit,value,kwargs,reason", [
    ("MAX_METADATA", 8, {}, "metadata limit"),
    ("MAX_BOXES", 2, {}, "box count limit"),
    ("MAX_TRACKS", 1, {}, "track count limit"),
    ("MAX_SAMPLES", 1, {}, "sample count limit"),
    ("MAX_OUTPUT", 0, {}, "output sample limit"),
    ("MAX_PAYLOAD", 0, {}, "payload limit"),
    ("MAX_EDITS", 0, {"tracks": [video(), Track(edits=[(2000, 0)])]}, "edit count limit"),
])
def test_resource_limits(tmp_path, monkeypatch, limit, value, kwargs, reason):
    monkeypatch.setattr(mp4, limit, value)
    with pytest.raises(ValueError, match=reason):
        read(tmp_path, **kwargs)
