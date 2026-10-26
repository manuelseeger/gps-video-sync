from datetime import date, datetime, timezone
import json

import numpy as np
import pytest

from gps_video_sync import GpsTimingPolicy, Telemetry, Track, synchronize

EPOCH = 1767225600.


def solve(t, deviation=0., *, duration=600., policy=None, epoch=EPOCH):
    origin = datetime.fromtimestamp(epoch, timezone.utc).date()
    track = Track(np.array([30000., 50000.]), np.ones(2), np.ones(2), np.ones(2), origin)
    telemetry = Telemetry(np.arange(0., duration or 600., 1.), {}, duration=duration,
                          gps_time=np.asarray(t), gps_utc=epoch + 30010 + t + deviation)
    return synchronize(track, telemetry, gps_policy=policy or GpsTimingPolicy())


BOUNDED = dict(max_uncertainty_seconds=1., allow_bounded_discontinuities=True)


@pytest.mark.parametrize('budget', [True, 0, -1, float('nan'), float('inf'), '1'])
def test_invalid_policy(budget):
    with pytest.raises(ValueError):
        GpsTimingPolicy(max_uncertainty_seconds=budget)


def test_policy_accepts_positive_budgets_beyond_default_range():
    assert solve(np.arange(600.), policy=GpsTimingPolicy(max_uncertainty_seconds=.1)).gps.reason_codes == ('whole_video_uncertainty_exceeded',)
    assert solve(np.arange(600.), policy=GpsTimingPolicy(max_uncertainty_seconds=10.)).gps.verdict == 'accepted'
    with pytest.raises(ValueError):
        GpsTimingPolicy(allow_bounded_discontinuities=1)


def test_small_step_keeps_detection_and_estimates_strict_rejection():
    t = np.arange(600.)
    delta = .2 * (t >= 300)
    strict = solve(t, delta)
    bounded = solve(t, delta, policy=GpsTimingPolicy(**BOUNDED))
    assert strict.gps.verdict == 'rejected'
    assert strict.gps.reason_codes == ('discontinuity_not_allowed',)
    assert strict.gps.window_step_detected == bounded.gps.window_step_detected == True
    assert bounded.gps.verdict == 'accepted'
    assert .5 <= bounded.uncertainty_seconds <= 1.
    assert strict.gps.fit.envelope.whole_video_seconds == bounded.uncertainty_seconds
    assert bounded.to_dict()['selection'] == {'motion_attempted': False, 'selected_method': 'gps'}
    assert strict.to_dict()['selection'] == {'motion_attempted': True, 'selected_method': 'none'}
    json.dumps(strict.to_dict(), allow_nan=False)


@pytest.mark.parametrize('where', ['prefix', 'suffix', 'middle', 'gap'])
def test_large_steps_cannot_be_hidden_by_clipping(where):
    t = np.arange(600.)
    delta = np.zeros(600)
    if where == 'prefix':
        delta[:2] = 3
    elif where == 'suffix':
        delta[-2:] = 3
    else:
        delta[300:] = 3
    if where == 'gap':
        t[300:] += 100
    result = solve(t, delta, duration=701., policy=GpsTimingPolicy(max_uncertainty_seconds=100., allow_bounded_discontinuities=True))
    assert result.gps.verdict == 'rejected'
    assert 'non_affine_excursion_too_large' in result.gps.reason_codes
    assert result.gps.blocks_transfer


def test_isolated_outlier_and_short_plateau_have_distinct_safety_sets():
    t = np.arange(600.)
    delta = np.zeros(600)
    delta[100] = 3
    single = solve(t, delta)
    assert single.gps.verdict == 'accepted'
    assert single.gps.isolated_outlier_count == 1
    delta[101] = 3
    plateau = solve(t, delta, policy=GpsTimingPolicy(**BOUNDED))
    assert plateau.gps.verdict == 'rejected'
    assert plateau.gps.isolated_outlier_count == 0
    assert plateau.gps.fit.envelope.residual_max_seconds >= 2.9


def test_duration_and_unsampled_tails():
    t = np.arange(100., 200.)
    unknown = solve(t, duration=None)
    assert 'duration_unknown' in unknown.gps.reason_codes
    short = solve(t, duration=201.)
    long = solve(t, duration=10000.)
    assert short.gps.verdict == 'accepted'
    assert long.gps.verdict == 'rejected'
    assert long.gps.fit.envelope.end_seconds > short.gps.fit.envelope.end_seconds
    assert long.gps.fit.envelope.whole_video_seconds > 2


@pytest.mark.parametrize('gap', [False, True])
def test_incompatible_local_rates_rejected(gap):
    t = np.arange(1200.)
    delta = np.where(t < 600, -.0008*t, -.48 + .0008*(t-600))
    if gap:
        t[600:] += 100
    result = solve(t, delta, duration=1301., policy=GpsTimingPolicy(max_uncertainty_seconds=100., allow_bounded_discontinuities=True))
    assert result.gps.verdict == 'rejected'
    assert 'rate_regime_inconsistent' in result.gps.reason_codes
    assert result.gps.rate_regime_change_detected


@pytest.mark.parametrize('where,sign,endpoint,truth', [
    ('suffix', 1, 2000., 32012.7),
    ('suffix', -1, 2000., 32007.3),
    ('prefix', 1, 0., 30007.3),
    ('prefix', -1, 0., 30012.7),
])
@pytest.mark.parametrize('gap', [False, True])
def test_short_endpoint_rate_regime_cannot_bound_extrapolated_truth(where, sign, endpoint, truth, gap):
    if where == 'suffix':
        t = np.r_[np.arange(1000. if gap else 1100.), np.arange(1100., 1200.)]
        delta = sign*.003*np.maximum(t-1100., 0.)
    else:
        t = np.r_[np.arange(800., 900.), np.arange(1000. if gap else 900., 2000.)]
        delta = sign*.003*np.minimum(t-900., 0.)
    result = solve(t, delta, duration=2000.)
    assert abs(result.gps.fit.mapping.utc_seconds(endpoint)-truth) == pytest.approx(2.7)
    assert result.gps.verdict == 'rejected'
    assert 'rate_regime_inconsistent' in result.gps.reason_codes
    assert result.gps.rate_regime_change_detected
    assert result.mapping is None


@pytest.mark.parametrize('where,sign,transition,endpoint,truth', [
    ('suffix', 1, 1100., 2000., 32012.7),
    ('suffix', -1, 1100., 2000., 32007.3),
    ('prefix', 1, 900., 0., 30007.3),
    ('prefix', -1, 900., 0., 30012.7),
    ('suffix', 1, 1106.7, 2000., 32012.6799),
    ('suffix', -1, 1106.7, 2000., 32007.3201),
    ('prefix', 1, 893.3, 0., 30007.3201),
    ('prefix', -1, 893.3, 0., 30012.6799),
])
@pytest.mark.parametrize('amplitude', [.049, .0499999, .05])
@pytest.mark.parametrize('epoch', [EPOCH, 253370678400.])
def test_noisy_endpoint_rate_regime_keeps_long_window_evidence(where, sign, transition, endpoint, truth, amplitude, epoch):
    t = np.arange(1200.) if where == 'suffix' else np.arange(800., 2000.)
    ramp = np.maximum(t-transition, 0.) if where == 'suffix' else np.minimum(t-transition, 0.)
    result = solve(t, sign*.003*ramp+amplitude*(-1.)**t, duration=2000., epoch=epoch)
    expected_rounding = abs(np.spacing(epoch+30010+t)).max()+abs(np.spacing(t)).max()+1e-8
    assert result.gps.timestamp_rounding_seconds == expected_rounding
    assert result.gps.to_dict()['effective_noise_radius_seconds'] == .05+expected_rounding
    assert abs(result.gps.fit.mapping.utc_seconds(endpoint)-truth) > 2.6
    assert result.gps.fit.envelope.whole_video_seconds < 2.
    assert result.gps.verdict == 'rejected'
    assert result.gps.reason_codes == ('rate_regime_inconsistent',)
    assert result.gps.rate_regime_change_detected
    assert result.gps.blocks_transfer
    assert result.mapping is None


def test_affine_feasibility_checks_observations_beyond_seed_extremes():
    from gps_video_sync.gps_timing import _affine_consistency, _Consistency

    t = np.arange(11.)
    delta = .1*t
    assert _affine_consistency(t, delta) is _Consistency.CONSISTENT
    delta[5] = .7
    assert _affine_consistency(t, delta) is _Consistency.INCONSISTENT


@pytest.mark.parametrize('failure', ['limit', 'unbounded', 'numerical', 'nonfinite', 'unverified', 'exception'])
def test_unresolved_affine_feasibility_rejects_instead_of_dropping_evidence(monkeypatch, failure):
    from types import SimpleNamespace
    import gps_video_sync.gps_timing as timing

    def failed_solver(*args, **kwargs):
        if failure == 'exception':
            raise RuntimeError('numerical solver failure')
        status = {'limit': 1, 'unbounded': 3, 'numerical': 4}.get(failure, 0)
        candidate = [np.nan, 0.] if failure == 'nonfinite' else [0., 0.]
        return SimpleNamespace(status=status, x=np.array(candidate))

    t = np.arange(1200.)
    delta = .003*np.maximum(t-1100., 0.)+.049*(-1.)**t
    control = solve(t, delta, duration=2000.)
    assert control.gps.reason_codes == ('rate_regime_inconsistent',)
    monkeypatch.setattr(timing, 'linprog', failed_solver)
    result = solve(t, delta, duration=2000.)
    assert result.gps.verdict == 'rejected'
    assert result.gps.reason_codes == ('clock_model_unbounded',)
    assert not result.gps.rate_regime_change_detected
    assert result.gps.blocks_transfer
    assert result.mapping is None
    json.dumps(result.to_dict(), allow_nan=False)


@pytest.mark.parametrize('position', [10., 75., 300., 425., 590.])
@pytest.mark.parametrize('sign', [-1, 1])
def test_quarter_second_step_keeps_detection_and_bounded_acceptance(position, sign):
    t = np.arange(600.)
    delta = sign*.25*(t >= position)
    strict = solve(t, delta)
    bounded = solve(t, delta, policy=GpsTimingPolicy(**BOUNDED))
    assert strict.gps.verdict == 'rejected'
    assert strict.gps.reason_codes == ('discontinuity_not_allowed',)
    assert strict.gps.window_step_detected == bounded.gps.window_step_detected == True
    assert strict.gps.gap_rate_mismatch_detected == bounded.gps.gap_rate_mismatch_detected == False
    assert strict.gps.rate_regime_change_detected == bounded.gps.rate_regime_change_detected == False
    assert bounded.gps.verdict == 'accepted'
    assert .5 <= bounded.uncertainty_seconds <= 1.
    assert strict.gps.fit.envelope == bounded.gps.fit.envelope
    assert np.max(abs(bounded.mapping.utc_seconds(t)-(30010+t+delta))) <= bounded.uncertainty_seconds


@pytest.mark.parametrize('sign', [-1, 1])
def test_same_rate_step_still_rejects_when_whole_video_budget_fails(sign):
    t = np.arange(600.)
    result = solve(t, sign*.25*(t >= 200.), policy=GpsTimingPolicy(**BOUNDED))
    assert result.gps.verdict == 'rejected'
    assert result.gps.reason_codes == ('whole_video_uncertainty_exceeded',)
    assert result.gps.window_step_detected
    assert not result.gps.rate_regime_change_detected
    assert 1. < result.gps.fit.envelope.whole_video_seconds < 1.01


@pytest.mark.parametrize('position', [20., 75., 200., 300., 425., 580.])
@pytest.mark.parametrize('sign', [-1, 1])
@pytest.mark.parametrize('noise', ['alternating', 'smooth'])
def test_same_rate_step_with_bounded_noise_does_not_create_rate_evidence(position, sign, noise):
    t = np.arange(600.)
    jitter = .05*(-1.)**t if noise == 'alternating' else .05*np.sin(t/20.)
    delta = .001*t+sign*.25*(t >= position)+jitter
    result = solve(t, delta, policy=GpsTimingPolicy(max_uncertainty_seconds=2., allow_bounded_discontinuities=True))
    assert result.gps.verdict == 'accepted'
    assert not result.gps.rate_regime_change_detected
    assert result.gps.fit.envelope.non_affine_excursion_seconds <= .5
    assert np.max(abs(result.mapping.utc_seconds(t)-(30010+t+delta))) <= result.uncertainty_seconds


@pytest.mark.parametrize('drift', [-.001, -.0003, 0., .0003, .001])
@pytest.mark.parametrize('noise', ['alternating', 'smooth'])
@pytest.mark.parametrize('epoch', [EPOCH, 253370678400.])
def test_quiet_steady_drift_with_bounded_noise_has_one_rate(drift, noise, epoch):
    t = np.arange(1200.)
    jitter = .05*(-1.)**t if noise == 'alternating' else .05*np.sin(t/20.)
    result = solve(t, drift*t+jitter, duration=1200., epoch=epoch)
    assert result.gps.verdict == 'accepted'
    assert not result.gps.rate_regime_change_detected
    assert not result.gps.discontinuity_detected
    assert result.mapping.clock_rate == pytest.approx(1.+drift, abs=.0003)
    assert np.max(abs(result.mapping.utc_seconds(t)-(30010+(1.+drift)*t+jitter))) <= result.uncertainty_seconds


def test_steady_drift_residuals_describe_returned_mapping():
    t = np.arange(3600.)
    result = solve(t, .001*t, duration=3600.)
    assert result.gps.verdict == 'accepted'
    assert result.mapping.clock_rate == pytest.approx(1.001)
    assert result.gps.fit.envelope.residual_max_seconds < .00001
    assert result.offset_seconds == pytest.approx(30010)


def test_unit_rate_residuals_include_unselected_drift():
    t = np.arange(600.)
    result = solve(t, .0003*t)
    assert result.mapping.clock_rate == 1.
    assert result.gps.fit.envelope.residual_max_seconds == pytest.approx(.08985, abs=1e-5)


def test_outside_duration_and_gaps_have_structured_support():
    result = solve(np.arange(600.), duration=500.)
    assert 'observations_outside_duration' in result.gps.reason_codes
    t = np.r_[np.arange(100.), np.arange(200., 300.)]
    result = solve(t, .2*(t >= 200), duration=300., policy=GpsTimingPolicy(**BOUNDED))
    assert result.gps.verdict == 'accepted'
    assert result.gps.fit.envelope.gaps == ((99., 200.),)
    assert result.gps.fit.envelope.whole_video_seconds >= .5
    assert len(result.gps.fit.envelope.runs) == 2


@pytest.mark.parametrize('seed', range(5))
def test_sparse_noisy_rate_evidence_does_not_invent_rate_changes(seed):
    t = np.r_[np.arange(6.), np.arange(100., 106.)]
    noise = np.random.default_rng(seed).uniform(-.05, .05, len(t))
    result = solve(t, noise, duration=106., policy=GpsTimingPolicy(max_uncertainty_seconds=100.))
    assert not result.gps.rate_regime_change_detected
    assert 'rate_regime_inconsistent' not in result.gps.reason_codes
    assert result.gps.fit.envelope.residual_max_seconds < .1


def test_batch_and_join_forward_policy_without_reset(tmp_path):
    from gps_video_sync import RecordingClock, match_videos
    from gps_video_sync.cli import _export
    t = np.arange(600.)
    first = Telemetry(t, {}, 'first.mp4', 600., t, EPOCH+30010+t+.2*(t >= 300),
                      clock=RecordingClock(0., .001, {'ACCL': (0, 600)}))
    second = Telemetry(np.arange(30.), {}, 'second.mp4', 30.,
                       clock=RecordingClock(600., .001, {'ACCL': (600, 630)}))
    paths = [tmp_path/'a.npz', tmp_path/'b.npz']
    for item, path in zip([first, second], paths):
        _export(item, path)
    track = Track(np.array([30000., 32000.]), np.ones(2), np.ones(2), np.ones(2), date(2026, 1, 1))
    strict = match_videos(track, paths, source='npz')
    assert [r.status for r in strict] == ['ambiguous', 'ambiguous']
    policy = GpsTimingPolicy(**BOUNDED)
    results = match_videos(track, paths, source='npz', gps_policy=policy)
    assert [r.status for r in results] == ['ok', 'ok']
    for result in results:
        assert result.mapping.source.kind == 'shot'
        assert result.gps.policy is policy
        assert result.gps.fit.envelope.duration_seconds == 630.
        assert result.to_dict()['gps']['diagnostics_coordinates'] == 'shot'
        assert result.to_dict()['selection']['selected_method'] == 'shot'
        assert result.gps.discontinuity_detected
    assert results[1].offset_seconds-results[0].offset_seconds == 600.
    tight = match_videos(track, paths, source='npz', gps_policy=GpsTimingPolicy(max_uncertainty_seconds=.1, allow_bounded_discontinuities=True))
    assert all(r.gps.verdict != 'accepted' for r in tight)


def test_bounded_steps_are_session_anchors_and_unsafe_motion_is_not(tmp_path):
    from dataclasses import replace
    from gps_video_sync import match_videos
    from gps_video_sync.cli import _export
    from gps_video_sync.models import CameraStart, Accepted, OwnMotion, RecordingClock
    from gps_video_sync.session import match_session
    track = Track(np.array([30000., 35000.]), np.ones(2), np.ones(2), np.ones(2), date(2026, 1, 1))
    t = np.arange(600.)
    recordings = [Telemetry(t, {}, f'{i}.mp4', 600., t, EPOCH+30010+i*1200+t+.2*(t >= 300),
                            camera_start=CameraStart(i*1200., .01, 'camera'),
                            clock=RecordingClock(i*1200., .001, {'ACCL': (i*1200, i*1200+600)})) for i in range(2)]
    recordings.append(Telemetry(np.arange(20.), {}, 'target.mp4', 20., camera_start=CameraStart(700., .01, 'camera'),
                                clock=RecordingClock(700., .001, {'ACCL': (700, 720)})))
    paths = [tmp_path/f'{i}.npz' for i in range(3)]
    for item, path in zip(recordings, paths):
        _export(item, path)
    results = match_videos(track, paths, source='npz', gps_policy=GpsTimingPolicy(**BOUNDED))
    assert [r.status for r in results] == ['ok', 'ok', 'ok']
    assert results[2].mapping.source.kind == 'session'
    assert results[2].offset_seconds == pytest.approx(30710.1, abs=1e-5)
    assert results[2].gps.reason_codes == ('no_observations',)
    assert results[2].to_dict()['selection'] == {'motion_attempted': True, 'selected_method': 'session'}
    unsafe = synchronize(track, recordings[0])
    assert unsafe.gps.blocks_transfer
    unsafe.outcome = Accepted(replace(results[0].mapping, source=OwnMotion()))
    unsafe.input_file = results[0].input_file
    unsafe.confidence['gps_rejected'] = 'Arbitrary human summary'
    target = synchronize(track, recordings[2])
    target.input_file = results[2].input_file
    placed = match_session(track, recordings, [unsafe, results[1], target])
    assert placed[2].status == 'ambiguous'
    assert placed[2].outcome.session.reason == 'too_few_anchors'


def test_bounded_small_plateau_covers_every_observation():
    t = np.arange(600.)
    delta = .2*((t >= 100) & (t < 105))
    result = solve(t, delta, policy=GpsTimingPolicy(**BOUNDED))
    assert result.gps.verdict == 'accepted'
    assert result.gps.isolated_outlier_count == 0
    residual = np.max(abs(30010+t+delta-result.mapping.utc_seconds(t)))
    assert result.gps.fit.envelope.residual_max_seconds == pytest.approx(residual)
    assert residual <= result.uncertainty_seconds


def test_correlated_density_does_not_shrink_deterministic_rate_bound():
    bounds = []
    for spacing in [1., .1]:
        t = np.arange(0., 600., spacing)
        result = solve(t, .04*np.sin(t/20))
        assert result.gps.verdict == 'accepted'
        bounds.append(result.gps.fit.envelope.correlated_rate_bound)
    assert .95 < bounds[1]/bounds[0] < 1.05


@pytest.mark.parametrize('deviation,reason', [
    (-np.arange(600.), 'non_increasing_clock'),
    (np.full(600, np.nan), 'insufficient_observations'),
])
def test_fundamental_bad_clocks_have_finite_json(deviation, reason):
    result = solve(np.arange(600.), deviation)
    assert result.gps.verdict == 'rejected'
    assert reason in result.gps.reason_codes
    assert result.mapping is None
    assert result.to_dict()['selection']['selected_method'] == 'none'
    json.dumps(result.to_dict(), allow_nan=False)


def test_numpy_policy_numbers_produce_plain_json():
    result = solve(np.arange(600.), policy=GpsTimingPolicy(max_uncertainty_seconds=np.float32(1)))
    assert result.gps.verdict == 'accepted'
    assert json.loads(json.dumps(result.to_dict(), allow_nan=False))['gps']['policy']['max_uncertainty_seconds'] == 1.


def test_cli_policy_and_saved_result_and_analysis(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from gps_video_sync.cli import _export
    from gps_video_sync.verify import select_result
    from gps_video_sync import read_video
    t = np.arange(600.)
    telemetry = Telemetry(t, {}, 'recording.mp4', 600., t, EPOCH+30010+t+.2*(t >= 300))
    archive, track, saved, plot = [tmp_path/name for name in ['data.npz', 'flight.igc', 'result.json', 'plot.png']]
    _export(telemetry, archive)
    track.write_text('HFDTE010126\nB0820000000000N00000000EA0000000000\nB0900000000000N00000000EA0000000000\n')
    base = [str(archive), '--source', 'npz', '--track', str(track)]
    policy = ['--gps-max-uncertainty-seconds', '1', '--gps-allow-bounded-discontinuities']
    def cli(command, extra):
        return subprocess.run([sys.executable, '-m', 'gps_video_sync', command, *base, *extra], capture_output=True, text=True)
    strict = cli('sync', [])
    assert strict.returncode == 2, strict.stderr
    assert json.loads(strict.stdout)['videos'][0]['gps']['verdict'] == 'rejected'
    bounded = cli('sync', [*policy, '--json', str(saved)])
    assert bounded.returncode == 0, bounded.stderr
    document = json.loads(saved.read_text())
    selected = select_result(document, read_video(archive, source='npz'))
    assert selected['gps']['verdict'] == 'accepted'
    assert selected['gps']['policy'] == {'max_uncertainty_seconds': 1., 'allow_bounded_discontinuities': True}
    fresh = cli('verify', [*policy, '--output', str(plot)])
    assert fresh.returncode == 0, fresh.stderr
    assert plot.read_bytes().startswith(b'\x89PNG')
    restored = cli('verify', ['--result', str(saved), '--output', str(plot)])
    assert restored.returncode == 0, restored.stderr
    for flag in [policy[:2], policy[2:]]:
        conflict = cli('verify', ['--result', str(saved), '--output', str(plot), *flag])
        assert conflict.returncode == 1
        assert 'cannot be used with verify --result' in conflict.stderr
    for value in ['0', '-1', 'nan', 'inf']:
        invalid = cli('sync', ['--gps-max-uncertainty-seconds', value])
        assert invalid.returncode == 1
        assert 'finite positive' in invalid.stderr
    script = Path(__file__).resolve().parents[1]/'scripts'/'check_gps_timing.py'
    analysis = subprocess.run([sys.executable, str(script), '--timing-npz', str(archive)], cwd=tmp_path,
                              capture_output=True, text=True)
    assert analysis.returncode == 0, analysis.stderr
    assert str(tmp_path) not in analysis.stdout
    assert '2026' not in analysis.stdout
    assert str(int(EPOCH)) not in analysis.stdout
    measured = json.loads(analysis.stdout)['timing_npz']
    assert measured['policies']['strict']['verdict'] == 'rejected'
    assert measured['policies']['bounded']['verdict'] == 'accepted'
    assert measured['duration_seconds'] == 600.
