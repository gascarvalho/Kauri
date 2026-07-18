# Seven-replica leaf-crash development smoke

This isolated runner measures whether a fixed seven-replica Kauri tree keeps
committing after leaf replicas 4 and 6 are stopped. It is development evidence
only and is not the canonical adaptive `SMOKE20` experiment.

Run:

```sh
experiments/adaptive/seven-replica-smoke/run.sh
```

The runner generates seven run-local BLS/TLS identities, starts the manager
and replicas in recorded process groups, waits for a common baseline, records
three five-second baseline buckets, kills only replicas 4 and 6, records one
grace and three post-crash buckets, validates the logs, and cleans up its own
process groups.

Results are written beneath `results/seven-replica-leaf-crash-smoke/` and
include raw logs, generated configuration, crash records, commit events,
reputation updates, `throughput.csv`, `throughput.svg`, and `verdict.json`.
Failed and degraded runs are retained.
