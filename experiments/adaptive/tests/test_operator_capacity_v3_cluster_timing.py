from pathlib import Path
import hashlib
import json

import pytest

from kauri_experiment import operator_capacity_v3_backend as backend
from kauri_experiment import operator_capacity_v3_cluster as cluster
from kauri_experiment import operator_capacity_v3_cluster_profiles as profiles
from kauri_experiment import operator_capacity_v3_cluster_timing as timing
from kauri_experiment import operator_capacity_v3_local_runner as physical
from kauri_experiment import operator_capacity_v3_materializer as mat
from test_operator_capacity_v3_backend import _fixture, _canonical
from test_operator_capacity_v3_materializer import _inputs, native_identities


def _cluster_fixture(tmp_path, regime='heterogeneous', arm='treatment'):
    root, manager, replicas, quota = _fixture(tmp_path)
    path = root / 'config/hotstuff.gen.conf'
    path.write_bytes(path.read_bytes().replace(b'aggregation-timeout = 0.5', b'aggregation-timeout = 2.0')
                    .replace(b'leader-progress-timeout = 5.0', b'leader-progress-timeout = 20.0'))
    manifest_path = root / 'materialization-manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['cluster_timing_profile'] = timing.expected_profile()
    manager[manager.index('--convergence-deadline-seconds') + 1] = '90'
    manifest['manager_argv_sha256'] = backend._argv_digest(manager)
    manifest['artifact_sha256']['config/hotstuff.gen.conf'] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest['synthetic_workload']['main_config_sha256'] = manifest['artifact_sha256']['config/hotstuff.gen.conf']
    manifest['arm'] = arm
    manifest['stage_a_native_arm'] = backend._ARMS[arm]
    args = manifest['stage_a_verifier_arguments']
    args[args.index('--arm') + 1] = backend._ARMS[arm]
    manifest_path.write_bytes(_canonical(manifest))
    quota.write_bytes(_canonical(profiles.expected_quota_profile(regime)))
    return root, manager, replicas, quota


@pytest.mark.parametrize('regime', ['heterogeneous', 'homogeneous'])
@pytest.mark.parametrize('arm', ['sham', 'treatment'])
def test_exact_timing_binds_plan_spawn_request_and_retained_replay(tmp_path, regime, arm):
    root, manager, replicas, quota = _cluster_fixture(tmp_path, regime, arm)
    args = dict(materialization_root=root, manager_argv=manager, replica_argv=replicas,
                quota_profile=quota, cluster_physical_regime=regime)
    plan = backend.prepare_no_launch_backend(**args)
    assert plan['cluster_timing_profile'] == timing.expected_profile()
    physical._verify_synthetic_config_before_spawn(root, manager)
    build = tmp_path / 'build.json';build.write_bytes(b'build')
    request = cluster.build_request(plan, root=root, run_id='test', build_receipt=build,
                                    booking_id=next(iter(cluster.BOOKINGS)))
    assert request['cluster_timing_profile'] == timing.expected_profile()
    (root / 'raw/preserved.json').write_bytes(b'preserved')
    retained = backend.inspect_retained_cluster_backend(**args)
    assert retained == plan
    assert plan['automatic_retries'] == 0 and plan['launch_permitted'] is False


def test_cluster_timing_cannot_enter_legacy_local_plan(tmp_path):
    root, manager, replicas, quota = _cluster_fixture(tmp_path)
    with pytest.raises(backend.OperatorCapacityV3BackendError, match='legacy local'):
        backend.prepare_no_launch_backend(materialization_root=root, manager_argv=manager,
                                          replica_argv=replicas, quota_profile=quota)


@pytest.mark.parametrize('deadline', ['30', '60', '180'])
def test_cluster_manager_deadline_drift_rejects_after_rehash(tmp_path, deadline):
    root, manager, replicas, quota = _cluster_fixture(tmp_path)
    manager[manager.index('--convergence-deadline-seconds') + 1] = deadline
    path = root / 'materialization-manifest.json'
    manifest = json.loads(path.read_bytes())
    manifest['manager_argv_sha256'] = backend._argv_digest(manager)
    path.write_bytes(_canonical(manifest))
    with pytest.raises(backend.OperatorCapacityV3BackendError, match='frozen cadence'):
        backend.prepare_no_launch_backend(materialization_root=root, manager_argv=manager,
            replica_argv=replicas, quota_profile=quota, cluster_physical_regime='heterogeneous')


@pytest.mark.parametrize('mode', ['missing-profile', 'null-profile', 'timer-drift', 'profile-drift', 'leader-drift', 'old-cluster-profile', 'previous-cluster-profile'])
def test_timing_drift_rejects_even_after_manifest_rehash(tmp_path, mode):
    root, manager, replicas, quota = _cluster_fixture(tmp_path)
    manifest_path = root / 'materialization-manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    config_path = root / 'config/hotstuff.gen.conf'
    if mode == 'missing-profile':del manifest['cluster_timing_profile']
    elif mode == 'null-profile':manifest['cluster_timing_profile'] = None
    elif mode == 'profile-drift':manifest['cluster_timing_profile']['aggregation_per_remaining_level_ms'] = 1500
    elif mode == 'timer-drift':config_path.write_bytes(config_path.read_bytes().replace(b'aggregation-timeout = 2.0', b'aggregation-timeout = 0.5'))
    elif mode == 'leader-drift':config_path.write_bytes(config_path.read_bytes().replace(b'leader-progress-timeout = 20.0', b'leader-progress-timeout = 5.0'))
    elif mode == 'old-cluster-profile':
        manifest['cluster_timing_profile'].update(kind='kauri-w18-cluster-aggregation-1s-leader-5s-v1',
            schema_version=1, aggregation_per_remaining_level_ms=1000, leader_progress_timeout_ms=5000)
        config_path.write_bytes(config_path.read_bytes().replace(b'aggregation-timeout = 2.0', b'aggregation-timeout = 1.0')
            .replace(b'leader-progress-timeout = 20.0', b'leader-progress-timeout = 5.0'))
    elif mode == 'previous-cluster-profile':
        manifest['cluster_timing_profile'].update(kind='kauri-w18-cluster-aggregation-1s-leader-10s-v2',
            schema_version=2, aggregation_per_remaining_level_ms=1000, leader_progress_timeout_ms=10000)
        config_path.write_bytes(config_path.read_bytes().replace(b'aggregation-timeout = 2.0', b'aggregation-timeout = 1.0')
            .replace(b'leader-progress-timeout = 20.0', b'leader-progress-timeout = 10.0'))
    manifest['artifact_sha256']['config/hotstuff.gen.conf'] = hashlib.sha256(config_path.read_bytes()).hexdigest()
    manifest['synthetic_workload']['main_config_sha256'] = manifest['artifact_sha256']['config/hotstuff.gen.conf']
    manifest_path.write_bytes(_canonical(manifest))
    with pytest.raises(backend.OperatorCapacityV3BackendError):
        backend.prepare_no_launch_backend(materialization_root=root, manager_argv=manager,
            replica_argv=replicas, quota_profile=quota, cluster_physical_regime='heterogeneous')


@pytest.mark.parametrize('arm', ['sham', 'treatment'])
def test_materializer_writes_identical_approved_timing_for_both_arms(tmp_path, native_identities, arm):
    values = _inputs(tmp_path, native_identities)
    values['arm'] = arm
    receipt_path = values['stage_a_verifier_receipt']['path']
    receipt = json.loads(receipt_path.read_bytes());receipt['arm'] = backend._ARMS[arm]
    receipt_path.write_bytes(_canonical(receipt))
    values['stage_a_verifier_receipt']['sha256'] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    values['cluster_timing_profile'] = timing.expected_profile()
    root = tmp_path / 'out'
    result = mat.materialize_operator_capacity_v3(root, **values)
    assert result['manifest']['cluster_timing_profile'] == timing.expected_profile()
    config = (root / 'config/hotstuff.gen.conf').read_text()
    assert 'aggregation-timeout = 2.0\n' in config
    assert 'leader-progress-timeout = 20.0\n' in config
    assert result['manager_argv'][result['manager_argv'].index('--convergence-deadline-seconds') + 1] == '90'
    assert result['manifest']['protocol'] == {'N':31, 'Q':21, 'tree_count':21}
    physical._verify_synthetic_config_before_spawn(root, result['manager_argv'])
    assert not (root / 'runtime').exists() and not list((root / 'raw').iterdir())


@pytest.mark.parametrize('field,value', [('schema_version', True), ('aggregation_per_remaining_level_ms', 500),
                                       ('leader_progress_timeout_ms', 5000), ('leader_activation_grace_ms', 0),
                                       ('convergence_deadline_seconds', 30),
                                       ('extra', 'unpinned')])
def test_timing_profile_is_exact_typed_and_closed(field, value):
    profile = timing.expected_profile();profile[field] = value
    with pytest.raises(ValueError):timing.aggregation_seconds(profile)


def test_legacy_default_remains_half_second():
    assert timing.aggregation_seconds() == 0.5
    assert timing.leader_progress_seconds() == 5.0
    assert timing.convergence_seconds() == 30
    assert timing.convergence_seconds(timing.expected_profile()) == 90


def test_approved_leader_timer_allows_native_fallback_recovery_horizon():
    profile = timing.expected_profile()
    # The native N31/fanout-five probe gives maximum level 3 and horizon 16 s.
    fallback_ms = 2 * 4 * profile['aggregation_per_remaining_level_ms']
    assert fallback_ms == 16000
    assert fallback_ms < profile['leader_activation_grace_ms'] + profile['leader_progress_timeout_ms']
    assert timing.leader_progress_seconds(profile) == 20.0
