# Worker-pool pattern (worked example)

Sources (read-only examples): sailsprep `profiles/orcd.yaml` (`resource_classes:`) and senselab
`src/senselab/utils/work_pool.py` (stdlib only; `uv run python -m senselab.utils.work_pool work|control POOL`).

## Resource classes (config)
Each class (e.g. `h100`, `h100-bigmem`, `a100`, `cpu`) lists partition entries:
`{partition, max_workers, gres, cpus, mem, time, requeue, exclude: [bad nodes]}`. Same class, different partitions,
each sized to that partition's QOS (e.g. pi_satra max 2 H100 workers, ou_bcs_normal 8, ou_bcs_low 8 with requeue;
cpu class has 16 workers on pi_satra/ou_bcs_*, 64 on mit_preemptable with requeue). Notes in the file record why:
a partition removed after a sandboxed tool failed to start on specific hosts, a bigmem class after host OOM kills.
`work_pool:` block sets lease_s, heartbeat_s, max_attempts and the controller job's own time limit.

## Mechanics (work_pool.py)
- Pool = a directory on shared storage: `queue/`, `leases/`, `done/`, `failed/`, `dead/`, `interrupted/`,
  `hostcheck/`, `workers/`, `controller/`, `status.json`.
- An item lives in exactly one place; claim and settle use atomic `rename`/exclusive `link`, verified by read-back
  (NFS retransmits can report failure after success). Ages use the storage clock.
- Worker: host check first (failure recorded under `hostcheck/<partition>.*`, claims nothing), then claim, run the
  item command with POOL_ITEM_ID/POOL_ATTEMPT, heartbeat, complete or fail. SIGTERM releases the item (preemption).
- Controller tick: reap expired leases, count queued items per class and live Slurm workers per partition, submit or
  cancel workers up to each entry's max_workers; items exceeding attempts go to `dead/`.
