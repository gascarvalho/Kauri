# N=7 crash-recovery analysis

This directory contains the canonical commit parser and raw throughput
attribution seam for the bounded seven-replica adaptive-v2 smoke scenario.

> **Evidence warning:** the unit-test fixtures are synthetic and are not
> experiment evidence. No figure produced from synthetic fixtures may be used
> in the thesis.

`analysis.py` accepts log lines of the form `KAURI_EVENT <canonical JSON>` and
uses the existing structured-event envelope. It accepts throughput commits
only when all of the following hold:

- `event_schema_version` is 1 and `run_id` matches the requested run;
- `source_kind` is `replica`, `source_id` is `2`, and `source_instance`
  matches the exact process instance supplied by the run manifest;
- source sequence strictly increases and monotonic time never regresses;
- `event_type` is `block.committed` and
  `payload.designated_observer` is true;
- the non-genesis height, full lowercase hash, transaction count, and exact
  decision-proof configuration are well formed;
- producer integer fields fit their declared unsigned 32- or 64-bit widths;
- `payload.decision_proof.block_hash` equals the committed block hash; and
- the leader is resolved through an explicit validated mapping keyed by
  `(epoch_number, epoch_digest, tree_id)`.

The commit event does not self-report its leader. Leader attribution must come
from validated epoch definitions (later serialized as `epochs.json`), which
prevents a log payload from choosing its own leader label.

An exact same-hash observer replay is counted once after its substantive
height, parent, transaction count, decision proof, resolved leader, view, and
batch metadata agree. The first event supplies the retained sequence and
timestamp. A same-hash metadata contradiction or two hashes at one height
fails analysis instead of being silently deduplicated.

Throughput is emitted as raw, half-open, phase-local buckets no wider than five
seconds. Crash and activation boundaries begin new buckets, incomplete phase
tails use their actual elapsed time, and missing intervals remain explicit
zero rows. Seven per-leader transaction/TPS columns conserve the aggregate
exactly. Baseline, degraded, and post-activation medians are calculated from
those raw buckets, including zeroes.

Run the synthetic unit suite with:

```sh
pytest -q \
  experiments/adaptive/n7-crash-recovery/tests/test_analysis.py
```

This module is not the full run validator or plotter. A final graph may be
generated only from a real run whose complete artifacts have passed the later
canonical validator. A parsed log or passing unit fixture is not, by itself,
accepted thesis evidence.
