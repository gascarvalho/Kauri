#!/usr/bin/env python3
"""Evaluate one already-validated W18 sham/treatment pair without launching.

The command only reopens existing external evidence.  It cannot create an
authority, a raw result, an approval, or a campaign claim.  A failed raw
recheck is reported as a nonzero incomplete pair so shell automation cannot
mistake it for an accepted descriptive observation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from .kauri_experiment import operator_capacity_v3_pair_evaluator as evaluator
    from .kauri_experiment import operator_capacity_v3_validation_bridge as bridge
except ImportError:
    from kauri_experiment import operator_capacity_v3_pair_evaluator as evaluator
    from kauri_experiment import operator_capacity_v3_validation_bridge as bridge


def _incomplete(detail: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "kauri-n31-operator-capacity-v3-matched-pair-result-v1",
        "verdict": "PAIR_INCOMPLETE_NO_CLAIM",
        "claim_eligible": False,
        "figure_eligible": False,
        "campaign_eligible": False,
        "detail": detail,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair-manifest", required=True, type=Path)
    parser.add_argument("--sham-root", required=True, type=Path)
    parser.add_argument("--treatment-root", required=True, type=Path)
    parser.add_argument("--sham-raw-validation", required=True, type=Path)
    parser.add_argument("--treatment-raw-validation", required=True, type=Path)
    parser.add_argument("--sham-authority", required=True, type=Path)
    parser.add_argument("--treatment-authority", required=True, type=Path)
    parser.add_argument("--stage-a-verifier-binary", required=True, type=Path)
    parser.add_argument("--stage-b-verifier-binary", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest, _raw = evaluator._document(args.pair_manifest, "frozen pair manifest")
        result = evaluator.evaluate_matched_pair(
            manifest,
            sham_root=args.sham_root,
            treatment_root=args.treatment_root,
            sham_raw_validation=args.sham_raw_validation,
            treatment_raw_validation=args.treatment_raw_validation,
            sham_authority=args.sham_authority,
            treatment_authority=args.treatment_authority,
            revalidate=bridge.make_pair_revalidator(
                stage_a_verifier_binary=args.stage_a_verifier_binary,
                stage_b_verifier_binary=args.stage_b_verifier_binary,
            ),
        )
    except (OSError, ValueError, evaluator.PairEvaluationError, bridge.OperatorCapacityV3ValidationBridgeError) as exc:
        print(json.dumps(_incomplete(str(exc)), sort_keys=True), file=sys.stdout)
        return 1
    print(json.dumps(result, sort_keys=True), file=sys.stdout)
    return 0 if result.get("verdict") == "PAIR_COMPLETE_DESCRIPTIVE_ONLY" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
