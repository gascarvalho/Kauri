"""Pure runtime-contract tests for the frozen SHAPE25 factorial."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from experiments.adaptive import run_shape_factorial_campaign
from experiments.adaptive.kauri_experiment import factorial_runtime
from experiments.adaptive.kauri_experiment.factorial_manifest import (
    FactorialManifestError,
    build_factorial_plan,
    load_frozen_manifest,
)
from experiments.adaptive.kauri_experiment.factorial_runtime import (
    ManagerSecretMaterial,
    build_factorial_runtime,
    build_slot_runtime,
    build_smoke_metadata,
    canonical_runtime_bytes,
    materialize_manager_argv,
    materialize_replica_argv,
    runtime_preflight,
)
from experiments.adaptive.kauri_experiment.factorial_validation import (
    FROZEN_RUNTIME_SHA256,
)

REPOSITORY = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v6.json"
)
V5_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v5.json"
)
V4_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v4.json"
)
V3_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v3.json"
)
V2_MANIFEST_PATH = (
    REPOSITORY / "experiments/adaptive/profiles/shape-placement-factorial-v2.json"
)


@pytest.fixture(scope="module")
def frozen_plan():
    return build_factorial_plan(load_frozen_manifest(MANIFEST_PATH))


@pytest.fixture(scope="module")
def runtime_plan(frozen_plan):
    return build_factorial_runtime(frozen_plan)


def _matched_slots(frozen_plan):
    block_id = frozen_plan.slots[0].block_id
    return tuple(slot for slot in frozen_plan.slots if slot.block_id == block_id)


def _option(argv: tuple[str, ...], name: str) -> str:
    assert argv.count(name) == 1
    return argv[argv.index(name) + 1]


def test_matched_arms_share_the_frozen_cutoff_and_transition_contracts(
    frozen_plan,
) -> None:
    slots = _matched_slots(frozen_plan)
    specs = tuple(build_slot_runtime(slot) for slot in slots)

    assert {spec.arm_code for spec in specs} == {"00", "P", "S", "PS"}
    assert len({spec.scientific_seed for spec in specs}) == 1
    assert len({spec.actor_ids for spec in specs}) == 1
    assert len({spec.fault_window for spec in specs}) == 1
    assert len({spec.cutoff_contract for spec in specs}) == 1
    assert len({spec.transition_sequence for spec in specs}) == 1
    assert specs[0].cutoff_contract.as_document() == {
        "actual_cutoffs_recorded_live": True,
        "actual_cutoff_validation_rule": "slot_local_monotonic_phase_order_v1",
        "baseline_bucket_count": slots[0].workload.baseline_bucket_count,
        "bucket_width_s": slots[0].workload.bucket_width_s,
        "epoch1_stable_bucket_count": slots[0].workload.epoch1_stable_bucket_count,
        "epoch2_stable_bucket_count": slots[0].workload.epoch2_stable_bucket_count,
        "evidence_window_rule": "fresh_exact_predecessor_after_common_commit",
        "fault_evidence_bucket_count": slots[0].workload.fault_evidence_bucket_count,
        "same_cutoff_rule_required": True,
    }
    assert specs[0].transition_sequence.transition_count == 2
    assert specs[0].transition_sequence.total_activation_overhead_blocks == (
        2 * slots[0].common_timers.activation_delay_blocks
    )


def test_shape_invocation_is_common_and_only_application_boolean_varies(
    frozen_plan,
) -> None:
    specs = {
        spec.arm_code: spec
        for spec in map(build_slot_runtime, _matched_slots(frozen_plan))
    }
    without_apply = {
        spec.shape_invocation.selector_input_document() for spec in specs.values()
    }

    assert len(without_apply) == 1
    assert all(spec.shape_invocation.compute_live for spec in specs.values())
    assert specs["00"].shape_invocation.apply_selected_by_transition == (
        False,
        False,
    )
    assert specs["P"].shape_invocation.apply_selected_by_transition == (
        False,
        False,
    )
    assert specs["S"].shape_invocation.apply_selected_by_transition == (
        False,
        True,
    )
    assert specs["PS"].shape_invocation.apply_selected_by_transition == (
        False,
        True,
    )
    for spec in specs.values():
        document = spec.as_document()
        assert "selected_fanout" not in document
        assert "applied_fanout" not in document
        assert "ranking" not in document
        assert "trees" not in document
        assert "evidence_cutoffs" not in document


def test_factorial_placement_is_expressed_as_live_acceptance_predicates(
    frozen_plan,
) -> None:
    specs = {
        spec.arm_code: spec
        for spec in map(build_slot_runtime, _matched_slots(frozen_plan))
    }

    for spec in specs.values():
        assert spec.epoch1_placement.policy_intent == "fault_containment"
        assert spec.epoch1_placement.actors_are_wait_exempt_leaves is True
        assert spec.epoch1_placement.actor_truth_is_policy_input is False
        assert spec.epoch2_placement.actors_are_wait_exempt_leaves is True
        assert spec.epoch2_placement.actor_truth_is_policy_input is False

    for code in ("00", "S"):
        assert specs[code].epoch2_placement.policy_intent == "fault_containment"
        assert (
            specs[code].epoch2_placement.roots_equal_live_highest_ranked_eligible
            is False
        )
    for code in ("P", "PS"):
        assert specs[code].epoch2_placement.policy_intent == (
            "performance_optimization"
        )
        assert (
            specs[code].epoch2_placement.roots_equal_live_highest_ranked_eligible
            is True
        )
        assert specs[code].epoch2_placement.influential_order_source == (
            "live_accepted_evidence_ranking"
        )


def test_causal_sequence_requires_independent_raw_actor_role_proof(
    frozen_plan,
) -> None:
    for spec in map(build_slot_runtime, _matched_slots(frozen_plan)):
        assert spec.causal_acceptance.as_document() == {
            "declared_or_synthetic_outcomes_accepted": False,
            "post_containment_actor_coverage_rule": "every_declared_actor",
            "post_containment_required_role": "leaf",
            "post_containment_wait_exempt": True,
            "pre_epoch1_actor_coverage_rule": (
                "each_declared_actor_has_source_bound_marker"
            ),
            "pre_epoch1_required_action": "omit_aggregate",
            "pre_epoch1_required_role": "internal",
            "proof_source": "independent_raw_artifact_validation",
        }


def test_manager_is_blinded_and_carries_two_live_transition_requests(
    frozen_plan,
) -> None:
    for slot in _matched_slots(frozen_plan):
        spec = build_slot_runtime(slot)
        manager = spec.manager_argv_template.argv

        assert _option(manager, "--shape-candidate-fanouts") == ",".join(
            map(str, slot.candidate_fanouts)
        )
        assert _option(manager, "--tree-fanout") == str(slot.initial_fanout)
        assert _option(manager, "--pipeline-stretch") == str(slot.pipeline_stretch)
        assert _option(manager, "--shape-deterministic-seed") == str(
            slot.scientific_seed
        )
        assert manager.count("--required-nonresponsive") == 1
        assert _option(manager, "--required-nonresponsive") == str(
            len(slot.byzantine_actor_ids)
        )
        assert "--shape-adaptation-enabled" not in manager
        assert manager.count("--transition-request") == 2
        assert manager.count("--bundle-output") == 2
        assert manager.count("--replica") == slot.replica_count
        assert not any("experiment-byzantine" in argument for argument in manager)
        assert not any("rotating-omission" in argument for argument in manager)
        requests = [
            json.loads(manager[index + 1])
            for index, argument in enumerate(manager)
            if argument == "--transition-request"
        ]
        assert [request["successor_epoch_number"] for request in requests] == [1, 2]
        assert [
            request["minimum_predecessor_residency_ms"] for request in requests
        ] == [
            0,
            slot.workload.epoch1_stable_bucket_count
            * slot.workload.bucket_width_s
            * 1_000,
        ]
        assert [
            request["minimum_post_baseline_observation_ms"] for request in requests
        ] == [
            (
                slot.byzantine.start_after_prelaunch_anchor_s
                + slot.workload.fault_evidence_bucket_count
                * slot.workload.bucket_width_s
            )
            * 1_000,
            0,
        ]
        assert [request["apply_shape_selection"] for request in requests] == [
            False,
            slot.shape_adaptation,
        ]
        assert requests[0]["policy_intent"] == "fault_containment"
        assert requests[1]["policy_intent"] == (
            "performance_optimization"
            if slot.placement_adaptation
            else "fault_containment"
        )
        assert all(
            request["evidence_window_rule"]
            == "fresh_exact_predecessor_after_common_commit"
            for request in requests
        )
        for request in requests:
            if request["policy_intent"] == "fault_containment":
                assert request["containment_baseline_root_source"] == (
                    "live_predecessor_roots"
                )
                assert request["policy_parameters"] == {}
            else:
                assert "containment_baseline_root_source" not in request
                assert request["policy_parameters"] == {}
        policy = slot.responsiveness_policy
        assert _option(manager, "--responsiveness-policy-version") == (
            policy.policy_version
        )
        assert _option(manager, "--responsiveness-attempt-window") == str(
            policy.attempt_window
        )
        assert _option(manager, "--responsiveness-minimum-attempts") == str(
            policy.minimum_attempts
        )
        assert _option(manager, "--responsiveness-minimum-response-rate-ppm") == str(
            policy.minimum_response_rate_ppm
        )
        assert _option(manager, "--responsiveness-maximum-timeout-rate-ppm") == str(
            policy.maximum_timeout_rate_ppm
        )
        assert _option(manager, "--responsiveness-trailing-timeout-streak") == str(
            policy.trailing_timeout_streak
        )
        assert _option(
            manager, "--responsiveness-latency-percentile-basis-points"
        ) == str(policy.latency_percentile_basis_points)
        assert (
            1_000_000 // len(slot.byzantine_actor_ids) > policy.maximum_timeout_rate_ppm
        )


def test_every_slot_requires_the_frozen_three_nonresponsive_actors(
    frozen_plan,
) -> None:
    for slot in frozen_plan.slots:
        spec = build_slot_runtime(slot)
        assert len(spec.actor_ids) == 3
        assert (
            _option(
                spec.manager_argv_template.argv,
                "--required-nonresponsive",
            )
            == "3"
        )


def test_manager_materialization_supplies_hex_and_absolute_slot_paths(
    frozen_plan, tmp_path: Path
) -> None:
    spec = build_slot_runtime(_matched_slots(frozen_plan)[0])
    slot_directory = tmp_path / spec.slot_id
    secrets = ManagerSecretMaterial(
        manager_tls_private_key_der_hex="a1b2",
        manager_tls_certificate_der_hex="c3d4",
        issuer_private_key_hex="11" * 32,
        replica_tls_certificate_der_hex=tuple(
            f"{replica_id + 1:02x}" for replica_id in range(spec.replica_count)
        ),
    )
    template_document = json.dumps(spec.as_document(), sort_keys=True)
    argv = materialize_manager_argv(spec, slot_directory, secrets)

    assert str(tmp_path) not in template_document
    assert secrets.issuer_private_key_hex not in template_document
    assert not any("{{" in argument or "}}" in argument for argument in argv)
    assert _option(argv, "--tls-privkey") == secrets.manager_tls_private_key_der_hex
    assert _option(argv, "--tls-cert") == secrets.manager_tls_certificate_der_hex
    assert _option(argv, "--issuer-private-key") == secrets.issuer_private_key_hex
    bundle_outputs = tuple(
        argv[index + 1]
        for index, argument in enumerate(argv)
        if argument == "--bundle-output"
    )
    assert bundle_outputs == tuple(
        str(slot_directory / transition.bundle_relative_path)
        for transition in spec.transitions
    )
    assert all(Path(output).is_absolute() for output in bundle_outputs)
    requests = tuple(
        json.loads(argv[index + 1])
        for index, argument in enumerate(argv)
        if argument == "--transition-request"
    )
    for request, transition, bundle_output in zip(
        requests,
        spec.transitions,
        bundle_outputs,
    ):
        # The native parser requires canonical relative declarations, then
        # derives the exact absolute snapshot sibling from bundle-output.
        assert request["bundle_path"] == transition.bundle_relative_path
        assert request["evidence_snapshot_path"] == (
            transition.request.evidence_snapshot_path
        )
        assert Path(bundle_output).parent / "evidence-snapshot.json" == (
            slot_directory / transition.request.evidence_snapshot_path
        )


def test_containment_roots_are_late_bound_from_each_live_predecessor(
    frozen_plan,
) -> None:
    cells: set[tuple[int, int]] = set()
    for slot in frozen_plan.slots:
        cell = (slot.replica_count, slot.initial_fanout)
        if cell in cells or slot.arm_code not in ("00", "S"):
            continue
        cells.add(cell)
        spec = build_slot_runtime(slot)
        epoch1_roots = spec.transitions[0].request.containment_baseline_roots
        epoch2_roots = spec.transitions[1].request.containment_baseline_roots
        assert epoch1_roots == ()
        assert epoch2_roots == ()
        assert spec.transitions[0].request.containment_baseline_root_source == (
            "live_predecessor_roots"
        )
        assert spec.transitions[1].request.containment_baseline_root_source == (
            "live_predecessor_roots"
        )
    assert cells == {
        (replica_count, fanout)
        for replica_count in (13, 22, 31)
        for fanout in (2, 3, 5)
    }


def test_prelaunch_fault_window_is_mechanically_feasible(frozen_plan) -> None:
    for slot in frozen_plan.slots:
        spec = build_slot_runtime(slot)
        timers = slot.common_timers
        workload = slot.workload
        minimum_duration = (
            workload.fault_evidence_bucket_count * workload.bucket_width_s
            + 2 * timers.transition_convergence_deadline_s
            + workload.epoch1_stable_bucket_count * workload.bucket_width_s
            + workload.epoch2_stable_bucket_count * workload.bucket_width_s
            + timers.drain_margin_s
            + timers.schedule_slack_s
        )

        assert spec.fault_window.start_after_prelaunch_anchor_s == (
            timers.startup_timeout_s
            + workload.baseline_bucket_count * workload.bucket_width_s
        )
        assert spec.fault_window.duration_s >= minimum_duration
        assert spec.fault_window.schedule_slack_s >= 30
        assert spec.fault_window.transition_observation_bound_rule == (
            "shared_slot_hard_deadline_until_manager_selection_v1"
        )
        assert timers.hard_timeout_s >= (
            spec.fault_window.start_after_prelaunch_anchor_s
            + spec.fault_window.duration_s
            + timers.drain_margin_s
        )


def test_replica_argv_materialization_uses_one_shared_raw_clock_anchor(
    frozen_plan,
    tmp_path: Path,
) -> None:
    slot = _matched_slots(frozen_plan)[0]
    spec = build_slot_runtime(slot)
    slot_directory = tmp_path / spec.slot_id
    anchor = 987_654_321_000
    processes = materialize_replica_argv(spec, slot_directory, anchor)
    expected_start = anchor + slot.byzantine.start_after_prelaunch_anchor_s * 10**9
    expected_end = expected_start + slot.byzantine.duration_s * 10**9

    assert len(processes) == slot.replica_count
    assert len(spec.process_logs.kauri_fault_marker_relative_paths) == (
        2 * slot.replica_count
    )
    assert len(set(spec.process_logs.kauri_fault_marker_relative_paths)) == (
        2 * slot.replica_count
    )
    assert "window-start-monotonic-ns" not in json.dumps(spec.as_document())
    assert "window-end-monotonic-ns" not in json.dumps(spec.as_document())
    for process in processes:
        argv = process.argv
        assert not any("{{" in argument or "}}" in argument for argument in argv)
        config_paths = tuple(
            argv[index + 1]
            for index, argument in enumerate(argv)
            if argument == "--conf"
        )
        assert config_paths == (
            str(slot_directory / spec.main_config.path),
            str(slot_directory / f"runtime/replica-{process.replica_id}.conf"),
        )
        assert all(Path(path).is_absolute() for path in config_paths)
        assert _option(argv, "--structured-event-run-id") == spec.slot_id
        assert _option(argv, "--structured-event-output") == str(
            slot_directory / f"raw/replica-{process.replica_id}.jsonl"
        )
        assert _option(argv, "--experiment-byzantine-mode") == slot.byzantine.mode
        assert _option(argv, "--experiment-byzantine-window") == (
            f"{slot.block_id}-persistent-omission-v1"
        )
        assert _option(argv, "--experiment-rotating-omission-actors") == ",".join(
            map(str, slot.byzantine_actor_ids)
        )
        assert _option(argv, "--experiment-byzantine-window-start-monotonic-ns") == str(
            expected_start
        )
        assert _option(argv, "--experiment-byzantine-window-end-monotonic-ns") == str(
            expected_end
        )
        assert _option(
            argv, "--experiment-byzantine-max-omissions-per-proposal"
        ) == str(slot.byzantine.max_omissions_per_proposal)
        assert _option(argv, "--experiment-rotating-omission-context-limit") == str(
            slot.byzantine.maximum_rotating_contexts
        )


def test_two_epoch_sequence_and_artifact_identity_are_deterministic(
    frozen_plan,
) -> None:
    first = build_slot_runtime(_matched_slots(frozen_plan)[0])
    second = build_slot_runtime(_matched_slots(frozen_plan)[0])

    assert first == second
    assert first.artifact_id == second.artifact_id
    assert first.transition_sequence.required_events == (
        "baseline_stable",
        "fault_window_open",
        "epoch1_command",
        "epoch1_activation",
        "epoch1_stable",
        "shape_v1_computed",
        "epoch2_command",
        "epoch2_activation",
        "epoch2_stable",
        "epoch2_drain_complete",
    )
    assert first.transition_sequence.required_events.count("epoch2_command") == 1
    assert first.transition_sequence.required_events.count("epoch2_activation") == 1
    assert first.transition_sequence.required_events.count("epoch2_stable") == 1
    assert first.transition_sequence.required_events.count("epoch2_drain_complete") == 1
    assert tuple(transition.successor_epoch for transition in first.transitions) == (
        1,
        2,
    )
    assert tuple(
        transition.request.minimum_predecessor_residency_ms
        for transition in first.transitions
    ) == (
        0,
        frozen_plan.slots[0].workload.epoch1_stable_bucket_count
        * frozen_plan.slots[0].workload.bucket_width_s
        * 1_000,
    )
    assert tuple(
        transition.request.minimum_post_baseline_observation_ms
        for transition in first.transitions
    ) == (
        (
            frozen_plan.slots[0].byzantine.start_after_prelaunch_anchor_s
            + frozen_plan.slots[0].workload.fault_evidence_bucket_count
            * frozen_plan.slots[0].workload.bucket_width_s
        )
        * 1_000,
        0,
    )
    assert len({transition.artifact_id for transition in first.transitions}) == 2


def test_runtime_contract_has_no_duplicate_transition_fields_or_bare_config_lines(
    frozen_plan,
) -> None:
    source = inspect.getsource(factorial_runtime.TransitionContract)
    spec = build_slot_runtime(_matched_slots(frozen_plan)[0])

    assert source.count("artifact_id: str") == 1
    assert all(" = " in line for line in spec.main_config.lines)
    assert (
        sum(
            line.startswith("leader-progress-timeout = ")
            for line in spec.main_config.lines
        )
        == 1
    )


def test_campaign_runtime_is_execution_ordered_and_cannot_launch(
    frozen_plan,
    runtime_plan,
) -> None:
    assert tuple(slot.execution_ordinal for slot in runtime_plan.slots) == tuple(
        range(1, len(frozen_plan.slots) + 1)
    )
    assert tuple(slot.slot_id for slot in runtime_plan.slots) != tuple(
        slot.slot_id for slot in frozen_plan.slots
    )
    assert runtime_plan.execution_authorized is False
    assert runtime_plan.execution_receipt_required is True
    assert runtime_plan.launch_permitted is False
    assert runtime_plan.automatic_retries == 0
    assert runtime_plan.replacement_policy == "none"
    assert runtime_plan.outcome_dependent_order is False
    with pytest.raises(FactorialManifestError, match="not authorized|receipt"):
        runtime_plan.require_execution_authorized()

    encoded = canonical_runtime_bytes(runtime_plan)
    assert encoded == canonical_runtime_bytes(build_factorial_runtime(frozen_plan))
    assert hashlib.sha256(encoded).hexdigest() == FROZEN_RUNTIME_SHA256
    document = json.loads(encoded)
    assert document["slot_count"] == len(frozen_plan.slots)
    assert "selected_fanout" not in document
    assert "ranking" not in document
    assert "window_start_monotonic_ns" not in document


def test_preflight_and_smoke_metadata_never_authorize_a_process(runtime_plan) -> None:
    preflight = runtime_preflight(
        runtime_plan,
        available_free_bytes=runtime_plan.minimum_free_bytes,
    )
    smoke = build_smoke_metadata()

    assert preflight["status"] == "PASS"
    assert preflight["launch_permitted"] is False
    assert preflight["automatic_retries"] == 0
    assert preflight["replacement_policy"] == "none"
    assert preflight["available_free_bytes"] == runtime_plan.minimum_free_bytes
    assert smoke.campaign_member is False
    assert smoke.figure_eligible is False
    assert smoke.denominator_contribution == 0
    assert smoke.launch_permitted is False
    assert "import subprocess" not in inspect.getsource(factorial_runtime)
    assert "import subprocess" not in inspect.getsource(run_shape_factorial_campaign)


def test_preflight_rejects_an_early_epoch1_evaluation_hold(runtime_plan) -> None:
    slot = runtime_plan.slots[0]
    transition = slot.transitions[0]
    early_request = replace(
        transition.request,
        minimum_post_baseline_observation_ms=(
            transition.request.minimum_post_baseline_observation_ms - 1
        ),
    )
    early_slot = replace(
        slot,
        transitions=(replace(transition, request=early_request), slot.transitions[1]),
    )
    early_runtime = replace(
        runtime_plan,
        slots=(early_slot, *runtime_plan.slots[1:]),
    )

    with pytest.raises(FactorialManifestError, match="invalid transition contract"):
        runtime_preflight(
            early_runtime,
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )


def test_preflight_rejects_low_disk_and_output_namespace_collisions(
    runtime_plan,
) -> None:
    with pytest.raises(FactorialManifestError, match="minimum-free-bytes"):
        runtime_preflight(
            runtime_plan,
            available_free_bytes=runtime_plan.minimum_free_bytes - 1,
        )
    assert (
        runtime_preflight(
            runtime_plan,
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )["status"]
        == "PASS"
    )
    assert (
        runtime_preflight(
            runtime_plan,
            available_free_bytes=runtime_plan.minimum_free_bytes + 1,
        )["status"]
        == "PASS"
    )

    slot = runtime_plan.slots[0]
    duplicate_events = replace(
        slot.structured_events,
        exclusive_output_per_process=False,
        replica_source_instances=(slot.structured_events.replica_source_instances[0],)
        * slot.replica_count,
        replica_output_relative_paths=(
            slot.structured_events.replica_output_relative_paths[0],
        )
        * slot.replica_count,
    )
    bad_events = replace(
        runtime_plan,
        slots=(replace(slot, structured_events=duplicate_events),)
        + runtime_plan.slots[1:],
    )
    with pytest.raises(FactorialManifestError, match="structured-event"):
        runtime_preflight(
            bad_events,
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )

    colliding_logs = replace(
        slot.process_logs,
        exclusive_output_per_process=False,
        manager_stdout_relative_path=(
            slot.process_logs.replica_stdout_relative_paths[0]
        ),
    )
    bad_logs = replace(
        runtime_plan,
        slots=(replace(slot, process_logs=colliding_logs),) + runtime_plan.slots[1:],
    )
    with pytest.raises(FactorialManifestError, match="KAURI_FAULT"):
        runtime_preflight(
            bad_logs,
            available_free_bytes=runtime_plan.minimum_free_bytes,
        )


def test_cli_preflight_passes_but_run_refuses(capsys) -> None:
    assert (
        run_shape_factorial_campaign.main(
            ["--manifest", str(MANIFEST_PATH), "preflight"]
        )
        == 0
    )
    preflight = json.loads(capsys.readouterr().out)
    assert preflight["status"] == "PASS"
    assert preflight["launch_permitted"] is False

    assert (
        run_shape_factorial_campaign.main(["--manifest", str(MANIFEST_PATH), "run"])
        == 2
    )
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["status"] == "REJECT"
    assert "authorized" in refusal["reason"] or "receipt" in refusal["reason"]


@pytest.mark.parametrize(
    "prior_manifest",
    (V2_MANIFEST_PATH, V3_MANIFEST_PATH, V4_MANIFEST_PATH, V5_MANIFEST_PATH),
)
def test_cli_defaults_to_v6_and_refuses_prior_production(
    prior_manifest: Path,
    capsys,
) -> None:
    assert run_shape_factorial_campaign.DEFAULT_MANIFEST == MANIFEST_PATH
    assert (
        run_shape_factorial_campaign.main(
            ["--manifest", str(prior_manifest), "plan"]
        )
        == 2
    )
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["status"] == "REJECT"
    assert "v1 through v5 are validation-only" in refusal["reason"]
