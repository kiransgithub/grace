# GRACE delivery plan

## Delivery contract

GRACE is the parent GPU reservation and governance control-plane project. Child modules separate domain rules, persistence, API transports, provider adapters, policy, reconciliation, metering, and deployment. The existing GPU-scheduler demonstration remains unchanged.

This delivery starts implementation; it does not certify a production service. The first release target is **M0 architecture plus M1 executable safety kernel, contracts, tests, and Kubernetes deployment source**. A successful local test is not evidence of physical GPU behavior, cloud access, HA, DR, or non-bypass enforcement.

`backlog.json` is the machine-readable work record. `status.md` records the latest evidence and blockers. Agent assignments are finite tasks in this conversation, not permanent background services or scheduled automations.

## Accepted user decisions and safe defaults

| Topic | Decision |
|---|---|
| Locations | Rank any authorized existing cluster where data is attested available, unless a request adds a strict location constraint. |
| Environment | Dev and QA first. Never spill between environments implicitly. Production requires both platform enablement and explicit request authorization. |
| Cloud infrastructure | Existing Kubernetes clusters only; no direct GPU VM provisioning or implicit new-cluster creation. |
| Cluster onboarding | Declarative registration, connectivity/identity/admission checks, discovery, quarantine, canary, explicit activation. |
| Capacity promise | Best-effort queueing for dev/QA initially. Reserved capacity is an entitlement, not a promised compute-throughput SLA. |
| Future guarantees | Pool-backed scheduled reservations only after real scheduler holds, topology, reclaim deadlines, and isolation are verified end to end. |
| Identity | Okta OIDC backed by Active Directory; authorization remains server-side and tenant-scoped. |
| GPU mode | KAI fractional GPU allocation without MIG; certified HAMi enables CUDA memory caps and separately validated SM-utilization caps. Software enforcement is distinct from KAI-only accounting, hardware isolation and throughput guarantees. |
| Costs | Showback initially; preserve immutable attribution and versioned rates for controlled chargeback later. |
| Recovery | Every stateful dependency needs backup, restore, ownership/fencing and a tested recovery runbook. |
| Language | Deliver the safety kernel in the repository's Python ecosystem; profile before moving proven CPU bottlenecks to Rust. Network, database, orchestration, and GPU capacity dominate this control plane. |

## First-principles invariants

1. A successful reservation is a committed, tenant-owned capacity entitlement. Availability reads are advisory and cannot themselves reserve capacity.
2. Admission must atomically recheck authoritative ledger capacity and fresh infrastructure observations in the same capacity domain. Stale, unhealthy, unreachable, or conflicting capacity is not promiseable.
3. Fractions use integer units, never floating-point arithmetic. Physical GPU identity, model, memory, node, topology, and allocatable resource type remain distinct.
4. An at-least-once command path must not create duplicate workloads. Deterministic operation IDs, durable outbox state, reconciliation, and target-side ownership checks must work together.
5. Uncertain remote submission is not failure. Do not release its hold or launch in a second cluster until absence or effective fencing is proven.
6. Cancellation and expiry first prevent new dispatch, then request termination, then verify release. A cancellation API response is not evidence that a GPU is already free.
7. Every mutation checks tenant, project, environment, role, state/version, and idempotency scope. A client-supplied business-unit label is not authority.
8. Production opt-in is permission-gated, not a client-controlled boolean. Global disablement always wins.
9. Low GPU utilization is evidence for investigation, not sufficient evidence for destructive reclamation. Missing telemetry must never be interpreted as idle.
10. A database fencing token protects nothing unless the execution/admission/lease authority rejects an old token. A Kubernetes pod can continue executing through a control-plane partition.
11. Database state is authoritative for intent, ownership and entitlement; infrastructure is authoritative for existence, health and actual execution. Reconciliation joins facts without silently overwriting audit history.
12. Cost observations are deduplicated and immutable. Corrections are adjusting records; showback estimates remain distinguishable from actual provider charges.

## Parent and child module responsibilities

| Module | Owns | Must not own |
|---|---|---|
| Domain | State transitions, validated types, capacity arithmetic, environment policy, idempotency semantics | Network clients, HTTP status codes, cloud credentials |
| Store | Atomic commits, uniqueness, concurrency, durable leases/outbox, migrations | Placement policy, transport contracts |
| REST/gRPC | Authentication context, bounded input parsing, error mapping, operation/status APIs | Direct provider mutations, duplicate domain logic |
| Placement/policy | Candidate filters, explainable ranking, approved locations and data attestations | Hardware health fabrication or implied production approval |
| Execution adapter | SkyPilot request translation and status/termination observation | Independent reservation issuance or uncontrolled spillover |
| Reconciler | Observe, compare, quarantine, adopt owned operations, retire confirmed capacity holds | Treating a timeout as proof of non-existence |
| Metering | Time-series usage joins, rate versions, cost ownership, adjustment ledger | Reservation locking or production admission |
| Deployment | Kubernetes service accounts, workloads, probes, resource limits, network boundaries | Shipping embedded secrets or claiming manifests prove enforcement |

## Milestones and gates

Durations are planning ranges after staffing and environment access, not delivery promises. M2 and later depend on actual cluster, identity, security and data-owner access.

| Milestone | Indicative duration | Concrete output | Exit gate |
|---|---:|---|---|
| M0 — Design baseline | 1–2 weeks | ADRs, optimized ER model, API semantics, threat model, backlog, proposed SLO/DR matrix | Invariants and ownership decisions reviewed; unresolved external dependencies explicit |
| M1 — Safety kernel and contracts | 2–3 weeks | Modular control-plane source, local adapter, tests, REST mapping/protobuf, SQL design, Kubernetes source | Repeatable local verification; supported/unsupported behavior documented; existing demo unaffected |
| M2 — Real dev vertical slice | 3–5 weeks | Transactional PostgreSQL, outbox workers, Okta, one real KAI cluster, actual SkyPilot launch/cancel | End-to-end physical fractional GPU and non-bypass tests pass; no optimistic release |
| M3 — QA multi-cluster and recovery | 3–5 weeks | On-prem plus GCP/Azure existing clusters, onboarding, data-aware routing, gRPC integration, restore/failover | Partition/duplicate/cancellation races pass; tested restores for all stateful dependencies |
| M4 — Governed pilot | 3–4 weeks | 20–50 users, representative applications, showback, supported reaping policies, profiling | Security and FinOps reviews; observed reliability and fairness; runbooks and support owner |
| M5 — Production opt-in | 4–6 weeks | Production control boundaries, resilience, DR drill, capacity policy, incident response | Explicit service owner/security approval and production readiness review |
| M6 — Optimization and chargeback | Ongoing | Profile-guided performance, efficiency advice, rate governance, optional stronger reservations | Measured improvement with no broken safety invariant |

M1 can be built and tested here. M2–M5 cannot honestly be declared complete using fake GPUs, local mocks, YAML inspection, or a single-host cluster.

## Dependency and staffing sequence

1. **delivery_manager** establishes task IDs, acceptance criteria and evidence records while **data_architect** defines durable state/ER contracts.
2. **domain_engineer** implements the pure policy and capacity kernel. **root** defines REST/gRPC mapping against those domain types.
3. **integration_engineer** develops bounded execution adapters and failure injection against stable interfaces. **platform_engineer** packages the modules into Kubernetes pods and documents security/DR assumptions.
4. Root integrates changes and runs the combined suite; **verification_engineer** independently reviews safety and failure cases. The delivery manager records actual command results, not inferred success.
5. Real integration starts only after persistent-store correctness and execution semantics are tested. Security and cluster operators then validate the real admission boundary.
6. QA resilience work precedes pilot reclamation. Production remains disabled until the M5 gate.

Suggested ongoing delivery team: technical lead, 2 backend/control-plane engineers, 2 GPU/Kubernetes engineers, 1 SRE, and part-time identity/security and FinOps owners. Existing AIDA/portal integration can be an API consumer rather than a second reservation authority.

## Testing pyramid and evidence requirements

| Level | Evidence required | Does not establish |
|---|---|---|
| Pure unit | Transition/property tests, fraction bounds, strict data/environment checks, error classification | DB isolation, remote execution or GPU isolation |
| Store integration | Real PostgreSQL concurrent transactions, retry/idempotency, rollback, schema upgrade tests | Cloud or scheduler correctness |
| Contract | Generated/validated protobuf, REST schema, error/status parity, deadlines, limits | Identity federation against enterprise Okta |
| Adapter integration | Fake faults plus actual installed SkyPilot contract/version checks | Physical GPU behavior when executed against Kind |
| Real GPU | Fractional allocation, memory/compute behavior, actual metrics, cancellation and confirmed release | Cross-region DR or production SLA |
| Security | Denied direct pod/job/VM creation; forged metadata and token replay rejected; permissions audited | Root/cluster-admin impossibility of bypass |
| Resilience | Worker kill after remote success, DB failover, target partition, stale observation, restore/catch-up | Guaranteed RPO without measured replication and restore evidence |
| Performance | Declared hardware, dataset, latency distribution, contention and sustained arrival rate | Universal performance from a microbenchmark |

Required race cases include: simultaneous fractional claims on one device; retries with same key/different payload; cancel versus activate; expiry versus renewal; dispatcher crash before/after remote acceptance; duplicate events; unknown remote outcome; stale workload ownership token; cluster removal with active leases; stale inventory; database restore with still-running workloads.

## Release definition of done

- Task acceptance criteria pass with evidence path, command, timestamp and scope.
- Safety-sensitive changes have a second-agent review and regression tests.
- There is no credential, internal token, production connection string, or embedded live kubeconfig in the repository or package.
- Type/lint/test checks pass and API errors contain a safe error code, request/correlation ID, and retry guidance without secret-bearing traces.
- SQL and transport changes have compatibility and migration notes.
- Durations, retries, queue sizes, request sizes, cancellation, and timeouts are bounded; transient errors are distinguishable from permanent and ambiguous failures.
- Kubernetes manifests are validated at the documented level. Runtime, rollout, health probe and security claims require a real cluster run.
- Operators have a rollback/restore path that does not resurrect an old dispatcher or free uncertain capacity.
- Deferred capabilities and real-world gates are explicit in release notes.
- Existing demo behavior is preserved unless a separately approved migration changes it.

## Stateful recovery coverage

The authoritative target definitions are in `../execution-plan.md` section 10 and the operations runbook. Current proposals: control API 99.9% in dev/QA; regional database failover RPO 0 for synchronously acknowledged writes and RTO up to 5 minutes; site disaster RPO up to 5 minutes and RTO up to 60 minutes. Future production 99.95% is conditional on every critical dependency meeting the error budget. These are design targets, not measured guarantees. Recovery remains closed to new scheduling until surviving workloads and reservation ownership reconcile. Measure service RTO through that reconciliation and safe reopening, not merely database promotion or process startup. If reconciliation exceeds the target, record a service recovery target miss while preserving safety; do not exclude it from reported downtime. Application data/checkpoint recovery has its own targets.

| Stateful component | Required strategy | Mandatory drill |
|---|---|---|
| Reservation PostgreSQL | HA writer, encrypted backups/WAL, point-in-time recovery, restore credentials, writer fencing | Restore at a prior time while remote work remains alive; quarantine/reconcile before reopening booking |
| Durable outbox/workflow state | Prefer same transactional database initially; preserve message IDs and operation generations | Replay unacked messages without launching duplicate work |
| SkyPilot controller state | Version-specific supported backup/persistence, controller ownership and failover procedure | Restore/replace controller without orphan or double launch; verify ownership of surviving workloads |
| Inventory observations | Rebuildable cache with freshness and observation provenance | Drop cache/restart watcher and prove stale capacity is not sold |
| Usage/cost ledger | Retention, deduplication keys, backups and replay of immutable source facts | Restore and replay without duplicate charge attribution |
| Metrics/traces/profiles | Retention and object-store lifecycle; prioritize billing facts over reconstructible telemetry | Prove retention/restore and tenant access; missing metrics do not trigger reaping |
| Secrets/certificates/configuration | Approved secret manager, GitOps config, recoverable key ownership | Recover without plaintext backup secrets or long-lived developer cloud credentials |

During a partition, existing admitted jobs may continue under their local policy, but new allocations against uncertain capacity fail closed. Recovery starts read-only, inventories surviving remote work, resolves ownership generations, and only then reopens admission. Multi-region automatic failover is not a substitute for fencing the former writer and execution controller.

## Risk register

| Risk | Mitigation and owner |
|---|---|
| Fractional accounting, software caps and hardware isolation are confused | Publish the KAI/HAMi capability matrix; verify CUDA memory/SM enforcement and reject uncertified profiles — platform_engineer |
| Database locking is correct but duplicate remote execution occurs | Durable operation identity plus target-side checks and uncertainty reconciliation — integration_engineer |
| Per-request production flag bypasses separation | Global disabled default, privileged authorization, separate credentials/namespaces/clusters — root/platform_engineer |
| Scheduler cannot enforce future reservation holds | Keep assurance best-effort; block scheduled guarantees until evidence exists — domain_engineer |
| SkyPilot HA/version assumptions are inaccurate | Pin tested version, verify supported state backend and ownership/failover procedure — integration_engineer |
| Idle detection kills data loading or production service | Multi-signal non-prod policy, dry-run first, confirmation, checkpoint opt-in — platform_engineer |
| Chargeback mistakes estimates for actual charges | Explicit cost basis, invoice reconciliation and adjustment ledger — data_architect |
| Language rewrite consumes delivery effort without measured gain | Baseline Python; profile and only isolate CPU-heavy candidate modules if needed — root |

## Changes and escalation

Any change weakening a first-principles invariant requires an ADR and security review. A newly discovered infrastructure limitation updates the adapter capability registry and blocks affected requests; it must not silently relax a user's environment, data, security, topology, or capacity contract. Procurement, enterprise access, real GPU evidence, production enablement and external issue creation remain separate actions requiring the appropriate owner and authorization.
