# GRACE v1 REST and gRPC contract

Authoritative protobuf: `proto/grace/v1/grace.proto`. Generated Python clients and
servicers are checked in under `src/grace/v1/`; do not hand-edit them. `0.1` implements
Create/Get/List/Cancel with one shared non-durable domain service. Other methods in
the contract are future work and return UNIMPLEMENTED (HTTP 501); no fake live dispatch.

| gRPC method | REST route | v0.1 |
|---|---|---|
| CreateReservation | POST /v1/reservations | Real simulation path |
| GetReservation | GET /v1/reservations/{id} | Implemented |
| ListReservations | GET /v1/reservations?page_size=&page_token= | Implemented, signed cursor, max 200 |
| CancelReservation | POST /v1/reservations/{id}:cancel | Implemented, observed cleanup before terminal release |
| RenewReservation | POST /v1/reservations/{id}:renew | Contract only |
| ActivateReservation | POST /v1/reservations/{id}:activate | Contract only |
| WatchReservation | GET /v1/reservations/{id}/events (SSE target) | Contract only |
| ClusterService | /v1/clusters target | Contract only; library lifecycle provided |
| MeteringService.GetUsage | GET /v1/usage | Contract only |
| PolicyService.GetPolicy/SetPolicy | GET/PUT /v1/policies/{id} | Admin target contract only; 501/UNIMPLEMENTED |

## Authentication

Enterprise target: AD-backed Okta OIDC, JWT audience/issuer/signature/expiry and app
authorization; internal workload identities use mTLS. Request tenant/application
identifiers must be authorized from verified claims and catalog membership. Never
accept a role in a payload. Production requires all policy gates. Current development
runtime intentionally has one fixed demo bearer identity, no production, no Okta, no
live cluster credentials. HTTP/gRPC default bind is loopback; pod mode uses private
network and no ingress. TLS gateway or service mesh is mandatory outside the local
simulation; the supplied development gRPC listener itself is plaintext.

## Create JSON example

```json
{
  "tenant_id": "demo",
  "application_id": "demo-app",
  "environment": "dev",
  "gpu_type": "A100-40GB",
  "gpu_millicards": 250,
  "gpu_memory_mib": 5120,
  "device_count": 1,
  "duration_seconds": 3600,
  "location": "onprem",
  "location_policy": "preferred",
  "wait_for_capacity": true,
  "queue_timeout_seconds": 3600,
  "data_locations": ["onprem", "gcp-us-central1"],
  "production_opt_in": false
}
```

`location_policy="strict"` keeps the request at its selected location;
`"preferred"` tries that location before approved/data-ready alternatives;
`"any"` requires an omitted location. `preferred` requires a location. The legacy
default remains strict when a location is provided, otherwise effective any.
Responses expose `effective_location_policy` and `selected_location` (null while queued).
MVP locations map to existing on-prem Kubernetes or regional GKE, never dev→prod.
In protobuf, use `placement.location` and `placement.location_policy`;
the existing `required_location` field remains accepted for older clients. Conflicting
old/new location values are rejected. Fractional values
are integers: 250 means 0.25 of each GPU's memory scheduling budget, not guaranteed 25%
compute. `gpu_memory_mib` is minimum required working set, must fit the selected share.
Multi-device requests need distinct compatible physical devices in one cluster.

REST: Authorization: Bearer <demo-secret>; Idempotency-Key: unique request key.
gRPC: lowercase `authorization` metadata and explicit `idempotency_key` message field.
Exactly one authorization value is required; duplicate credentials are rejected.
REST replies carry `X-Correlation-ID`; gRPC replies carry an `x-correlation-id` trailer.
The same key, same canonical spec, same caller returns the existing reservation.
Different spec returns ALREADY_EXISTS/409. Create and cancel namespaces are separate.
Cancel needs current etag (`If-Match: "1"` on REST); changed etag is ABORTED/409. Exact
cancel replay returns the original cancellation response, even if resource later changes.

Creation returns HTTP 201 and either QUEUED (no capacity hold) or RESERVED (allocated,
no workload dispatch). `wait_for_capacity` defaults true; false preserves HTTP 429 /
RESOURCE_EXHAUSTED on shortage. Invalid credentials/shape/policy still fail immediately.
`queue_timeout_seconds` defaults to 3600; while queued, `expires_at` and `admitted_at`
are absent/null and `queue_expires_at` is the waiting deadline. On admission,
`expires_at = admitted_at + duration_seconds`; waiting consumes no granted duration.
The prototype observer rechecks current authorization, policy and inventory on each
placement pass. Identity dependency failure retains the queue; confirmed revocation
cancels it. Equal-priority FIFO uses insertion order, with non-fitting heads allowing
backfill. This does not promise a start time or fairness across separate tenants.

Every response includes read-only `effective_policy`: `policy_id`, `version`,
`priority`, `preemption_enabled`, `preemption_exempt`, `idle_reclamation_exempt`.
Users cannot submit these fields. `project_id`, if supplied, must be authorized;
business unit comes from the trusted identity context. Internal admin rules resolve
project > BU > tenant/environment > default. Queued policy refreshes at reconciliation;
active grants retain their immutable snapshot. The prototype default is priority 0
with preemption disabled. Admin policy transport is deliberately not implemented;
the domain's trusted `configure_policy` method is a testable integration boundary.

Queued cancellation is immediately terminal because it has never held capacity.
For allocated reservations, REST cancel 202 / gRPC RELEASING is accepted
intent, not evidence of freed capacity. Demo synthetic observer eventually confirms
absence; a live implementation must also fence delayed dispatch, stop SkyPilot recovery
and verify the real allocation/pod identities.

## Contract versioning and operations

- The next persistence adapter must translate external `prod` to the SQL model's
  `production`, and API `RESERVED` to SQL `held`. Internal reservation/allocation/
  execution states are distinct; do not cast SQL strings into protobuf enums.
  QUEUED was added as enum number 9; cancelled versus expired terminal views need
  their recorded lifecycle reason. The durable schema's interval and placement
  constraints still require the planned migration in [persistence contract](mvp-persistence-contract.md).
  None of this mapping
  is implicitly supplied by enabling the optional PostgreSQL chart.
- Stable field numbers; reserve deleted numbers/names; add optional fields compatibly.
- No default prod enum; unspecified environment invalid. Unspecified guarantee defaults
  best-effort. Proto shapes use 64-bit memory and timestamps with UTC semantics.
- Canonical error table in execution-plan.md; field details and retryability target
  use google.rpc.Status/BadRequest/RetryInfo when durable server is added. Current server
  returns stable domain code plus detail; it does not claim full rich-error implementation.
- Explicit client deadlines; server body max 64KiB, max 200 concurrent RPCs. Async
  activation avoids long HTTP requests; operation completion means verified outcome.
- Watch target: durable per-reservation event cursor, at-least-once replay, tenant
  authorization on reconnect, bounded buffers/retention and OUT_OF_RANGE on expired
  cursors. Current Watch returns UNIMPLEMENTED; no in-memory stream masquerading as durable.
- Mutation idempotency stored transactionally in PostgreSQL before external side effects
  in durable release. Keys and canonical original results retained by documented SLA.
- Generated clients may be Java, Python, Go or Rust; transport contract is language-neutral.

## Regenerate and validate

```bash
python -m grpc_tools.protoc -I proto --python_out=src --grpc_python_out=src proto/grace/v1/grace.proto
PYTHONPATH=src python -m unittest discover -s tests -p 'test_transport.py' -v
```

Tests actually open loopback HTTP and gRPC listeners, create/replay/cancel reservations,
compare shared state, enforce limits/auth and validate error mapping. This is not proof
of Okta, distributed PostgreSQL, GPU execution, TLS ingress or HA behavior.
