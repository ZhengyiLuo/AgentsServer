# Available-memory admission

Before starting a new agent turn, AgentsServer checks estimated **available
memory on the server machine**, not the phone/desktop client's RAM. This does
not measure how much RAM AgentsServer itself uses, and a rejection does not
establish an out-of-memory crash or memory leak.

## Defaults and overrides

| Setting | Default | Purpose |
| --- | ---: | --- |
| `AGENTSDOCK_MIN_START_AVAILABLE_MEM_MB` | 512 | Minimum available memory before a new agent turn, including queued turns and goal resumes that use normal admission. |
| `AGENTSDOCK_JOB_MIN_AVAILABLE_MEM_MB` | 4096 | Additional reserve before launching a scheduled job. |

Both values are in **MiB** (1024 × 1024 bytes); the existing `_MB` environment
variable names are retained for compatibility. Linux uses `MemAvailable`;
macOS estimates available/reclaimable pages from `vm_stat`. They are launch-time
snapshots, not resource reservations or process memory limits.

Explicit settings are unchanged. Legacy `ZENITHBOT_MIN_START_AVAILABLE_MEM_MB`
and `ZENITHBOT_JOB_MIN_AVAILABLE_MEM_MB` are still accepted; their respective
`AGENTSDOCK_` settings take precedence, including an explicit zero. Set variables
in the **server service's environment**, not just an unrelated terminal, and
restart that service when it is safe to apply configuration changes. A positive
value sets a floor; the existing nonpositive-value opt-out remains supported,
but disabling the guard is not recommended as a low-memory troubleshooting step.

Admission blocks **below** the configured floor; equality passes this particular
check. Concurrency limits, provider readiness, maintenance/update fences and
per-chat ownership checks still apply. An unavailable memory reading retains
the previous behavior: no memory-based rejection. Scheduled jobs must pass both
the global turn guard and their additional job guard; the first blocker is
reported. Their 4096 MiB default remains conservative because unattended work
can wait instead of competing with interactive work.

## Why 512 MiB

The previous 2048 MiB default prevented otherwise usable low-RAM hosts from
starting a turn. A 1024 MiB floor would still reject the reported **567 MiB**
case. The new 512 MiB default admits that case while retaining a nonzero guard.
This is a less restrictive policy choice, **not proof that every provider,
project or concurrency level can run safely with 512 MiB available**. Operators
can retain a higher floor for their workloads.

When the guard blocks, the existing HTTP 503 detail now says, for example:

```text
agent launch deferred: low available memory on the server: 480 MiB available; at least 512 MiB required to start an agent turn. Close unused applications or stop other agent runs on the server, then retry.
```

The minimum comes from the effective configuration, not a hardcoded message.
For example, an operator retaining 2048 MiB will see **2048 MiB required**.
Scheduled-job memory messages similarly show their effective job minimum.
Free memory on the server, let other work finish, or use a host with more RAM
if this happens repeatedly; merely refreshing the client does not free RAM.

## Validation scope

Focused regressions execute the actual admission helpers with controlled
memory readings: below/at/above the floor, the 567 MiB report, canonical/legacy
overrides, explicit opt-out, missing readings, scheduled jobs and independent
concurrency/maintenance guards. These are policy and error-message tests, not
provider load benchmarks. No live host is deliberately placed under memory
pressure, and end-to-end provider reliability on a constrained 512 MiB reserve
remains unverified. No existing service needs restarting to run these tests.

Local verification passed 371 focused tests across admission, session/backend
lifecycle, scheduled jobs, server update/restart, host hardening and Codex goal
resume suites. The real send entrypoint was exercised with a mocked provider:
567 MiB reaches admission, while low-memory rejections retain HTTP 503, include
the effective minimum and recovery advice, and release the turn reservation.
This was isolated local verification, not a full CI or constrained-host load run.
