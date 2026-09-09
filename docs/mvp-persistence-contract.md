# Clarified MVP persistence contract

This is the implementation specification for a future `003_mvp_queue_policy.sql` and its matching PostgreSQL repository adapter. That migration does **not** exist yet. Migrations `001`/`002` remain unchanged and retain their previously verified 13 PostgreSQL cases. The Python queue and API demonstrate the new behavior in memory; they do not persist it through the old SQL functions.

The MVP uses existing on-premises Kubernetes and GKE GPU clusters across approved regions, with KAI/HAMi fractional GPU sharing and no MIG. All admission is best effort. Priority is zero until an administrator configures a project, BU or tenant/environment policy. Showback remains separate from future chargeback exports.

## Contract and exact mapping gaps

| API behavior | Existing SQL `001`/`002` | Migration `003` and adapter requirement |
|---|---|---|
| `location_policy: strict | preferred | any` | Only `strict | any_approved`; strict requires one `strict_pool_id` | Canonical new values; stable logical location catalog; multiple eligible pools per location; preserve old strict pool pins without broadening them |
| `location` and `selected_location` | No logical-location FK on reservation; selected pool appears on allocation | Requested location FK and selected pool FK; resolve selected location through immutable cluster/location association |
| `project_id` | Derived through application; caller supplies only application ID | Resolve the application/project/BU chain from trusted records; supplied project must match it and current Okta-backed project membership |
| `wait_for_capacity`, default `true` | No field or atomic durable create/queue function | Persist accepted value and use one transaction for idempotency, intent, queue order, snapshot and events |
| `queue_timeout_seconds`, default `3600` | No separate waiting timeout | Persist integer timeout and database-clock queue deadline; range `1..604800`; queue duration does not consume the admitted duration |
| Queued `expires_at = null`, no allocation | `starts_at`/`ends_at` always non-null; acquisition tests both | Nullable admitted interval; prohibit leases until admission; replace all affected functions and lifecycle checks |
| `admitted_at`; expiry starts from admission | `starts_at` means requested calendar start | Rename/version interval semantics explicitly; a fresh admitted interval is created atomically with the first hold |
| User-visible effective priority/protections | Opaque `policy_version`; `idle_reclaim_enabled`; no policy registry | Typed immutable policy versions and snapshots, current-policy resolution during queue admission, protected admin mutations |
| Queue cancellation/timeout | `request_cancellation` can release a row without allocation, but there is no queue timeout outcome | Persist terminal reason; API distinguishes cancellation, queue timeout and release; no capacity-release evidence required for a never-admitted request |
| Stateless replicas behind GTM | Python dictionary and mutex implement runtime state | PostgreSQL repository, shared authorization state, durable idempotency/outbox and a single writable DR authority are mandatory |

API `prod` maps explicitly to SQL `production`; no location fallback changes environment. The MVP exposes dev and QA; enabling production remains a separate environment/request authorization gate.

## Migration 003 changes

### Location catalog

Create `locations(id UUID PK, tenant_id UUID, environment TEXT, code TEXT, provider TEXT, region TEXT, enabled BOOLEAN)` with unique `(tenant_id, environment, id)` and `(tenant_id, environment, code)`. Codes are stable identifiers such as `onprem`, `gcp-us-central1` and `gcp-us-east1`, not arbitrary strings treated as trusted routing instructions. New MVP locations permit `provider IN ('onprem','gcp')`; future Azure enablement is an explicit versioned onboarding change.

Add `clusters.location_id` and a composite tenant/environment FK. Each execution cluster belongs to one logical location; multiple clusters and resource pools may share it. Keep existing immutable cluster UID and physical GPU UUID uniqueness. Guard location identity and cluster reassignment while commitments exist; a changed display name must never change a reservation's routing semantics.

Add `reservations.requested_location_id` and `selected_pool_id` with composite tenant/environment FKs. New-contract requests obey:

- `strict` and `preferred` require a requested location. `any` requires it to be null.
- `strict` accepts only an authorized, data-ready pool in that location. `preferred` first evaluates fitting pools in that location, then other authorized, data-ready locations. `any` evaluates all approved candidates without a requested-location bias.
- One gang fits one execution cluster/pool. A logical region containing several clusters is not permission to split a gang across them.
- `selected_pool_id` is null before admission. At admission it must equal the allocation pool; `selected_location` is its catalog code. Original requested location is retained when fallback occurs.
- The legacy `strict_pool_id` is retained for old-contract history. If old rows are ever replayed, that exact pin remains an additional restriction; converting it only into a region/location restriction would silently widen authorization.

The existing `project_pool_access` and per-pool `data_attestations` remain authoritative. Request-level location attestation must be resolved to an explicitly recorded eligible pool set at submission; newly onboarded pools require fresh attestation/approval before they can run an already queued request. If two pools in one region have different data reachability, a bare region name cannot establish both. Recheck current access, trust, data classification and attestation for the actual pool in the placement transaction and before dispatch.

### Scheduling policy and snapshots

Create these tables with composite tenant/environment ownership throughout:

| Table | Columns and constraints |
|---|---|
| `scheduling_policies` | Stable `id`, `tenant_id`, `environment`, `scope_kind IN ('environment','business_unit','project')`, nullable `business_unit_id`, nullable `project_id`, positive `current_version`; CHECK enforces the exact scope shape; partial unique indexes enforce one head per scope |
| `scheduling_policy_versions` | PK `(policy_id, version)`; typed `priority INTEGER CHECK (priority BETWEEN 0 AND 1000000)`, `preemption_enabled`, `preemption_exempt`, `idle_reclamation_exempt`; creator identity, timestamp, reason; rows immutable |
| `reservation_policy_snapshots` | UUID PK; reservation FK; source policy ID/version FK; typed copies of effective values; `phase IN ('submission','queue_resolution','admission')`; database timestamp; rows immutable; exactly one submission and at most one admission snapshot per reservation; append queue resolution only when effective policy changes |
| `scheduling_domains` | PK `(tenant_id, environment)`; monotonic `next_enqueue_sequence`; policy-generation counter; this row serializes queue admission and policy publication for that domain |

For project scope, require a composite FK that proves the project belongs to the stated BU, not just independent valid project and BU IDs. Add the corresponding unique key `(tenant_id, business_unit_id, id)` on `projects`. The environment default has neither BU nor project; a BU rule has BU only; a project rule has both.

Resolution is **project > BU > tenant/environment default**, replacing the whole rule rather than merging individual Boolean fields. Seed an actual versioned default per tenant/environment with priority `0` and all three flags `false`. Request bodies cannot set any of these values. Only a backend with independently verified administrator authorization can publish a version; `(policy_id, expected_version)` is a compare-and-swap check. Publish the immutable version, move the head, advance the scheduling-domain policy generation and append the audit event in one transaction.

A submission snapshot records what the user initially saw. Each successful queue authorization pass resolves current policy for ordering and user visibility, even when capacity is unavailable. When policy changes, append an immutable queue-resolution snapshot, update the reservation's `current_policy_snapshot_id`, and advance its version in the same transaction. Repeated passes with unchanged policy must not create duplicate snapshots or change the etag. Admission records its own immutable snapshot; running reservations retain that snapshot. An identity dependency outage leaves the previous snapshot with an explicit authorization-unavailable queue reason. Future retroactive changes to running grants need an audited operation and never edit history.

`preemption_enabled=false` disables policy-driven preemption; `preemption_exempt=true` independently protects the victim even when preemption is enabled. `idle_reclamation_exempt` controls idle cleanup separately. A higher number alone does not invent an exemption: administrators publish the protection flags together with their high-priority rule, and users can inspect them. Neither protection cancels the reservation expiry, user cancellation or necessary infrastructure shutdown. Backend KAI/PriorityClass enforcement must be certified separately; SQL policy rows alone do not prevent Kubernetes preemption.

### Queue timing and interval conversion

Add a `contract_version SMALLINT` discriminator so historical interval meaning is never silently rewritten. Version `1` retains original rows; new API writes use version `2`. Rename `reservations.starts_at` to `admitted_at` and `ends_at` to `expires_at` only in the controlled migration described below; document that legacy version-1 values remain historical requested intervals. Add:

| Field | New-contract meaning |
|---|---|
| `requested_duration_seconds INTEGER` | Accepted grant duration, positive and bounded by the same API/admin limit; not inferred from the queue deadline |
| `wait_for_capacity BOOLEAN NOT NULL DEFAULT true` | Whether lack of immediate fair admission produces a queued reservation |
| `queue_timeout_seconds INTEGER NOT NULL DEFAULT 3600` | Accepted wait budget; `1..604800` |
| `queue_expires_at TIMESTAMPTZ` | Deadline computed once at submission using database time; retained for audit, emitted by the API only while queued |
| `enqueue_sequence BIGINT` | Domain-monotonic accepted request order, unique with tenant/environment; no reliance on timestamp ties or process insertion order |
| `admitted_at`, `expires_at` | Both null until admission; then `expires_at = admitted_at + requested_duration_seconds`; both remain on terminal admitted history |
| `selected_pool_id UUID` | Null until admission; FK to actual accounting pool |
| `submission_policy_snapshot_id`, `current_policy_snapshot_id`, `admission_policy_snapshot_id` | Same-reservation, tenant/environment FKs; current reference tracks latest resolved immutable policy; admission reference null until placement |
| `terminal_reason`, `queue_reason` | Bounded codes, not raw provider exceptions; distinguish `queue_timeout`, `user_cancelled`, `authorization_revoked`, `completed`, `expired` |

Replace the old unconditional non-null timestamps, `ends_at > starts_at` and strict-pool CHECK with named, contract-version-aware constraints. Version-2 waiting rows require absent admission interval, absent selected pool and absent admission snapshot; admitted states require all four. Require `(admitted_at IS NULL) = (expires_at IS NULL)` and a finite, increasing admitted interval. A terminal never-admitted row keeps null admission timestamps. Use a deferred cross-table constraint trigger to forbid any allocation/lease for an unadmitted version-2 row and to enforce same-reservation snapshot/pool consistency at commit.

Keep `capacity_leases.starts_at`/`ends_at` as non-null accounting intervals: they are created only at admission and match the new reservation interval. Retain sorted GPU locks, integer sums, effective memory checks, all-or-nothing gang inserts, and the `one_open_allocation` unique index. Once a dispatch starts, its accounting commitment still extends to infinity until verified release; the contractual expiry does not make a device free.

Replace `reservation_expiry` with a partial index on `(expires_at, id)` for active admitted states. Add queue deadline `(tenant_id, environment, queue_expires_at, id) WHERE state='queued'`, queue candidate `(tenant_id, environment, enqueue_sequence) WHERE state='queued'`, and policy lookup unique indexes per scope. At this scale, resolve current priority by joining current policy heads while holding the scheduling-domain lock and sort `(priority DESC, enqueue_sequence ASC)`. Do not use a stale denormalized queue-priority index after an administrator changes policy. Benchmark before introducing a maintained priority projection or partitioning domains further.

The grant clock starts at capacity admission, not queue submission or first CUDA activity. Provisioning consumes part of the admitted duration; expose this explicitly. A different startup allowance or runtime-only lease clock would be a separately versioned contract. Future `start_at` and capacity guarantees remain rejected in the new MVP API even though the historical schema could store such intent.

## Mutation functions and transaction boundaries

Every trusted mutation validates the DR epoch. Establish one lock order: DR fence, scheduling domain, relevant policy heads, reservation rows in UUID order, allocation rows, GPU rows in UUID order. Idempotency records are claimed before executing their associated business mutation, within that same transaction; no provider call occurs before commit. Functions handling the same objects must use the same order, including cancellation, policy publication and queue admission.

| Function to add or replace | Required behavior |
|---|---|
| `create_reservation_v2(...)` | Validate current authenticated identity, application/project/BU/environment, shape, location and data attestation; resolve scoped idempotency; serialize accepted queue order; insert immutable submission snapshot, reservation, queue intent and events; attempt fair immediate admission in this transaction |
| `publish_scheduling_policy(..., expected_version, ...)` | Administrator authorization, scope ownership, typed validation, monotonic version CAS, audit and policy-generation advance; no caller-selected priority on reservation create |
| `admit_next_queued(domain, expected_dr_epoch)` | Fresh policy ordering and current authorization, queue deadline and data recheck; refresh visible policy snapshots even for shortage; bounded candidate/backfill evaluation; commit admission timestamp/expiry/snapshot, selected pool, every lease and outbox/ledger event atomically |
| `acquire_capacity` replacement/internal helper | Require the version-2 admission path to hold the domain/reservation locks and prove fairness; remove old non-null calendar assumptions; direct calls cannot bypass queue order |
| `mark_submitting` replacement | Use admitted interval, snapshot/pool match and current authorization/data/capability readiness; fence before provider side effects; keep existing occupancy revalidation |
| `protect_capacity_lease` replacement | Compare to explicit admitted interval with null-safe checks; retain verified-release, integer entitlement, occupancy and immutable-identity invariants |
| `expire_queued_reservations(...)` | Under the same domain/reservation locks, terminalize due unadmitted rows with reason `queue_timeout` and events; never delete history or fabricate capacity-release evidence |
| `request_cancellation` replacement | A waiting row terminalizes without backend cleanup; an admitted planned hold can release under existing no-dispatch proof; dispatched/unknown work retains all commitments pending verified cleanup |
| `request_policy_release(...)` | Evaluate the admitted protection snapshot for idle/preemption reasons; retain contractual expiry and user-cancel paths; release still needs the existing cleanup evidence |

`record_ownership_fence` and `release_after_cleanup` retain their evidence predicates, but their lock order and terminal-reason mapping must be audited against the new functions. Do not grant a backend access to the legacy acquisition path as an escape hatch from queue fairness. All protected functions remain search-path-pinned, least-privilege `SECURITY DEFINER`; PUBLIC execute stays revoked.

Queue admission performs no network I/O while locks are held. Current external identity authorization is resolved into a trusted, freshness-bounded local authorization projection before entering the transaction; inside it, recheck enabled identity, project membership, environment, approved pool/data and projection version/expiry. An Okta synchronization outage is `AUTHORIZATION_UNAVAILABLE`, not permission and not proof of revocation. Confirmed revocation terminalizes the queued request; uncertainty keeps it queued until a later safe retry or timeout. Never substitute the newly arriving caller's claims when placing another user's waiting request.

Scoped idempotency stays `(tenant, environment, identity, method, key)` plus canonical request hash. Include normalized location policy, duration, wait budget and project in that hash. Defaulted and explicitly default-valued requests canonicalize identically. Same key/different content is a conflict. Store the create outcome atomically; replaying a queued request returns that same reservation, with current resource state. With `wait_for_capacity=false`, no fair placement yields a stable capacity-unavailable result and no reservation/lease; do not roll back its idempotency result and later turn the same retry into a different acceptance.

### Fairness and bounded backfill

Default priority zero produces oldest-accepted-first selection among requests that can fit. Admin-defined priority changes the ordering before FIFO ties. A newcomer cannot bypass an older eligible request through a fast-path API call. The domain lock is the fairness serialization point; per-GPU locks still enforce capacity. `SKIP LOCKED` is suitable for at-least-once outbox delivery, but by itself is not a proof of reservation FIFO ordering.

Start with a bounded scan of 100 queued rows and at most 8 admissions per transaction. Backfill may pass an older request only after that transaction verifies the older request cannot fit its current authorized, data-ready candidate pools, or authorization evidence is temporarily unavailable. Record why it was skipped. If the bounded scan cannot establish the fairness condition, commit no later admission and retry after a short jittered delay. A large head request can wait or time out; this MVP promises neither a future start nor starvation freedom under continuing higher-priority arrivals. Expose wait age, reason and timeout, without presenting a queue position as a start-time guarantee.

Only conflict/deadlock/serialization faults receive bounded whole-transaction retries. Shape/policy/auth failures fail immediately; stale inventory keeps capacity unavailable. A provider timeout after committed dispatch becomes unknown and blocks spillover until the original command/resources are reconciled. Outbox workers commit claims before network calls, use deterministic command keys and recheck the epoch at the execution boundary.

## Stateless GTM and DR gate

GTM routes clients to ready stateless GRACE API pods. Correctness depends on a single writable PostgreSQL authority shared by those pods, rather than memory locks or session affinity. A site that cannot reach that writer or validate its epoch must fail mutation readiness. Connection recovery and stale connection pools must be tested during promotion; DNS or GTM traffic changes do not revoke an old executor's rights.

Back up and recover the **whole GRACE schema**: policy heads/versions/snapshots, queue sequence/deadlines, identities and mappings, idempotency, outbox receipts, inventory, allocations/leases, usage, costs and audit. PostgreSQL-local HA, replication, encrypted PITR copies, encryption keys and restore access need tested operations. Restore SkyPilot API/controller state and execution identity or demonstrate deterministic reconstruction before dispatch resumes. Persisted telemetry, profile artifacts, notification delivery and billing exports each need an explicit recovery/replay policy; GRACE API statelessness does not make these stores stateless.

On regional recovery: fence the former writer and execution/admission boundary, recover required state, keep mutation readiness closed, advance the DR epoch, inventory all managed Kubernetes objects (including those absent from a rolled-back DB), restore/quarantine missing commitments and resolve delayed commands, then reopen queue admission. Expired deadlines do not release live workloads. A nonzero replication RPO can lose a committed queue/booking row while infrastructure survives; the reconciliation step is mandatory. Numeric RPO/RTO and measured restore evidence remain deployment acceptance inputs, not properties inferred from GTM.

## Upgrade sequence

1. Keep `001`/`002` immutable. Build `003` and its full regression suite against both a fresh schema and representative restored version-1 data.
2. Audit version-1 rows and emit a migration report: original interval, strict pool, data attestations, state and policy provenance. Never fabricate new admin policy evidence for old grants. Backfill nullable/new-contract-only columns without changing historical intent.
3. For initial cutover, close the DR mutation gate and require **no open allocations or nonterminal reservations**. Complete verified cleanup; explicitly cancel/resubmit waiting legacy demand if needed. Do not simply delete it or reset its timeout. This controlled MVP cutover avoids mixed live interval contracts; later zero-downtime upgrades require their own compatibility design.
4. Rename interval columns, retain version-1 semantics in history, install version-aware constraints and replacement functions, seed approved locations/policies and validate the new schema version. Record the migration in the owner-managed schema-version table and audit log. Update reports that referenced old column names.
5. Deploy the PostgreSQL adapter only when it requires the exact compatible schema version and policy/capability contract. A mismatch fails startup/readiness; the in-memory repository is never an automatic fallback.
6. Run cross-pod, DR and backend gates below before enabling multiple stateless replicas. Reopening GTM readiness is the last step. Rollback after version-2 writes requires a compatible forward fix or tested full restore with fencing/reconciliation, not just an old application image.

## Acceptance gates

These are required new tests and evidence, **not tests already completed**:

| Gate | Concrete acceptance evidence |
|---|---|
| Schema compatibility | Fresh `001→002→003` and populated legacy-history upgrade; active legacy rows block cutover; old strict pool never broadens; incompatible adapter fails readiness |
| Interval correctness | Queued timestamps/selected pool are absent; no waiting lease can be inserted even by a privileged backend function; duration starts at admission; queue timeout racing admission has one outcome |
| Durable fairness | Two API pods and two schedulers competing across regions cannot leapfrog an older fitting request; equal-priority timestamp ties obey persisted sequence; admin changes affect pending order; bounded backfill records reason |
| Capacity regression | Re-run all original 13 PostgreSQL cases against updated functions, plus concurrent fractional gangs and actual new nullable timing; no double booking and no partial gang commit |
| Retry/idempotency | Concurrent same-key creates return one reservation/event; normalized defaults replay; changed location/project/wait budget conflicts; process death after DB commit/before response does not duplicate admission |
| Authorization | Mismatched project/application/BU, revoked owner, stale auth projection, region allowed but pool denied, missing data attestation and dev-to-QA spill all fail closed at promotion/dispatch |
| Policy visibility | User read shows policy ID/version and all protection flags; project overrides BU/default; administrator CAS conflict is safe; active snapshots survive subsequent policy edits and restore |
| Lifecycle | Waiting cancel/timeout needs no provider deletion; idle/preemption exemptions are independent; expiry still stops an exempt grant; unknown cleanup retains capacity; KAI backend preemption matches admitted policy |
| Stateless failover | Kill either API pod mid-create/cancel and retry at another GTM endpoint; same result; no session affinity or pod-local policy/state dependency |
| Regional DR | Restore full schema/SkyPilot state, fence old writers/executors and reconcile a deliberately lost committed row whose pod survives; no admission until that occupancy is restored; measure RPO/RTO |

Passing queue unit tests or deploying the simulation into Kind does not satisfy these persistence gates. Real HAMi memory/SM enforcement and deep GPU profiling also require a physical CUDA-capable test environment.
