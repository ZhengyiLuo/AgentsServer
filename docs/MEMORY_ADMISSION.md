# Optional available-memory admission

AgentsServer does not impose a minimum available-memory floor by default.
Operators can opt into a launch-time floor for their own server:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `AGENTSDOCK_MIN_START_AVAILABLE_MEM_MB` | 0 (disabled) | Minimum available memory before a new agent turn, including queued turns and goal resumes. |
| `AGENTSDOCK_JOB_MIN_AVAILABLE_MEM_MB` | 0 (disabled) | Additional reserve before launching a scheduled job. |

Values are in MiB (1024 × 1024 bytes). Linux uses `MemAvailable`; macOS
estimates available/reclaimable pages from `vm_stat`. These are launch-time
snapshots, not resource reservations or process memory limits.

Existing explicit settings remain effective. Legacy `ZENITHBOT_` variants are
accepted; `AGENTSDOCK_` values take precedence, including explicit zero.
Configure the server service environment and restart when appropriate to apply
changes. A positive value selects a floor; zero or a negative value disables it.

A configured floor blocks only below the threshold; equality passes. Missing
memory readings do not reject work. Scheduled jobs apply both configured floors.
Errors report the observed memory and effective configured minimum. For example,
a server explicitly configured with a 2048 MiB floor reports:

```text
agent launch deferred: low available memory on the server: 567 MiB available; at least 2 GiB (2048 MiB) required to start an agent turn. Close unused applications or stop other agent runs on the server, then retry.
```

Provider readiness, exact chat ownership, explicit concurrency settings and
in-progress update transactions remain independent. Tests cover disabled
admission, positive floors, boundary equality, missing readings, configuration
precedence, scheduled jobs and the actual send entrypoint with a test provider.
No live host is deliberately put under memory pressure.
