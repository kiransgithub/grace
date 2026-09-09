# GRACE operations, security and stateful disaster recovery

## Status and authority

This is a design/runbook plus deployment source. The current API is an isolated,
single-process simulation with volatile state and a demo token. No real cloud,
Kubernetes GPU, SkyPilot, Okta or PostgreSQL integration is proven by the unit
tests. Optional PostgreSQL pods do not make the API durable. Neither this
runbook nor replica-count settings constitute a measured availability claim.

The initial authorized environments are dev and QA only. Location flexibility
means crossing *approved locations in the request's environment*, not crossing
from dev into QA or production. Production remains off at deployment and API
levels. A future per-request production flag will be necessary but insufficient:
the authenticated caller needs a production grant and current approval, an
approved production pool and a enabled production control-plane deployment.

Only GRACE can authorize reservations; observed infrastructure is authoritative
for actual execution and health. Never overwrite real observation with a desired
DB value. An unknown or stale state is not available capacity. Allocation leases
do not become reusable until actual release/termination has been observed.

## Pod delivery and separation of responsibility

| Pod/module | Current state | Required durable implementation |
|---|---|---|
| API | One simulation pod, REST and gRPC share one process | Stateless replicas; authentication; transactional repository |
| Reservation core | Python module in API | One transaction for calendar/allocation/outbox; stable fencing epochs |
| Dispatcher | Adapter contract/library only | Durable worker, deduplicated commands, downstream enforcement |
| Discovery/reconciler | Synthetic inventory in simulation | Per-cluster agents/watchers; periodic authoritative full scans |
| Idle policy/sweeper | Core policy functions | Durable scheduled decisions, notification outbox, workload-class policies |
| PostgreSQL | Optional unused dev-only child chart | Approved external HA/PITR service and tested SQL repository |
| Cost/audit export | Contracts and design | Immutable event sink, replay-safe consumer, reconciled warehouse |

The parent Helm chart owns versioned child modules. API, workers, reconciliation
and metering must scale independently once their contracts are durable. Do not
split transactional reservation logic into independent microservices that cannot
commit atomically. Do not run an in-memory API behind multiple replicas.

Pods use a non-root UID, seccomp RuntimeDefault, dropped capabilities, read-only
root filesystem, CPU/memory requests/limits and bounded temporary storage. The
API ServiceAccount does not mount a Kubernetes token or receive cluster RBAC.
Container/pod hardening is not tenant admission enforcement. Images must be
scanned, signed and pinned by digest in the deployment promotion pipeline.

API liveness asks whether the process can answer, not whether the DB/cloud is up.
Durable readiness will require the correct schema, authoritative DB access,
valid deployment configuration and acceptable reconciliation state. A target
cluster outage should disable that pool, not kill the entire API. Dependency
timeouts and circuit breakers must be bounded; provider failures may not cause
unbounded request goroutines/threads or in-process queues. Retry only idempotent
operations with jitter, deadline and an explicit retry budget.

## Identity, gateway and production boundaries

1. AD remains the enterprise directory; Okta is the OIDC authority used by GRACE.
   Validate issuer, audience, approved signing algorithm, key, expiry and relevant
   not-before claims; do not accept a token simply because it parses. Handle
   key rotation with bounded JWKS caching. Reject unknown signing keys when the
   issuer cannot be consulted; never disable validation during an Okta incident.
2. Derive tenant/application authorization from validated claims and a managed
   entitlement mapping. Client-provided business-unit, cost-center, queue or
   environment labels are not authorization. Map service identities separately
   from human interactive sessions. Rotation/revocation and audit must be tested.
3. The public gateway requires TLS, rate limits, request-size/deadline limits and
   authenticated scopes. Internal services use workload identity and preferably
   mTLS. Keep REST/gRPC error semantics equivalent and avoid secret-bearing errors.
4. End users cannot submit GPU-requesting pods, controllers, node pools or direct
   cloud GPU VMs. GRACE/SkyPilot dispatch identities alone receive the scoped
   permission. Restrict the SkyPilot API and CLI credentials too; a public
   SkyPilot endpoint with launch privileges would bypass the gateway.
5. Kubernetes admission validates the server-issued reservation binding,
   generation/fencing token, tenant, resource quantities, allowed scheduler,
   approved queue, node selectors and service account. Check controller-created
   pods and update paths. Replaying a valid label is not sufficient. Implement
   a target-side lease cache/enforcer or equivalent atomic gate; a network call
   to the central DB from every admission decision is not the only design.
6. Block privileged pods, hostPath GPU-device mounts, host access, unapproved
   device requests, direct binding and mutation of admission/RBAC/queue controls.
   No system can stop a trusted cluster/org administrator who can change all
   controls; emergency admin access is the explicit audited exception.
7. Unknown GPU consumers trigger quarantine of affected capacity and a security
   incident. Do not automatically terminate an unidentified workload: identify
   its owner and criticality, follow the approved containment policy, and record
   the decision. A production emergency kill is not a generic reconciler action.
8. Production idle auto-termination is off by default. Future production support
   requires application-specific SLO/replica-floor/approval policy. Low utilization
   alone does not prove idle work, in any environment.

### Network controls

Default-deny ingress and egress; explicitly allow gateway-to-API and
worker-to-target flows. The prototype chart permits DNS and configured selectors
only; it does not have cloud credentials. Future egress allowlists cover DNS,
PostgreSQL TLS, Okta/JWKS via an approved egress proxy, SkyPilot API, selected
Kubernetes APIs, telemetry sinks, secret manager and notifications. Standard
NetworkPolicy cannot enforce arbitrary DNS hostnames; use the enterprise egress
gateway or a verified CNI FQDN policy. Account for node-local DNS deployment if
present; do not broaden all egress solely to repair a DNS misconfiguration.

## Existing-cluster onboarding procedure

1. Register an inventory endpoint in DISABLED state with stable cluster ID,
   location, environment, network endpoint, expected CA and credential reference.
   Registering an endpoint does not create a cluster or promise new capacity.
2. Confirm the owner, backup/recovery dependencies, maintenance window, cost rate
   source and permitted data classifications. Store credentials only as managed
   references. Establish per-cluster least-privilege execution and observer roles.
3. Verify API connectivity, version compatibility, clock skew, GPU inventory,
   healthy device plugin/operator, KAI queue bindings, resource limits, fractional
   accounting, admission controls, logs and metrics. KAI-only sharing is accounting;
   certified KAI/HAMi adds software-enforced CUDA memory limits and optionally
   validated SM-utilization caps. Verify library injection, effective limits and
   anti-opt-out policy; this is distinct from MIG hardware isolation.
4. Import observed GPUs and topology with last-seen timestamps; deduplicate by
   stable provider/cluster/node/device identity, not node name alone. Reconcile
   existing workloads and unexplained consumption before any pool is enabled.
5. Run synthetic identity/admission tests and a separately approved canary job on
   the target's real GPUs. Prove cancel, warning, expiry, resource release and
   telemetry attribution. Onboarding acceptance must not use fake GPU metrics.
6. Enable only approved dev/QA tenant pools. Production onboarding is a separate
   gate. Emit an immutable onboarding event and record the canary evidence.

Updating endpoint metadata cannot silently relocate reservations or change a
cluster's environment. Credential rotation is independent of reservation identity.
Offboarding first denies new reservations, drains existing leases, reconciles
release, then removes execution permissions; keep the audit/cost records.

## Stateful DR catalogue and proposed objectives

The objectives below are planning targets, **not tested SLAs**. Confirm budget,
site connectivity, managed-service guarantees, retention and business impact
before approval. HA failover within one site and recovery after losing the site
are distinct tests. A cold restore must include dependencies and reconciliation,
not merely the time for a database process to start.

| Stateful component | Owner | Protection and recovery requirement | Proposed RPO / RTO |
|---|---|---|---|
| GRACE PostgreSQL: reservations, leases, idempotency, outbox, prices | Database SRE + control-plane owner | Local synchronous HA; encrypted base backups + continuous WAL to independent failure domain; PITR and restore drills | Regional synchronous acknowledged writes: 0 / <=5 min; async site loss: <=5 min / <=60 min including safe reconciliation and reopening |
| SkyPilot execution state / DB / service-owned persistent files | GPU platform SRE | Back up all state required by the pinned SkyPilot deployment; independently validate supported HA topology; recover realm before resubmission | <=5 min / <=60 min, to be validated against supported deployment |
| Persistent workflow/command queue and consumer offsets | Messaging SRE | Multi-zone replication, retained events, cross-site replication/backup; replay with deduplication | <=5 min / <=60 min; outbox replay is authoritative for commands |
| Immutable audit and usage events | Security + FinOps data owner | Append-only/WORM policy as required; independent replicated retention; sequence/gap validation and replay manifests | <=5 min export lag / <=4 h reporting recovery; critical authorization event committed in DB transaction |
| Showback warehouse, billing exports and rate snapshots | FinOps | Rebuildable from retained usage facts and versioned rates; provider invoice reconciliation | <=24 h / <=24 h, without blocking safe reservations |
| Prometheus/metrics long-term store | Observability SRE | Remote write, replicated long-term storage; preserve device/workload identity | <=15 min / <=4 h; missing metrics disables idle inference |
| Notification delivery state and suppression/dead-letter queues | Application platform | Durable outbox, receipts, deduplicated retries, retention | Same as source DB / <=4 h; no reclaim solely because a notification failed |
| Secrets, PKI, KMS metadata and recovery grants | IAM/Security | Managed vault HA and encrypted recovery; off-site key recovery tested with dual control | Per security baseline; recover before decrypting/restoring protected state |
| GitOps, Helm values, approved policy, schemas, OCI images and signatures | Platform engineering | Remote Git, independent mirrors/backups, immutable tagged artifacts | Approved release: 0 / <=60 min dependency recovery |
| Training checkpoints, dataset/catalog versions and artifact stores | Application/data owner | Versioned durable object/file storage with location-specific restore plans; retain access/IAM and encryption dependencies | Workload-specific approved RPO/RTO; GRACE cannot promise application recovery without it |
| Ceph/PVC/object storage backing any of the above | Storage SRE | Independent backups/replication appropriate to failure domains; snapshots alone in the same failure domain are insufficient | Must meet each consuming service objective |
| Optional bundled dev PostgreSQL | Named developer + platform owner | Dev PVC plus explicit export/PITR plan; chart has no backup controller | <=24 h / <=8 h proposed; never a production control-plane database |

Do not create a persistent store without an owner, retention rule, restore
procedure and verified encryption-key dependency. Persistent caches must either
be declared disposable or appear in this catalogue. The simulation's API memory
is deliberately disposable and cannot be backed up as a durable ledger; a
restart starts a fresh simulation. Notification/analytics lag must be visible.

### DR failover sequence: fence before allocating

1. Declare incident scope, incident commander and approved target site. Pause
   new allocation/renewal/dispatch writes; return an explicit retryable unavailable
   status. Existing healthy work continues unless the application's policy says
   otherwise. A worker lease timeout does not prove the remote job has stopped.
2. **Fence the old writer and old dispatchers** through the actual infrastructure
   and identity control plane. DNS changes alone are not fencing. Revoke/reroute
   write credentials and isolate the old leader; confirm target enforcers will
   reject old allocation epochs. If isolation cannot be proved, stay unavailable.
3. Restore/promote PostgreSQL and dependent key material using the approved
   recovery point. Record the precise recovered LSN/time, any possible lost
   interval and a new allocation authority epoch. Keep API writes closed.
4. Restore the matching execution state, durable queues and policy/schema
   versions. Check schema compatibility, backup integrity and access. Do not
   replay launch commands yet; a restored outbox may omit or repeat commands.
5. Discover workloads from **every** target cluster and SkyPilot realm. Match
   reservation ID, deterministic operation identity and fencing epoch. Rebuild or
   investigate allocations created after the restored recovery point. Quarantine
   orphan/uncertain devices; never call them free because their DB row is missing.
6. Reconcile calendar commitments, dispatch acknowledgements, external workload
   existence, terminal resource release and pending cancellations. An async DR
   data-loss window can lose a reservation or idempotency key: preserve incident
   evidence and prevent retries from becoming a second actual launch.
7. Compare inventory freshness and health; replay safe, deduplicated outbox
   actions only after adoption/cancellation decisions. Notify affected owners
   about lost promises; do not manufacture a historical no-double-booking claim.
8. Approve a small canary allocation/release in each re-enabled pool. Resume
   writes incrementally; pools with uncertainty stay unavailable. Measure actual
   end-to-end RTO from incident to safe admission, and RPO from evidence.
9. Reconcile audit/cost gaps and provider usage, archive incident logs, then plan
   controlled failback. Failback requires the same fencing and reconciliation
   gates; never bring the former primary online as a concurrent writer.

A strict zero-loss cross-site reservation guarantee requires a correspondingly
strong database/quorum design with latency and availability trade-offs. Async
replication cannot promise RPO 0. The recovery gate is mandatory even when local
database promotion succeeds automatically.

## Failure and exception handling matrix

| Event | Safe immediate action | Recovery criterion |
|---|---|---|
| DB commit result uncertain | Return operation-unknown with stable request identity; do not launch elsewhere | Query committed idempotency/operation record on authoritative DB |
| SkyPilot submit timed out | Mark uncertain; retain reservation and prohibit spillover | Correlate deterministic workload identity before retry/adoption |
| Target cluster unreachable/stale watch | Exclude affected pool from fresh commitments; retain existing leases | Full scan and fresh health observations |
| Cancel races activation | CAS/versioned state transition and fenced command; retain capacity meanwhile | Observe workload absent/stopped and terminate lease exactly once |
| Release times out | Keep RELEASING/UNCERTAIN; do not increment free capacity | Actual pod/device release observed |
| Metrics unavailable | Mark telemetry unknown; suspend utilization-based reclaim | Fresh signals over complete policy observation window |
| Notification delivery fails | Bounded retries/dead-letter + operator alert | Delivery policy satisfied; hard contractual expiry remains a separate explicit policy |
| Idle candidate has low compute but data loading/checkpointing | No immediate kill; evaluate class/signals and grace period | Proven applicable idle condition or explicit cancellation |
| Invalid/unauthorized request | Fail fast before resource/calendar mutations | Correct caller grant and validated input |
| OOM/restart of current simulator | Mark session reset; run with one replica only | New synthetic session; never describe lost state as recovered |

Use structured error codes, correlation IDs, bounded details and redaction. Do
not retry invalid requests, authorization failures or permanent unsupported GPU
shapes. Configuration errors must fail at startup instead of creating a partly
operational server. Reservation writes and audit/outbox facts commit together;
network work occurs after commit, never while holding capacity locks.

## Acceptance evidence required before critical applications

- Container build, Helm render/schema/security checks, actual pod startup,
  read-only filesystem and authenticated REST/gRPC probes on approved clusters.
- Single-authority concurrency tests on **real PostgreSQL**, not only threads
  sharing a Python object; verify overlapping fractional and time-window leases.
- Restart/failover tests showing committed reservations survive; intentional
  response-loss tests prove idempotency. Multi-replica API does not imply SkyPilot
  itself supports active-active operation.
- Actual KAI fractional multi-tenant workloads verify quantity/memory accounting,
  enforcement, fairness, release and GPU telemetry; no MIG assumed.
- Forced dispatch uncertainty, delayed observation, admission replay and
  cancellation races preserve safety and recover within agreed budgets.
- Cross-site restore drill with old-writer fencing, lost-WAL-window simulation,
  workload adoption and reconciliation before capacity becomes promiseable.
- Okta key rotation/outage and identity revocation, least-privilege tests,
  production enablement denial and bypass attempts at every privileged path.
- Application checkpoints restored on approved alternate location, with data
  availability/access verified; confirmation checkbox alone is MVP attestation.
- Showback replay produces the same totals; usage facts, price snapshots and
  provider invoices reconcile within an explicitly agreed tolerance.

Record command/test version, environment, operator, timestamps, output artifacts,
pass/fail and unresolved caveats for every gate. Keep the production switch off
until these are signed off by platform, security, database and application owners.
