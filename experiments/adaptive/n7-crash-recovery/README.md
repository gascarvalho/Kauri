# N=7 crash-recovery validation and figure

This directory contains the strict analysis gate for the frozen
`N=7, f=2, Q=5` adaptive-v2 crash experiment. It does not contain a fabricated
run or a thesis figure. Test fixtures are generated only under pytest temporary
directories and are explicitly non-evidence.

## Evidence inputs

The validator accepts three kinds of input:

1. A canonical run manifest. It binds the full Kauri Git revision, clean
   worktree state, frozen profile identity and SHA-256, completed and
   non-interrupted run state, exact structured-event source instances,
   measurement boundaries, and the two crash records.
2. Canonical epoch definitions. Epoch 0 contains the seven cyclic trees rooted
   `0..6`. Epoch 1 has unchanged membership, the exact root set `2..6` in its
   committed tree-ID order generated from the manager snapshot ranking, and
   replicas `0` and `1` as wait-exempt physical leaves in every binary tree.
3. One raw `StructuredEventSink` JSONL file per declared source. These are bare
   JSON objects, one per line. A `KAURI_EVENT ` prefix is accepted only by
   `analysis.py` when its explicit diagnostic compatibility option is enabled;
   the evidence validator always rejects it.

Commit evidence has two layers. Every replica emits a minimal
`block.commit_observed` witness for each locally committed block. Its height and
hash are the cross-replica agreement key; its parent hash, transaction count,
and commit-batch index make the local commit sequence auditable without
requiring proposal-time metadata. Replica 2 additionally supplies the rich
`block.committed` stream used for throughput and epoch/tree/leader attribution.
The runner derives root cycles only from that rich observer stream, but treats
a cycle as common only when every current participant has emitted the matching
`block.commit_observed` witnesses. A rich event from a participant does not
substitute for a missing witness.

Each crash record is manifest-owned because the current process-lifecycle event
does not identify its subject and a `SIGKILL`ed process cannot emit its own
exit. The record binds replica, PID, PGID, `SIGKILL` number, request time in the
shared monotonic-raw clock, and a confirmed exit with the same PID/PGID/signal.
The manager declaration must state that it receives no crash ground truth.

The runner-owned frozen settings are checked in as `profile.json`, with profile
identity `n7-f2-q5-crash-recovery-v2`. The validator pins its exact schema,
values, bytes, and SHA-256; changing arbitrary profile bytes and merely updating
the manifest hash cannot pass. Campaign v2 distinguishes the minimum
post-activation measurement grace from the leader-suspicion activation grace.
The former only excludes transition work from throughput measurement; the
latter remains a protocol timer that prevents premature suspicion after exact
activation. Neither value substitutes for the other. `tests/synthetic_run.py`
copies this profile as an executable schema example, but its output is never
acceptable experiment evidence.

## Real campaign

Run the real local campaign from the Kauri repository root with:

```sh
python3 experiments/adaptive/n7-crash-recovery/run.py
```

The runner refuses to start evidence collection unless Kauri is on the fixed
`feature/adaptive-epoch-throughput` branch, the worktree is clean, local `HEAD`
equals `origin/feature/adaptive-epoch-throughput`, the required adaptive-v2
binaries exist, and the default loopback ports are free. It generates fresh
run-local identities, launches seven replicas plus the authenticated adaptation
manager in separate process groups, waits for seven complete baseline buckets
and a terminal `0..6` leader cycle, sends `SIGKILL` only to replicas 0 and 1,
then waits for the committed successor and seven complete post-start buckets.
Alternative binary, results-root, port, and timeout paths are available through
`python3 experiments/adaptive/n7-crash-recovery/run.py --help`.

Each attempt is preserved below `results/n7-crash-recovery/`. The manifest
hash-binds the exact executables, effective replica configs, initial epoch
input, redacted launch arguments, timeouts, pipeline/block settings, one-block
tree-switch period, five-block activation delay, and snapshot seed. The
validator also reopens and hashes the actual `--conf` files and checks their
relevant option values; normalized settings are not accepted as self-attested
proof.

The runner itself never declares scientific success. It invokes `validator.py`
after cleanup, and invokes `plot.py` only if the immutable validator verdict is
`PASS`. A failed, incomplete, interrupted, inconsistent, or rejected attempt
remains on disk and is never plotted.

## PASS gate

`validator.py` uses only the Python standard library and `analysis.py`. A PASS
requires all of the following:

- exact `N=7`, `f=2`, fixed quorum `Q=5`, and unchanged membership `0..6`;
- complete, contiguous, source-bound raw JSONL with replica 2 as the sole
  designated authoritative commit observer;
- a complete rich `block.committed` chain at replica 2 for throughput and
  epoch/tree/leader attribution, plus `block.commit_observed` witnesses from
  every current participant for runner readiness and agreement checks;
- a complete final epoch-0 root cycle `0..6`, ending at root 6 before either
  crash request;
- confirmed crashes of replicas 0 and 1 only, with no crash event or ground
  truth supplied to the manager;
- an identical committed epoch command and one matching successor activation
  at every surviving replica;
- predecessor work admitted before activation may drain contiguously under its
  exact Epoch 0 identity until the first common Epoch 1 commit; any predecessor
  commit after that successor commit is rejected;
- the first common Epoch 1 commit becomes common when every survivor has
  emitted its matching commit witness; that common time must occur no later
  than the frozen ten-second maximum activation-to-successor interval;
- `post_start` is the later of activation plus the frozen minimum
  post-activation measurement grace and the first common Epoch 1 commit; legal
  predecessor drain before that boundary is excluded from the post-change
  measurement window;
- successor root set exactly `2..6`, preserving its committed tree-ID order,
  with failed replicas `0` and `1` both wait-exempt physical leaves;
- `block.commit_observed` agreement by replicas `2..6` on every authoritative
  measurement height and hash;
- raw, phase-local, zero-filled throughput buckets no wider than five seconds,
  with at least seven complete raw buckets in both baseline and post phases and
  seven leader columns that conserve aggregate throughput exactly;
- no authoritative commit stall above ten seconds in baseline or post, and no
  degraded-phase stall above the protocol-derived 25-second bound (which
  accommodates the possible `T5,T6,T0,T1` timeout sequence after replicas 0 and
  1 crash while still rejecting an unbounded degraded interval);
- a responsive baseline containing `on_time` evidence for every replica inside
  `[baseline_start, first_crash)`; pre-baseline observations do not count;
- for each failed replica, at least `f+1=3` distinct reporters with at least
  `K=2` uncompensated timeout observation IDs each after that replica's own
  confirmed crash and before the command, and a net crash-to-command score drop
  of at least 6; the one legal same-ID timeout-to-late transition cancels that
  timeout attempt, while standalone or repeated late events fail validation;
- complete score trajectories for all seven replicas, with replicas 0 and 1
  the lowest-ranked pair before the command and at run end; and
- post-activation median throughput strictly above degraded median throughput,
  with the post/baseline recovery ratio reported rather than assumed.

Missing required evidence produces an immutable `INCOMPLETE` verdict.
Contradictory evidence or a failed invariant produces an immutable `FAIL`
verdict. Neither can be plotted. Validation also refuses to overwrite any
existing canonical artifact, and exclusive claim files prevent concurrent
validators or plotters from replacing one another's outputs.

Run validation with:

```sh
python3 experiments/adaptive/n7-crash-recovery/validator.py \
  --manifest /path/to/run/manifest.json \
  --epochs /path/to/run/epochs.json \
  --output-dir /path/to/validated-run
```

A PASS directory contains canonical copies of the manifest, profile, and epoch
definitions plus `throughput.csv`, `reputation.csv`, and `validation.json`.
The verdict SHA-256-binds every copied input and CSV.

## PASS-only figure

`plot.py` imports Matplotlib only after it has read a PASS verdict and verified
the hashes of all five bound artifacts. It writes both `figure.png` and
`figure.pdf` and refuses to overwrite either.

```sh
python3 experiments/adaptive/n7-crash-recovery/plot.py \
  /path/to/validated-run
```

The upper panel contains the raw aggregate throughput line and seven raw lines
attributed to the scheduled replica leader. It shades baseline, degraded, and
post-activation phases; shows both crash requests, the command, activation,
and activation-to-post transition interval; labels the three regions as normal
Epoch 0, crashed Epoch 0, and Epoch 1 with crashed replicas at leaves; and draws
the three raw phase medians. The lower panel contains seven stepwise manager
reputation trajectories, emphasizing the two crashed replicas.

Run the synthetic non-evidence suite with:

```sh
pytest -q experiments/adaptive/n7-crash-recovery/tests
```
