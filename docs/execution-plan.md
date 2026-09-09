# GRACE execution blueprint

GPU Reservation, Allocation & Control Engine • design baseline 2026-09-09 • v0.1

## Delivery boundary

This blueprint and the adjacent contracts/schema define the target. The executable
delivered in this milestone is a **non-durable, single-process simulation safety
kernel** with real REST/gRPC transports, not a completed enterprise service. Its
allocator and adapters can be tested without GPUs; running that test does not certify
SkyPilot/KAI, PostgreSQL multi-replica behavior, Okta or DR. The older laptop demo is
preserved. Delivery status and evidence are maintained in `delivery/` and
`verification.md`.

## 1. Requirements frozen from owner decisions

| Decision | Rule |
|---|---|
| Location flexibility | No required location: choose any **authorized cluster within the requested lifecycle environment**, only where data exists and residency/trust policy permits. Explicit required location: no spill. |
| Infrastructure | Existing Kubernetes clusters only (on-prem, GKE, AKS); no cloud VM or new cluster creation. Existing node-pool autoscaling is cluster-owner policy, not a guarantee. |
| Default service class | Best-effort immediate allocation or queued request in the durable release; no advance capacity or fractional compute SLA at launch. Simulator fails capacity shortage explicitly rather than claiming a durable queue. |
| Scheduled assurance | Add pool-backed reservations only after a native capacity-hold/bind protocol, topology, no-show/overrun and failure spare-capacity tests pass. Queue quota alone is not a calendar reservation. |
| Identity | AD is corporate identity source; Okta is OIDC issuer; group/SCIM sync maps to tenant, BU, project and role IDs. No AD password forwarding. |
| Cost | Showback first. Decimal rate/versioned usage ledger supports future approved chargeback; chargeback disabled. |
| Production | Separate `prod` infrastructure and identity boundary. Global gate + eligible app + approved requester + request opt-in. A user checkbox cannot override authorization. v0.1 deliberately rejects production. |
| GPU sharing | Full device or KAI fractional sharing, no MIG. Explicit modes separate KAI accounting, HAMi CUDA memory enforcement and separately certified HAMi memory plus SM-utilization caps. Version alone cannot certify the runtime; software caps are not MIG isolation or throughput guarantees. |
| DR | Every stateful dependency must have recovery ownership, backup scope, restore procedure and tested RPO/RTO. |

Production workload class and lifecycle environment are different concepts: allowing
production does not turn a dev cluster into a production environment or permit idle
termination of inference replicas. Production critical services use dedicated
full-device capacity unless a separately accepted isolation/risk policy allows sharing.

## 2. First-principles invariants

1. A reservation is authorized desired state; a GPU observation is measured fact.
2. No mutation is accepted if durable reservation authority cannot commit.
3. Promised capacity must be physically fit-able: type, per-device memory/fraction,
   CPU/RAM, node count, topology, data and trust domain. Sum-of-cluster-GPUs is not enough.
4. Capacity claims count once by allocation identity across ledger and observed state.
   Use their **union of claims**, not `min(free_in_db, free_in_infra)`: disjoint unknown
   claimants can make the minimum unsafe. Shared KAI reservation pods count the parent
   physical GPU once, while children consume fractional capacity; do not double-charge
   or double-subtract the parent and its mapped children.
5. Never interpret missing/stale metrics, a timeout, pod deletion request or expired
   DB lease as proof that the GPU is free. UNKNOWN/RELEASING keep their claims.
6. No cross-cluster retry until the original attempt is proven absent or fenced at
   its actual execution/output authority. A fencing integer only works where enforced.
7. A PostgreSQL transaction cannot atomically commit a Kubernetes/cloud API action.
   Use outbox + explicit intent/attempt + idempotent reconciliation. Do not promise
   exactly-once physical launches; jobs writing business outputs need deduplication or
   transactional commit protocols too.
8. Best effort never becomes guaranteed simply because priority is high. Priority,
   fair-share weight, quota and contractual capacity are separate policy dimensions.
9. No user-controlled labels, headers, priority classes, scheduler names, credential
   references or environment booleans confer authorization.
10. Fractional accounting is integer millicards (1..1000 per physical device) and MiB.
    At any overlapping instant, total admitted share <=1000 and working-set budget
    <= usable VRAM. HAMi can enforce the resulting CUDA memory cap in certified
    runtimes; the ledger itself does not implement the CUDA interception layer.

## 3. Parent system and child modules

One parent product and standalone repository (`kiransgithub/grace`), with independently owned boundaries.
Start as a modular application; split deployables only when scaling, fault containment
or privileges justify it. Do not start with a dozen mutually dependent microservices.

| Module | Owns | Must not own |
|---|---|---|
| `domain` | Rules, integer units, state transitions, per-device feasibility, typed errors | HTTP, cloud credentials, SQL connections |
| `transport` | REST/gRPC parsing, limits, identity binding, error mapping, deadlines | Independent copy of allocation rules |
| `store` (next milestone) | PostgreSQL transactions, leases, idempotency, outbox, tenant scoping | Physical workload execution |
| `adapters` | Certified cluster registry and SkyPilot task/lifecycle translation | Autonomous unapproved spillover, direct bypass submission |
| reconciliation worker | Inventory watch/relist, state evidence, orphan detection, release confirmation | Synthesizing health when telemetry disappears |
| lifecycle worker | Expiry/no-show/cancel/idle decisions and notification intent | Removing capacity before observed cleanup |
| metering worker | Immutable usage/rate facts and corrections | Reading GPU util% as invoice truth |
| admission controller | Bounded launch-authorization checks near cluster; signed capabilities | Broad tenant identities or remote long-running admission calls |
| delivery agent | Backlog, acceptance evidence and blockers in Git | Production control or persistent background work by implication |

Parent Helm chart composes child control-plane/execution/state modules. External
state references are explicit. The initial memory-backed executable is limited to
one pod; HA replicas are blocked until the durable repository exists.

## 4. Technology decision and performance budget

Python 3.12 domain + gRPC + async REST + native SkyPilot Python adapter. PostgreSQL
is the durable target. The bottleneck at hundreds of users is expected to be
external provisioning and transaction contention, not language arithmetic; this is
a hypothesis to benchmark, not a performance claim. Avoid Rust/Python serialization
and operational duplication before measurements justify it. A pure placement library
can later become Rust behind the same internal contract if profiles show material CPU
cost. Rust does not cure stale observations, unsafe retries or distributed double booking.

Target tests: 500 registered users, 500 apps, 1,000 simultaneous submissions against
one pool; sustained and burst QPS finalized from onboarding measurements. Provisional
control API p95 <250ms reads and <750ms commit-only writes at 50 rps; GPU startup is a
separate histogram, never included in API synchronous response time. These are design
targets, not results. Bound body to 64 KiB, page size to 200, retries and queue lengths.

The stated 70-node, dual-GPU on-prem estate is 140 physical devices before unhealthy
cards, maintenance, environment partitioning and operational headroom. Fractions increase
concurrent logical placements, not the estate's physical compute or memory. Import actual
GPU models/VRAM/topology during discovery; never seed those 140 devices as healthy from a
spreadsheet assertion alone. The executable demo uses four explicitly synthetic devices.

Use indexed candidate sets then filter/fit; deterministic ordering and stable tie-breaks.
Lock GPUs in stable order inside short transactions; never call SkyPilot while holding
DB locks. Use a sweep of interval boundaries, not sum of every overlapping interval
(disjoint reservations may each overlap the proposed long interval). Index live leases,
outbox ready times, tenant lookups and KAI workload identities. Add telemetry partitions
only on measured volumes; keep raw high-cardinality samples out of reservation DB.

## 5. Request and cancellation protocol

1. Authenticate token, verify issuer/audience/signature/expiry, resolve roles/app membership.
2. Validate shape, integer ranges, application ownership, environment and data attestation.
3. Filter registered/certified/fresh clusters. An unavailable optional cluster does not
   stop others; stale candidates are excluded with reason codes.
4. Durable target: transaction claims idempotency key scoped by tenant+subject+method,
   compares canonical body digest, reserves feasible capacity, appends outbox and audit.
5. Return committed reservation; activation returns a long-running operation.
6. Worker records dispatch intent/epoch, pins one certified context and calls SkyPilot.
7. Observe SkyPilot ID, Kubernetes UID and eventual KAI GPU assignment. A task-generated
   name is correlation, not a substitute for this evidence.
8. If actual KAI assignment differs from accounting placement, reconcile the whole
   allocation atomically; do not claim GRACE can pin arbitrary UUIDs through SkyPilot.
9. Cancel/expire: commit STOP_REQUESTED, stop recovery controllers, request graceful
   shutdown, enforce bounded grace, and observe absence + device readiness before release.
10. Unknown cluster partition: leave claims held, block duplicate replacement and notify SRE.

Future reservations need both real backend holds and backfill overrun protection.
KAI fair-share, HAMi utilization caps and priority are cluster/runtime mechanisms, not durable cross-cloud reservation
semantics. One authority chooses placement; SkyPilot alternatives/recovery must remain
within that lease or acquire a new authorized lease with explicit single-owner handoff.

## 6. Identity and mandatory gateway

AD → Okta federation → short-lived OIDC access tokens → GRACE. Use stable `sub`, tenant
membership and application service identities; email is display/notification metadata.
Reject wrong issuer/audience/algorithm/expired token and unknown `kid`; refresh JWKS
boundedly and fail closed if unverifiable. mTLS and workload identity for internal calls.
Keep user identities distinct from dispatcher cloud/Kubernetes credentials. v0.1 uses
a deliberately separate fixed demo bearer secret and never pretends it is Okta.

Only dispatchers may create GPU-bearing pods or their parent controllers. Admission
must match **both** `nvidia.com/gpu` and fractional KAI annotations, runtime classes,
device mounts and node access. A fractional Sky task may look CPU-only to Kubernetes;
integer-resource quotas alone miss it. Deny changes to approved metadata/priority,
service accounts, nodeName/bindings, privileged/hostPath/hostPID/hostNetwork, CSI/CDI
device access and token minting. Check actual API caller and controller ownership,
not only pod `serviceAccountName`. Users cannot alter policies or dispatcher identities.

Use fail-closed HA admission with short local capability verification. Signed launch
capabilities bind reservation/attempt/epoch/context/namespace/spec hash/time/maximum
replicas and are consumed/reconciled to prevent replay. Workload child pods need bounded
controller ownership-aware tokens. NetworkPolicy complements RBAC, it does not replace it.
Cluster administrator remains break-glass trust boundary; audit and limit elevation.
For HAMi pools, admit only the approved isolator's specific library/preload mounts,
and protect its webhook, namespace labels and injected settings. Deny per-pod
`kai-resource-isolator.io/inject=false` and namespace webhook-ignore opt-outs by
ordinary users. Broad hostPath access is not an acceptable way to enable HAMi.
Record approved image, CUDA/driver/HAMi versions and real runtime limit evidence.

## 7. Seamless existing-cluster onboarding

Register a declarative cluster descriptor, never a raw long-lived credential in API.
Reconcile: REGISTERED → DISCOVERED → VALIDATING → CERTIFIED → ENABLED. Validate reachability,
OIDC/workload identity, namespace/RBAC/admission, KAI version+CRDs+sharing, NVIDIA runtime,
device inventory/DCGM, clock, metrics freshness, model labels, storage/data attestations,
egress/DNS, image registry, workload startup and teardown, and fractional isolation policy.
Certification is a versioned matrix (SkyPilot client/server, Kubernetes, KAI, GPU Operator,
driver, image). Changes invalidate certification. Failure quarantines the cluster without
impacting others. Drain stops new allocation; unregister requires all owned work released.

SkyPilot 0.13 and KAI 0.17 are reference pins from the demo, not a certified fraction
combination. Fraction translation is experimental until a real-GPU test proves container
visibility, KAI fraction, correct CPU/RAM, Ray scheduling, attribution and teardown.
Never send `nvidia.com/gpu: 0.25`; never substitute direct kubectl when SkyPilot cannot
express a governed requirement. See `skypilot-kai.md` for integration gate details.

## 8. Idle, expiry and telemetry

Dev default: 20 minutes suspected idle, notify, 10-minute grace, bounded save/stop.
QA batch: no-show/start deadline and progress-aware timeout; no generic low-util kill
during preprocessing, checkpointing, I/O or distributed barriers. No metrics means
UNKNOWN, not idle. Notifications go to verified directory email initially; delivery
is retryable and deduplicated by reservation+event+policy version. Channel provider
credentials/templates must be supplied before email is enabled.

Use DCGM + kube-state + pod-resource/KAI identity mapping + framework progress signals.
For fractions, GPU-level utilization is shared and cannot be copied to each job as if
attributed. Label unattributed/shared estimates explicitly. Collect energy, SM activity,
memory/DRAM traffic, GPU errors, throttling, PCIe/NVLink, CPU/data/network wait, queue and
startup latency. Targeted Nsight/PyTorch capture requires approval and captures sensitive
traces; schedule away from production neighbours. Track throughput and cost-per-output,
not just utilization. Avoid universal numerical efficiency targets across workloads.

## 9. Showback ledger

Allocate integer millicard-seconds and memory-MiB-seconds, with timestamps and ownership
snapshotted when consumed. Measure allocated/reserved/executing intervals separately.
Record decimal rates+currency+effective dates, hardware/provisioning/idle/storage/network
components and correction events. Fraction × wall time is allocation showback, not proof
of exclusive compute consumption. Under shared hosts, total allocated costs plus idle
and platform overhead must reconcile to pool bill without double charging the parent GPU.
No single inferred discount applies to every application. Export BU/project/app/env/provider
trends and idle opportunities to warehouse; provider invoice reconciliation is delayed.
Future chargeback adds approved allocation rule, invoices, dispute/correction workflow
and financial ownership. It must not be enabled solely by a developer flag.

## 10. Availability and DR targets (proposed, require acceptance tests)

Dev/QA control API target 99.9% monthly; future production 99.95% only after all critical
dependencies meet budget. Three failure domains are preferred for quorum control plane
and synchronous HA DB. If only two sites exist, use a third witness/fencing authority
or one active site with controlled DR; do not build two independent active writers.
Regional DB failover target RPO 0 for synchronously acknowledged writes, RTO <=5 minutes.
Site disaster target RPO <=5 minutes and RTO <=60 minutes initially, with new scheduling
paused until infrastructure reconciliation completes. Data/checkpoints have app-specific
RPO/RTO and may dominate recovery; a metadata restore does not recover model progress.

Critical caveat: asynchronous DB recovery may lose reservation claims while GPUs still
run. Fence the old writer AND executor credentials/network paths, rotate recovery epoch,
inventory all clusters and recover unknown claims before admitting new work. Merely
incrementing a DB number in a recovered copy does not fence the old site. Preserve
credential/PKI recovery and audit; test old-site return, packet partition and unkillable
workloads. Detailed stateful register and restore runbook: `operations.md`.

OSS SkyPilot currently supports external PostgreSQL recovery but not multi-replica API
servers. Operate supported independent execution realms with recovery runbooks, or
evaluate the vendor HA option. Durable GRACE can accept intent when an executor is down,
but must report degraded provisioning and must not call this end-to-end active-active HA.

## 11. Error and retry contract

| Condition | gRPC / REST | Action |
|---|---|---|
| Invalid type, fractional float, missing data | INVALID_ARGUMENT / 400 | Fail immediately; field-specific error |
| Missing/invalid identity | UNAUTHENTICATED / 401 | No domain action |
| Tenant/app/prod/location denied | PERMISSION_DENIED / 403 | Audit denial; no retry |
| Cross-tenant object lookup | NOT_FOUND / 404 | Avoid object enumeration |
| Same idempotency key, different canonical body | ALREADY_EXISTS / 409 | Return conflict, never replay wrong request |
| Capacity exhausted | RESOURCE_EXHAUSTED / 429 | Queue only when durable queue committed; simulator fails |
| Stale observation/policy prerequisite | FAILED_PRECONDITION / 412 | Refresh/reconcile; do not assume free |
| Etag/concurrent state changed | ABORTED / 409 | Re-read; bounded retry with same intent |
| DB unavailable | UNAVAILABLE / 503 | No reservation committed, safe retry with same key |
| SkyPilot deadline after submit | DEADLINE_EXCEEDED / 504 | Persist UNKNOWN attempt; reconcile before retry |
| Unexpected defect | INTERNAL / 500 | Correlation ID, redact details; alert |
| Feature not delivered | UNIMPLEMENTED / 501 | Explicit; never success-shaped placeholder |

PostgreSQL 40001/40P01 retries need whole-transaction bounded backoff+jitter and an
absolute deadline. Dead letter repeated deterministic failures. Cancellation and
timeout are separate concepts. Handle process signals gracefully and drain work.

## 12. Execution sequence and staffing

| Milestone | Estimate with prerequisites | Deliverables / exit gate |
|---|---|---|
| M0 design baseline | 1–2 weeks including owner review | Decision log, threat model, ER, proto, parent/child modules, risk ledger; initial source delivered in this iteration |
| M1 safety foundation | 2–3 weeks including team acceptance | Domain simulation, typed APIs, schema, experimental task compiler, Kubernetes source; initial executable delivered in this iteration |
| M2 real dev vertical slice | 3–5 weeks | PG repository, real transaction concurrency, outbox/cancel/renew/expiry, AD/Okta, signed admission, certified full+fraction lifecycle on one real cluster |
| M3 QA multi-cluster & recovery | 3–5 weeks | Existing on-prem/GKE/AKS onboarding/drain, data-aware routing, partition/duplicate/cancellation races and stateful restores |
| M4 governed pilot | 3–4 weeks | 20–50 users, representative apps, email, DCGM attribution, showback, profiling and daily reconciliation |
| M5 production opt-in | 4–6 weeks | Load/chaos/security/DR acceptance, support runbooks, application SLO and isolation review, explicit production readiness approval |
| M6 optimization & chargeback | Ongoing, separate acceptance | Measured improvements, finance-approved chargeback and certified stronger reservations |

These ranges match the delivery-manager plan. Parallel agent work speeds source delivery; identity,
hardware, procurement, security and restore evidence cannot be replaced by agents.
Do not sum estimates as a fixed promised launch date. Critical path: fractional SkyPilot
certification and durable fence/release protocol → security controls → real workloads → DR.

Engineering role agents are session-scoped and do not imply staffed 24×7 support.
The project-management agent maintains Git backlog/evidence and can be resumed from
that state in another session. No silent recurring automation or cloud deployment.

## Primary references

Shopify's published design is a useful precedent: persistent multi-cloud Kubernetes
clusters, SkyPilot as a launcher, policy-based routing, data already present in each
environment, mandatory cost ownership and a GPU reaper. Their documented scheduler
is **Kueue**, not KAI. Reuse the separation of responsibilities, not an assumption that
their quotas or utilization threshold prove GRACE's reservation/DR guarantees.
See [Shopify's engineering account](https://shopify.engineering/skypilot).

- [KAI GPU sharing and isolation warning](https://github.com/NVIDIA/KAI-Scheduler/blob/main/docs/gpu-sharing/README.md)
- [NVIDIA tenant GPU sharing discussion](https://developer.nvidia.com/blog/how-to-run-isolated-tenant-kubernetes-clusters-on-shared-gpu-infrastructure/)
- [SkyPilot OSS HA limitations](https://docs.skypilot.ai/en/latest/reference/api-server/api-server-upgrade.html)
- [SkyPilot server-side policies](https://docs.skypilot.ai/en/latest/cloud-setup/policy.html)
- [PostgreSQL concurrency and serializable retries](https://www.postgresql.org/docs/current/transaction-iso.html)
- [gRPC Python generated contracts](https://grpc.io/docs/languages/python/basics/)
- [Shopify SkyPilot architecture](https://shopify.engineering/skypilot)

No claim is made that Meta or Uber use this precise SkyPilot reservation design.
