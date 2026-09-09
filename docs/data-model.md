# GRACE data model and consistency contract

Status: executable PostgreSQL 16+ migration source and opt-in integration tests. This document does **not** claim that the PostgreSQL persistence adapter, production role grants, or real KAI mapping reconciler have been integrated or runtime-certified. The in-process reference service and this SQL model are separate deliverables until the persistence milestone joins them.

## 1. First-principles decisions

1. The database owns authorized intent, logical capacity commitments, accounting and audit history. Kubernetes/KAI/DCGM own observed execution and physical health. Neither an optimistic database row nor low GPU utilization proves a card is free.
2. Every tenant, BU, project, application, identity, cluster, node, device, reservation and allocation has an immutable internal UUID. OIDC identity is `(issuer, subject)`, not an email address or display name. Active Directory identities are federated through Okta; do not store AD passwords.
3. Dev, QA and production are separate deployment and identity boundaries, not just a label. Environment is nevertheless included in composite foreign keys to catch data defects. Production starts globally disabled and also requires request-level authorization. Moving from dev to QA is **not** ordinary location fallback.
4. Existing Kubernetes clusters are onboarded. `connector_ref` points to configured workload identity/secret references, never embedded credentials. No GPU VM provisioner is represented by this schema.
5. A GPU request is `(gpu_model, gpu_count, fraction_millis per GPU, memory_mib minimum working set per GPU, topology)`. Integer fractions avoid float comparison drift. `1000` is one logical full device, `250` is one quarter. A lease reserves the effective KAI memory entitlement `ceil(physical memory × fraction / 1000)`, not just the requested minimum; the minimum must fit the rounded-down entitlement. This is accounting and scheduling entitlement, **not** guaranteed compute throughput or hardware memory isolation. No MIG resource or slice table exists in this MVP.
6. Immediate allocation is best effort until actual backend binding is verified. Future starts may be recorded and queued, but the acquisition function rejects advance capacity holds. Guaranteed future reservations are a later adapter-gated feature, not a promise created by a database insert.
7. Once dispatch starts, GPU capacity remains committed until verified cleanup. Expiration requests cleanup; it does not prove deletion. Uncertain and unreachable allocations consume budget indefinitely until reconciled.
8. Fractional sharing is allowed only in an explicitly approved project/pool mapping with matching application/pool trust domains. KAI-only accounting does not enforce CUDA consumption. Certified HAMi runtimes can enforce CUDA memory and separately configured SM-utilization caps; these software controls are distinct from hardware fault isolation and do not establish a guaranteed share of throughput. Preserve the approved workload trust boundary and verify opt-out prevention.

## 2. Optimized ER views

The diagrams deliberately split ownership, physical inventory, lifecycle and accounting. Keys repeated in child tables are integrity boundaries, not separate entities.

### Ownership

```mermaid
erDiagram
    direction TB
    ORGANIZATION ||--o{ BUSINESS_UNIT : owns
    BUSINESS_UNIT ||--o{ PROJECT : owns
    PROJECT ||--o{ APPLICATION : owns
    APPLICATION ||--o{ RESERVATION : requests
    ORGANIZATION {
        uuid id PK
        text name
    }
    BUSINESS_UNIT {
        uuid id PK
        uuid tenant_id FK
        text cost_center
    }
    PROJECT {
        uuid id PK
        uuid business_unit_id FK
    }
    APPLICATION {
        uuid id PK
        uuid project_id FK
        text trust_domain
    }
    RESERVATION {
        uuid id PK
        uuid application_id FK
        text environment
        timestamptz ends_at
    }
```

`identities` and `project_memberships` attach verified Okta subjects to project roles. `project_pool_access` is the explicit allowlist for each project/environment/pool, including fractional-sharing approval. A user cannot select an arbitrary pool by supplying its UUID.

### Physical inventory

```mermaid
erDiagram
    direction TB
    CLUSTER ||--o{ RESOURCE_POOL : exposes
    CLUSTER ||--o{ GPU_NODE : contains
    RESOURCE_POOL ||--o{ PHYSICAL_GPU : accounts
    GPU_NODE ||--o{ PHYSICAL_GPU : hosts
    CLUSTER ||--o{ INFRASTRUCTURE_OBSERVATION : reports
    CLUSTER {
        uuid id PK
        text environment
        text onboarding_state
        text connector_ref
    }
    RESOURCE_POOL {
        uuid id PK
        text sharing_mode
        text trust_domain
        text kai_queue
    }
    GPU_NODE {
        uuid id PK
        text kubernetes_node_uid
        text fabric_domain
    }
    PHYSICAL_GPU {
        uuid id PK
        text hardware_uuid
        bigint usable_memory_mib
        timestamptz observed_at
    }
    INFRASTRUCTURE_OBSERVATION {
        uuid id PK
        text source
        timestamptz observed_at
        jsonb facts
    }
```

Nodes carry rack, zone, fabric, CPU and host-memory metadata; GPUs carry NUMA/NVLink/PCI and driver/firmware metadata. Registered Kubernetes cluster UID and full GPU hardware UUID are globally unique, preventing the same physical capacity from being onboarded separately under two tenants. A ready cluster must have its real UID; cluster/node/GPU identity fields are update-guarded. This initial model assigns inventory to one tenant; shared cross-tenant physical infrastructure would require a central-owner/access-mapping redesign, not duplicate inventory records. CPU/host-memory/NIC/storage scheduling budgets are **not yet enforced by these GPU lease functions**; the production placement adapter must also validate those resources and expose pending backend status honestly. A `same_fabric` label is a necessary filter, not proof of bandwidth, NCCL performance or a topology reservation.

### Reservation and execution

```mermaid
erDiagram
    direction TB
    RESERVATION ||--o{ ALLOCATION : attempts
    ALLOCATION ||--|{ CAPACITY_LEASE : accounts
    PHYSICAL_GPU ||--o{ CAPACITY_LEASE : budget_target
    RESERVATION ||--o{ WORKLOAD : authorizes
    WORKLOAD ||--o{ EXECUTION_ATTEMPT : retries
    ALLOCATION ||--o{ EXECUTION_ATTEMPT : permits
```

Each allocation is one all-or-nothing set of accounting leases and has a DR epoch plus monotonic fencing token. One reservation has at most one open allocation. `execution_attempts` carries the reservation ID in composite foreign keys to ensure both its workload and allocation belong to the same reservation.

`capacity_leases.accounting_gpu_id` is a packing/accounting choice, **not** a claim that a vanilla Kubernetes extended-resource request pins a GPU UUID. `workload_gpu_bindings.actual_gpu_id` is the independently observed KAI/container assignment. The adapter must enforce aggregate pod/gang budgets, collect actual assignment and quarantine mismatches before claiming assured allocation. A binding-reconciliation implementation is a required integration milestone; the SQL schema alone does not make KAI placement atomic with PostgreSQL.

### Usage and money

```mermaid
erDiagram
    direction TB
    ALLOCATION ||--o{ USAGE_FACT : meters
    USAGE_FACT ||--o{ COST_ENTRY : values
    PRICE_RATE ||--o{ COST_ENTRY : prices
    BUSINESS_UNIT ||--o{ COST_ENTRY : attributed
    COST_ENTRY ||--o{ COST_ENTRY : corrects
```

`usage_facts` has a same-reservation composite FK to allocation. Cost entries snapshot the BU/cost-center at consumption, use decimal money and currency-bound rate references, and require a stable `charge_event_key` for deduplication. Corrections append a new entry referencing the original. Chargeback export and bill-to-ledger reconciliation are separate deliverables; cost rows do not initiate financial transfers.

## 3. Table responsibility map

| Responsibility | Tables | Boundary / important rule |
|---|---|---|
| Organization and authorization | organizations, business_units, projects, applications, identities, project_memberships | Composite tenant FKs; API verifies subject and role before SQL invocation |
| Environment and DR | environment_controls, dr_fence | Production off by default; single writable authority; DR epoch fenced |
| Cluster onboarding | clusters, cluster_onboarding_checks, project_pool_access | A cluster is not eligible until ready and approved |
| Inventory | resource_pools, gpu_nodes, physical_gpus, infrastructure_observations | Stale/unknown inventory is ineligible; observed external occupancy is not double-counted as managed leases |
| Intent | reservations, data_attestations | Time-bounded demand; data attestation per eligible pool |
| Capacity | allocations, capacity_leases | Atomic gang holds; integer memory/fraction budgets; release only after evidence |
| Execution | workloads, execution_attempts, workload_gpu_bindings | Retries and actual GPU assignment distinct from accounting bins |
| Reliable delivery | idempotency_records, outbox_events, consumer_receipts | At-least-once delivery; durable duplicate suppression and command reconciliation |
| Audit and showback | ledger_events, audit_events, price_rates, usage_facts, cost_entries | Immutable events/rates/facts; corrections append; not Prometheus as billing ledger |
| Operations | reconciliation_incidents, notifications | Drift is investigated; notification delivery can retry independently |

Mutable descriptive JSON is limited to heterogeneous inventory labels, source evidence and event payloads. Authoritative tenant IDs, GPU shape, intervals, status, amounts and foreign keys remain typed columns. High-cardinality telemetry belongs outside the OLTP database; store summaries and evidence references here.

## 4. SQL mutation contract

Apply migration files in numeric order to a **new** database using an owner-only migration job, with `psql -v ON_ERROR_STOP=1`. The DR fence begins closed. Seed tenant/environment policy and verified inventory before opening it.

| Function | Result | Required behavior |
|---|---|---|
| `acquire_capacity(reservation_id, gpu_ids[], expected_dr_epoch)` | Allocation UUID | Reservation must be queued/requested; exact device count; locks all accounting GPUs in UUID order; commits every lease plus outbox/ledger event or none |
| `mark_submitting(allocation_id, expected_dr_epoch)` | Fence token | Locks reservation and allocation; rejects cancellation, expired/future reservation and stale epoch before provider call |
| `request_cancellation(reservation_id, expected_dr_epoch)` | Current state | Never-dispatched planned holds can release; dispatched holds become cancel_requested and retain capacity |
| `record_ownership_fence(allocation_id, observation_id, expected_dr_epoch)` | Void | Records fresh controller evidence that stale owners can no longer recreate work, after stop requested |
| `release_after_cleanup(allocation_id, observation_id, expected_dr_epoch)` | Void | Requires a fresh complete snapshot after stop and fencing, matching cluster/tenant/environment/allocation/epoch/token; no remaining resources or unresolved dispatch |

The external gRPC service must validate authenticated tenant/project access, then invoke these internal functions. They are `SECURITY DEFINER`, search-path pinned, and revoked from PUBLIC; they are **not** safe direct end-user SQL APIs. Use a non-login schema owner and grant only explicitly required function execution to the trusted backend role. Do not grant it table ownership or arbitrary DML on capacity/allocation state. The migration intentionally creates no organization-specific production users/grants; platform IAM must supply those and test negative permissions before deployment. Inventory ingestion and audit ingestion use different narrowly scoped writer identities. Schema owner/superuser can still defeat ordinary constraints; that is a privileged break-glass boundary, not an anti-bypass guarantee.

Idempotency processing must be in the **same** transaction as reservation creation/transition and its outbox event: key scope is tenant + environment + identity + method + key; store a canonical request hash; the same key with different content is a conflict. Row uniqueness alone is insufficient if external dispatch precedes commit. Caller retries `40001` and `40P01` with bounded jitter and an overall deadline; do not retry invalid shape, identity or policy errors.

Workers atomically claim pending outbox rows with `FOR UPDATE SKIP LOCKED` and a bounded claim lease, then commit before network I/O. Provider timeout is `unknown`, not automatic retry into a different cluster. Reconcile deterministic command key / execution attempt / actual resources before resubmitting. A database fencing token does not stop a stale provider call unless the execution/admission boundary checks it. There is no exactly-once network-provisioning claim.

### Fractional overlap invariant

For every accounting GPU and instant covered by a new lease:

`sum(active fraction_millis) + observed unmanaged fraction <= 1000`

`sum(active memory_mib) + observed unmanaged memory <= usable_memory_mib`

The trigger locks the GPU row, groups coincident start/end events and computes peak running sums. Intervals are half-open `[start, end)`. Two leases may each intersect a third interval without intersecting each other; summing every overlapping row would incorrectly reject that valid schedule. A simple exclusion constraint is correct for exclusive devices but cannot express this fractional sum invariant.

For any dispatched allocation, its accounting interval extends to **infinity until verified release**, even if contractual `ends_at` has passed. An expired notebook that cannot be reached is still consuming capacity. For never-dispatched planned holds, the calendar interval remains contractual and dispatch is prohibited after expiration.

Use READ COMMITTED with the GPU-row serialization point, or SERIALIZABLE with full-transaction retries. REPEATABLE READ is explicitly rejected: a transaction may retain an old occupancy snapshot even after waiting for another holder's row lock. Multi-GPU acquisition uses sorted locks and one transaction. These are executable constraints, but concurrency correctness still needs real PostgreSQL tests; textual inspection is not that proof.

### SQL errors and API mapping

| SQLSTATE | Meaning | Suggested gRPC status |
|---|---|---|
| GRC01 | DR closed / stale epoch | UNAVAILABLE or FAILED_PRECONDITION; never dispatch |
| GRC02 / GRC11 | Forbidden history deletion/mutation | FAILED_PRECONDITION |
| GRC03 | Lifecycle or immutable identity violation | FAILED_PRECONDITION |
| GRC04 | Missing cleanup evidence | FAILED_PRECONDITION |
| GRC05 | Stale/unhealthy/clock-invalid inventory | UNAVAILABLE |
| GRC06 | Invalid GPU shape, topology or maintenance | INVALID_ARGUMENT / FAILED_PRECONDITION |
| GRC07 | Logical fraction or memory exhausted | RESOURCE_EXHAUSTED |
| GRC08 | Future assurance not supported by adapter | FAILED_PRECONDITION |
| GRC09 | Disabled environment / placement authorization | PERMISSION_DENIED |
| GRC10 | Data not attested | FAILED_PRECONDITION |
| GRC12 | Ambiguous overlapping rate | ALREADY_EXISTS |
| GRC13 | Unsafe isolation level | INTERNAL; fix deployment configuration |
| 23505 | Duplicate key | ALREADY_EXISTS or return stored idempotent result |
| 40001 / 40P01 | Serialization / deadlock retry | Retry whole transaction within deadline, then ABORTED |

Do not expose raw SQL, JWTs, credentials, dataset access tokens or provider stack traces in user errors.

## 5. Reconciliation and DR

- Inventory has `observed_at` and `received_at` separately. A complete Kubernetes snapshot is not inferred from a partial watch event; watch disconnects and resource-version gaps trigger relist and ineligibility until repaired.
- The discovery pipeline must classify unrecognized GPU pods and shadow accounting. Unknown occupancy sets `allocatable=false`; never replace unknown with zero. KAI-assigned GPU mappings are joined by actual pod/container UID and GPU UUID.
- Do not reduce an occupied device's health/usable memory and erase its leases. Mark the pool/GPU ineligible, retain commitments and raise an incident; reconciliation restores reality or obtains user-approved remediation.
- SQL cleanup evidence is an immutable controller attestation, not proof produced by a database query. The trusted reconciler must prove all owned pods/controllers/compute environments are gone, prevent their recreation, and persist remaining UID list `[]`. Metrics silence, a successful delete request or a missing SkyPilot dashboard row is insufficient.
- The release function requires state `releasing`, a recorded stop request and independently recorded ownership fence. Cleanup evidence must be fresh, complete, strictly after both timestamps, and affirm `ownership_fenced`, `scheduler_share_released`, `dispatch_reconciled`, the exact epoch/fencing token and an empty remaining UID list. An early empty snapshot is rejected even if it was captured after allocation creation.
- PostgreSQL uses synchronous local HA and tested encrypted PITR backups; the standby/backup policy, separate failure-domain copy, keys and restore procedure are deployment tasks. Other stateful services need their own DR plans; copying the reservation DB alone is not a complete restore.
- Regional DR sequence: fence old writers/executors at infrastructure/admission boundary; recover DB and required SkyPilot state; keep mutations disabled; increment `dr_fence.epoch`; reconcile all resources and restore missing commitments; adopt validated surviving allocations into the new epoch using an audited recovery procedure; only then reopen new reservations.
- A nonzero async replication RPO may lose a committed reservation while its pod survives. Reconcile **all** managed resource UIDs before reopening—not just objects referenced by the restored DB. Regional zero-loss booking requires an appropriate synchronous/quorum data architecture and fencing tradeoff, not an optimistic RPO statement.
- Epoch allocation adoption, actual-binding repair, reservation renewal and expiration workflow integration remain explicit implementation milestones. Do not improvise ad hoc SQL in production.

## 6. Performance and retention

Initial scale is hundreds of users/applications: PostgreSQL is sufficient; Redis is not an authoritative lock. Device-level locks serialize conflicting assignments while unrelated GPUs progress independently. Sorted gang locks, bounded lock timeouts, connection pooling and cancellation/deadline propagation matter more than a language microbenchmark.

Indexes target eligible GPUs, active interval lookups, expiry scans, application history, pending outbox claims and BU/time cost queries. Partial indexes omit released history. Covering columns avoid needless heap reads on common lookups. Do not partition small transactional tables prematurely; measure query plans and vacuum behavior first. Add time partitions to append-only observation/usage/event tables only when retention and demonstrated volume justify them.

Keep a documented idempotency retention period at least as long as the maximum accepted replay window; do not delete keys while a reservation or provider operation is uncertain. Historical deletion is a privileged retention/migration process with archival controls, not an application endpoint. Export audit history to an independently protected archive; PostgreSQL triggers alone are not tamper-proof storage.

Meter entitlement as integer fraction-milliseconds: `duration_ms * fraction_millis`, summed per GPU. One full GPU-hour is `3_600_000_000` fraction-milliseconds. Showback amount = fraction-milliseconds / 3,600,000,000 × decimal price per GPU-hour. Utilization samples describe efficiency separately; missing samples remain missing, not assumed idle/zero. Physical wall-time and fraction-based showback are separate reports; a logical 25% allocation does not prove 25% of physical work or energy.

## 7. Verification and remaining gates

`tests/test_schema_contract.py` checks structural source contracts without a database. `tests/test_schema_postgres.py` runs opt-in executable tests against a deliberately empty disposable PostgreSQL database. It installs migrations and tests overlap, effective memory, stale inventory, cross-environment denial, atomic multi-device rollback, cancellation, cleanup evidence ordering and concurrent competition. These tests must pass in CI on PostgreSQL 16 before persistence certification. They do not substitute for real KAI admission/fractional-memory enforcement, actual mapping, duplicate SkyPilot launch recovery or regional restore drills.

```sh
# Point only at a newly created disposable test DB; no existing grace schema.
# The explicit opt-in acknowledges that this suite installs migrations and test rows.
export GRACE_TEST_POSTGRES_DISPOSABLE=1
export GRACE_TEST_POSTGRES_DSN='postgresql://test-user:placeholder@127.0.0.1:5432/grace_disposable_test'
python -m unittest discover -s tests -p 'test_schema*.py' -v
```

Without that opt-in and `psycopg`, runtime tests report SKIPPED, not passed. The suite refuses to alter an existing `grace` schema and does not drop databases or schemas. Cleanup of the explicit disposable DB belongs to the CI fixture owner.

Remaining schema/integration work includes tenant-scoped query authorization tests, renewal with atomic gang revalidation, metering ingestion interval/rate/pool integrity, CPU/host-memory accounting, bounded-rate outbox worker implementation, idempotent reservation-create function, full RLS/role-grant threat review if direct reporting access is enabled, and backend binding/cleanup attestation certification. Production and chargeback remain gated.
