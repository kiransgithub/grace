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
  "data_locations": ["onprem", "gcp-us-central1"],
  "production_opt_in": false
}
```

`location` omitted means placement freedom **within** authorized environment/data
locations; it never permits dev→prod. `location` present means strict. Fractional values
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

Creation response is RESERVED, not RUNNING. REST cancel 202 / gRPC RELEASING is accepted
intent, not evidence of freed capacity. Demo synthetic observer eventually confirms
absence; a live implementation must also fence delayed dispatch, stop SkyPilot recovery
and verify the real allocation/pod identities.

## Contract versioning and operations

- The next persistence adapter must translate external `prod` to the SQL model's
  `production`, and API `RESERVED` to SQL `held`. Internal reservation/allocation/
  execution states are distinct; do not cast SQL strings into protobuf enums.
  The durable queue needs new enum values added compatibly, and cancelled versus
  expired terminal views need their recorded lifecycle reason. None of this mapping
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
