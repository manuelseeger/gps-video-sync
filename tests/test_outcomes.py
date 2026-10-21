from gps_video_sync.models import (
    Accepted, Ambiguous, Failed, JoinedShot, OwnGps, SyncResult, TimeMapping,
    SessionInsufficient, SessionContradiction,
)


def test_mapping_is_authoritative_for_serialized_rate_and_source():
    result = SyncResult(Accepted(TimeMapping(120, .5, 1.001, OwnGps())),
                        drift={"clock_rate": .9})
    value = result.to_dict()
    assert (value["status"], value["offset_seconds"], value["clockRate"], value["method"]) == ("ok", 120, 1.001, "gps")
    assert value["source"] == {"kind": "gps"}
    assert value["uncertainty_seconds"] == .5
    assert result.mapping.utc_seconds(1000) == 1121


def test_joined_source_retains_solving_method_and_supports():
    result = SyncResult(Accepted(TimeMapping(150, 2, 1.001,
                        JoinedShot("motion", ("first.mp4", "second.mp4"), 1, 30))),
                        input_file="second.mp4")
    value = result.to_dict()
    assert value["method"] == "motion"
    assert value["source"]["kind"] == "shot"
    assert value["source"]["supporting"] == ["first.mp4"]
    assert value["shot"]["start_seconds"] == 30


def test_only_accepted_outcome_has_an_offset():
    for outcome, status, message in [
        (Ambiguous("Weak motion", SessionInsufficient("too_few_anchors")), "ambiguous",
         "Weak motion; session Fewer than two accepted recordings from this camera"),
        (Ambiguous("Weak motion", SessionContradiction("anchors_disagree", ("a.mp4", "b.mp4"))), "ambiguous",
         "Weak motion; session Accepted recordings disagree on the camera clock offset"),
        (Failed("No telemetry samples"), "failed", "No telemetry samples"),
    ]:
        value = SyncResult(outcome).to_dict()
        assert value["status"] == status
        assert value["message"] == message
        assert value["offset_seconds"] is None
        assert value["source"] is None
        assert value["clockRate"] is None
