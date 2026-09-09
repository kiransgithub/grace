-- GRACE control-plane schema, PostgreSQL 16+. Apply with ON_ERROR_STOP=1.
-- This is an executable first migration, not evidence of a running PostgreSQL service.
-- IDs are internal immutable identifiers. Names and external subject IDs are not keys.
BEGIN;
CREATE SCHEMA grace;
REVOKE ALL ON SCHEMA grace FROM PUBLIC;

CREATE TABLE grace.organizations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE grace.business_units (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES grace.organizations(id),
  name text NOT NULL,
  cost_center text NOT NULL,
  UNIQUE (tenant_id, id), UNIQUE (tenant_id, name)
);
CREATE TABLE grace.projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  business_unit_id uuid NOT NULL,
  name text NOT NULL,
  FOREIGN KEY (tenant_id, business_unit_id) REFERENCES grace.business_units(tenant_id, id),
  UNIQUE (tenant_id, id), UNIQUE (tenant_id, business_unit_id, name)
);
CREATE TABLE grace.applications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  name text NOT NULL,
  trust_domain text NOT NULL DEFAULT 'unassigned',
  FOREIGN KEY (tenant_id, project_id) REFERENCES grace.projects(tenant_id, id),
  UNIQUE (tenant_id, id), UNIQUE (tenant_id, project_id, name)
);
CREATE TABLE grace.identities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES grace.organizations(id),
  issuer text NOT NULL,
  subject text NOT NULL,
  identity_kind text NOT NULL CHECK (identity_kind IN ('human', 'service')),
  enabled boolean NOT NULL DEFAULT true,
  UNIQUE (tenant_id, id), UNIQUE (tenant_id, issuer, subject)
);
CREATE TABLE grace.project_memberships (
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  identity_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('viewer', 'submitter', 'operator', 'owner')),
  PRIMARY KEY (tenant_id, project_id, identity_id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES grace.projects(tenant_id, id),
  FOREIGN KEY (tenant_id, identity_id) REFERENCES grace.identities(tenant_id, id)
);
CREATE TABLE grace.environment_controls (
  tenant_id uuid NOT NULL REFERENCES grace.organizations(id),
  environment text NOT NULL CHECK (environment IN ('dev', 'qa', 'production')),
  enabled boolean NOT NULL DEFAULT false,
  production_requests_enabled boolean NOT NULL DEFAULT false,
  PRIMARY KEY (tenant_id, environment),
  CHECK (environment = 'production' OR NOT production_requests_enabled)
);
-- One writable region / control-plane database at a time. Recovery fences the
-- old executor first, then increments epoch under this row's exclusive lock.
CREATE TABLE grace.dr_fence (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  epoch bigint NOT NULL DEFAULT 1 CHECK (epoch > 0),
  mutations_enabled boolean NOT NULL DEFAULT false,
  active_site text NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO grace.dr_fence(singleton, active_site) VALUES (true, 'unconfigured');

CREATE TABLE grace.clusters (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  name text NOT NULL,
  provider text NOT NULL CHECK (provider IN ('onprem', 'gcp', 'azure', 'other')),
  region text NOT NULL,
  -- A configured reference, never kubeconfig / credentials stored in this table.
  connector_ref text NOT NULL,
  immutable_cluster_uid text,
  trust_domain text NOT NULL,
  onboarding_state text NOT NULL DEFAULT 'discovered'
    CHECK (onboarding_state IN ('discovered', 'validating', 'ready', 'draining', 'quarantined', 'retired')),
  policy_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, environment) REFERENCES grace.environment_controls(tenant_id, environment),
  UNIQUE (tenant_id, environment, id), UNIQUE (tenant_id, environment, name),
  UNIQUE (immutable_cluster_uid),
  CHECK (onboarding_state <> 'ready' OR immutable_cluster_uid IS NOT NULL)
);
CREATE TABLE grace.cluster_onboarding_checks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  cluster_id uuid NOT NULL,
  check_name text NOT NULL,
  result text NOT NULL CHECK (result IN ('pass', 'fail', 'unknown')),
  observed_at timestamptz NOT NULL,
  evidence_ref text,
  FOREIGN KEY (tenant_id, environment, cluster_id) REFERENCES grace.clusters(tenant_id, environment, id)
);
CREATE TABLE grace.resource_pools (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  cluster_id uuid NOT NULL,
  name text NOT NULL,
  gpu_model text NOT NULL,
  memory_class_mib bigint NOT NULL CHECK (memory_class_mib > 0),
  sharing_mode text NOT NULL CHECK (sharing_mode IN ('exclusive', 'kai_fractional')),
  trust_domain text NOT NULL,
  kai_queue text NOT NULL,
  enabled boolean NOT NULL DEFAULT false,
  observations_max_age_seconds integer NOT NULL DEFAULT 60
    CHECK (observations_max_age_seconds BETWEEN 1 AND 3600),
  maintenance_starts_at timestamptz,
  maintenance_ends_at timestamptz,
  FOREIGN KEY (tenant_id, environment, cluster_id) REFERENCES grace.clusters(tenant_id, environment, id),
  UNIQUE (tenant_id, environment, id), UNIQUE (tenant_id, environment, cluster_id, id),
  UNIQUE (tenant_id, environment, cluster_id, name),
  CHECK ((maintenance_starts_at IS NULL AND maintenance_ends_at IS NULL)
    OR (maintenance_starts_at IS NOT NULL AND maintenance_ends_at > maintenance_starts_at))
);
CREATE TABLE grace.project_pool_access (
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  project_id uuid NOT NULL,
  pool_id uuid NOT NULL,
  fractional_sharing_approved boolean NOT NULL DEFAULT false,
  PRIMARY KEY (tenant_id, environment, project_id, pool_id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES grace.projects(tenant_id, id),
  FOREIGN KEY (tenant_id, environment, pool_id) REFERENCES grace.resource_pools(tenant_id, environment, id)
);
CREATE TABLE grace.gpu_nodes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  cluster_id uuid NOT NULL,
  kubernetes_node_uid text NOT NULL,
  name text NOT NULL,
  zone text NOT NULL,
  rack text,
  fabric_domain text,
  cpu_millicores bigint NOT NULL CHECK (cpu_millicores >= 0),
  host_memory_mib bigint NOT NULL CHECK (host_memory_mib >= 0),
  labels jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(labels) = 'object'),
  FOREIGN KEY (tenant_id, environment, cluster_id) REFERENCES grace.clusters(tenant_id, environment, id),
  UNIQUE (tenant_id, environment, cluster_id, id),
  UNIQUE (tenant_id, environment, cluster_id, kubernetes_node_uid)
);
CREATE TABLE grace.physical_gpus (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  cluster_id uuid NOT NULL,
  pool_id uuid NOT NULL,
  node_id uuid NOT NULL,
  hardware_uuid text NOT NULL,
  pci_address text,
  numa_node integer,
  nvlink_domain text,
  model text NOT NULL,
  memory_mib bigint NOT NULL CHECK (memory_mib > 0),
  usable_memory_mib bigint NOT NULL CHECK (usable_memory_mib > 0 AND usable_memory_mib <= memory_mib),
  health text NOT NULL DEFAULT 'unknown' CHECK (health IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
  allocatable boolean NOT NULL DEFAULT false,
  observed_at timestamptz,
  -- Unmanaged occupancy ONLY: managed leases are accounted separately. Unknown
  -- occupancy must set allocatable=false, not optimistically default to zero.
  external_fraction_millis integer NOT NULL DEFAULT 0 CHECK (external_fraction_millis BETWEEN 0 AND 1000),
  external_memory_mib bigint NOT NULL DEFAULT 0 CHECK (external_memory_mib BETWEEN 0 AND usable_memory_mib),
  driver_version text,
  firmware_version text,
  FOREIGN KEY (tenant_id, environment, cluster_id, pool_id)
    REFERENCES grace.resource_pools(tenant_id, environment, cluster_id, id),
  FOREIGN KEY (tenant_id, environment, cluster_id, node_id)
    REFERENCES grace.gpu_nodes(tenant_id, environment, cluster_id, id),
  UNIQUE (tenant_id, environment, id),
  UNIQUE (tenant_id, environment, pool_id, id),
  UNIQUE (hardware_uuid)
);
CREATE INDEX gpu_pool_healthy ON grace.physical_gpus (tenant_id, environment, pool_id, observed_at)
  INCLUDE (id, node_id, usable_memory_mib) WHERE allocatable AND health = 'healthy';
CREATE TABLE grace.infrastructure_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  cluster_id uuid NOT NULL,
  source text NOT NULL CHECK (source IN ('kubernetes', 'kai', 'dcgm', 'skypilot', 'cloud_audit')),
  observation_kind text NOT NULL,
  observed_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  cluster_resource_version text,
  is_complete_snapshot boolean NOT NULL DEFAULT false,
  facts jsonb NOT NULL CHECK (jsonb_typeof(facts) = 'object'),
  evidence_ref text,
  FOREIGN KEY (tenant_id, environment, cluster_id) REFERENCES grace.clusters(tenant_id, environment, id),
  UNIQUE (tenant_id, environment, id)
);
CREATE INDEX observations_recent ON grace.infrastructure_observations
  (tenant_id, environment, cluster_id, observed_at DESC);

CREATE TABLE grace.reservations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  application_id uuid NOT NULL,
  requested_by uuid NOT NULL,
  gpu_model text NOT NULL,
  gpu_count integer NOT NULL CHECK (gpu_count BETWEEN 1 AND 1024),
  fraction_millis integer NOT NULL CHECK (fraction_millis BETWEEN 1 AND 1000),
  -- Requested minimum working set; lease memory is the effective KAI share.
  memory_mib bigint NOT NULL CHECK (memory_mib > 0),
  topology text NOT NULL DEFAULT 'single_node' CHECK (topology IN ('single_node', 'same_fabric')),
  starts_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL,
  location_policy text NOT NULL DEFAULT 'any_approved' CHECK (location_policy IN ('any_approved', 'strict')),
  strict_pool_id uuid,
  -- Assurance is not a scheduling priority. Future booking is deferred until
  -- the backend adapter can attest a capacity hold, not just an SQL promise.
  assurance text NOT NULL DEFAULT 'best_effort'
    CHECK (assurance IN ('best_effort', 'allocated', 'backend_held')),
  workload_class text NOT NULL CHECK (workload_class IN ('interactive', 'batch', 'service')),
  state text NOT NULL DEFAULT 'requested'
    CHECK (state IN ('requested', 'queued', 'held', 'activating', 'running', 'cancel_requested',
      'expire_requested', 'releasing', 'released', 'rejected', 'failed', 'quarantined')),
  production_authorized boolean NOT NULL DEFAULT false,
  production_authorized_by uuid,
  idle_reclaim_enabled boolean NOT NULL DEFAULT true,
  checkpoint_capable boolean NOT NULL DEFAULT false,
  policy_version text NOT NULL,
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, environment) REFERENCES grace.environment_controls(tenant_id, environment),
  FOREIGN KEY (tenant_id, application_id) REFERENCES grace.applications(tenant_id, id),
  FOREIGN KEY (tenant_id, requested_by) REFERENCES grace.identities(tenant_id, id),
  FOREIGN KEY (tenant_id, production_authorized_by) REFERENCES grace.identities(tenant_id, id),
  FOREIGN KEY (tenant_id, environment, strict_pool_id) REFERENCES grace.resource_pools(tenant_id, environment, id),
  UNIQUE (tenant_id, environment, id),
  CHECK (ends_at > starts_at),
  CHECK ((location_policy = 'strict') = (strict_pool_id IS NOT NULL)),
  CHECK (environment <> 'production' OR (production_authorized AND production_authorized_by IS NOT NULL)),
  CHECK (environment = 'production' OR NOT production_authorized),
  CHECK (workload_class <> 'service' OR NOT idle_reclaim_enabled)
);
CREATE INDEX reservation_expiry ON grace.reservations (ends_at, id)
  WHERE state IN ('held', 'activating', 'running');
CREATE INDEX reservation_application_history ON grace.reservations
  (tenant_id, environment, application_id, created_at DESC) INCLUDE (state, ends_at);
CREATE TABLE grace.data_attestations (
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  reservation_id uuid NOT NULL,
  pool_id uuid NOT NULL,
  attested_by uuid NOT NULL,
  attested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  dataset_ref text NOT NULL,
  classification text NOT NULL,
  available boolean NOT NULL,
  PRIMARY KEY (tenant_id, environment, reservation_id, pool_id, dataset_ref),
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, pool_id) REFERENCES grace.resource_pools(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, attested_by) REFERENCES grace.identities(tenant_id, id)
);
CREATE TABLE grace.allocations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  reservation_id uuid NOT NULL,
  pool_id uuid NOT NULL,
  dr_epoch bigint NOT NULL CHECK (dr_epoch > 0),
  fence_token bigint GENERATED ALWAYS AS IDENTITY,
  state text NOT NULL DEFAULT 'planned' CHECK (state IN ('planned', 'submitting', 'running', 'releasing', 'released', 'quarantined')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  dispatch_started_at timestamptz,
  stop_requested_at timestamptz,
  ownership_fenced_at timestamptz,
  fencing_observation_id uuid,
  cleanup_observation_id uuid,
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, pool_id) REFERENCES grace.resource_pools(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, cleanup_observation_id) REFERENCES grace.infrastructure_observations(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, fencing_observation_id) REFERENCES grace.infrastructure_observations(tenant_id, environment, id),
  UNIQUE (tenant_id, environment, id), UNIQUE (tenant_id, environment, pool_id, id),
  UNIQUE (tenant_id, environment, reservation_id, id)
);
CREATE UNIQUE INDEX one_open_allocation ON grace.allocations (reservation_id) WHERE state <> 'released';
CREATE TABLE grace.capacity_leases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  pool_id uuid NOT NULL,
  allocation_id uuid NOT NULL,
  -- Budget/packing target ONLY. This never claims KAI will pin this hardware UUID.
  accounting_gpu_id uuid NOT NULL,
  fraction_millis integer NOT NULL CHECK (fraction_millis BETWEEN 1 AND 1000),
  memory_mib bigint NOT NULL CHECK (memory_mib > 0),
  starts_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL,
  state text NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'bound', 'releasing', 'released')),
  FOREIGN KEY (tenant_id, environment, pool_id, allocation_id) REFERENCES grace.allocations(tenant_id, environment, pool_id, id),
  FOREIGN KEY (tenant_id, environment, pool_id, accounting_gpu_id) REFERENCES grace.physical_gpus(tenant_id, environment, pool_id, id),
  UNIQUE (allocation_id, accounting_gpu_id),
  UNIQUE (tenant_id, environment, id),
  CHECK (ends_at > starts_at)
);
CREATE INDEX capacity_overlap_lookup ON grace.capacity_leases (accounting_gpu_id, starts_at, ends_at)
  INCLUDE (fraction_millis, memory_mib, id) WHERE state <> 'released';

CREATE TABLE grace.workloads (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  reservation_id uuid NOT NULL,
  artifact_digest text NOT NULL,
  template_ref text NOT NULL,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')),
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id),
  UNIQUE (tenant_id, environment, id), UNIQUE (tenant_id, environment, reservation_id, id)
);
CREATE TABLE grace.execution_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  reservation_id uuid NOT NULL,
  workload_id uuid NOT NULL,
  allocation_id uuid NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  command_key text NOT NULL,
  skypilot_realm text NOT NULL,
  skypilot_cluster_name text NOT NULL,
  skypilot_request_id text,
  skypilot_job_id text,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'submitting', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')),
  error_class text,
  error_code text,
  sanitized_error text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, environment, reservation_id, workload_id)
    REFERENCES grace.workloads(tenant_id, environment, reservation_id, id),
  FOREIGN KEY (tenant_id, environment, reservation_id, allocation_id)
    REFERENCES grace.allocations(tenant_id, environment, reservation_id, id),
  UNIQUE (tenant_id, environment, id), UNIQUE (workload_id, attempt_number),
  UNIQUE (skypilot_realm, command_key)
);
-- Actual post-scheduling device assignment, independent of accounting bins.
CREATE TABLE grace.workload_gpu_bindings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  execution_attempt_id uuid NOT NULL,
  actual_gpu_id uuid NOT NULL,
  pod_uid text NOT NULL,
  container_name text NOT NULL,
  fraction_millis integer NOT NULL CHECK (fraction_millis BETWEEN 1 AND 1000),
  memory_mib bigint NOT NULL CHECK (memory_mib > 0),
  observed_at timestamptz NOT NULL,
  observation_id uuid NOT NULL,
  FOREIGN KEY (tenant_id, environment, execution_attempt_id) REFERENCES grace.execution_attempts(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, actual_gpu_id) REFERENCES grace.physical_gpus(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, observation_id) REFERENCES grace.infrastructure_observations(tenant_id, environment, id),
  UNIQUE (execution_attempt_id, actual_gpu_id, pod_uid, container_name)
);

CREATE TABLE grace.idempotency_records (
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  identity_id uuid NOT NULL,
  method text NOT NULL,
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
  request_hash text NOT NULL,
  reservation_id uuid,
  operation_id uuid,
  response_status integer,
  response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  retain_until timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, environment, identity_id, method, idempotency_key),
  FOREIGN KEY (tenant_id, identity_id) REFERENCES grace.identities(tenant_id, id),
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id),
  CHECK (retain_until > created_at)
);
CREATE TABLE grace.outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  reservation_id uuid NOT NULL,
  event_type text NOT NULL,
  aggregate_version bigint NOT NULL CHECK (aggregate_version > 0),
  dr_epoch bigint NOT NULL CHECK (dr_epoch > 0),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  claimed_until timestamptz,
  claim_owner text,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  published_at timestamptz,
  last_error_code text,
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id),
  UNIQUE (reservation_id, aggregate_version, event_type)
);
CREATE INDEX outbox_ready ON grace.outbox_events (available_at, id) WHERE published_at IS NULL;
CREATE TABLE grace.consumer_receipts (
  consumer_name text NOT NULL,
  event_id uuid NOT NULL REFERENCES grace.outbox_events(id),
  processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (consumer_name, event_id)
);
CREATE TABLE grace.ledger_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  reservation_id uuid NOT NULL,
  aggregate_version bigint NOT NULL,
  event_type text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  dr_epoch bigint NOT NULL,
  facts jsonb NOT NULL CHECK (jsonb_typeof(facts) = 'object'),
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id),
  UNIQUE (reservation_id, aggregate_version, event_type)
);
CREATE TABLE grace.price_rates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  pool_id uuid NOT NULL,
  currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  rate_kind text NOT NULL CHECK (rate_kind IN ('showback', 'chargeback', 'provider_estimate')),
  effective_from timestamptz NOT NULL,
  effective_until timestamptz NOT NULL CHECK (effective_until > effective_from),
  price_per_gpu_hour numeric(24,12) NOT NULL CHECK (price_per_gpu_hour >= 0),
  source_ref text NOT NULL,
  FOREIGN KEY (tenant_id, environment, pool_id) REFERENCES grace.resource_pools(tenant_id, environment, id),
  UNIQUE (tenant_id, environment, id), UNIQUE (tenant_id, environment, id, currency)
);
CREATE TABLE grace.usage_facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  reservation_id uuid NOT NULL,
  allocation_id uuid NOT NULL,
  starts_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL CHECK (ends_at > starts_at),
  source text NOT NULL,
  source_record_id text NOT NULL,
  gpu_millisecond_fractions numeric(30,0) NOT NULL CHECK (gpu_millisecond_fractions >= 0),
  utilization_sample_count bigint NOT NULL DEFAULT 0 CHECK (utilization_sample_count >= 0),
  missing_sample_count bigint NOT NULL DEFAULT 0 CHECK (missing_sample_count >= 0),
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, reservation_id, allocation_id)
    REFERENCES grace.allocations(tenant_id, environment, reservation_id, id),
  UNIQUE (tenant_id, environment, id), UNIQUE (source, source_record_id)
);
CREATE TABLE grace.cost_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  usage_fact_id uuid NOT NULL,
  price_rate_id uuid NOT NULL,
  charge_event_key text NOT NULL,
  -- Snapshot ownership at consumption: later re-orgs must not rewrite history.
  business_unit_id uuid NOT NULL,
  cost_center_snapshot text NOT NULL,
  currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  amount numeric(24,8) NOT NULL,
  entry_kind text NOT NULL CHECK (entry_kind IN ('estimate', 'settlement', 'correction')),
  corrects_entry_id uuid,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, environment, usage_fact_id) REFERENCES grace.usage_facts(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, price_rate_id, currency) REFERENCES grace.price_rates(tenant_id, environment, id, currency),
  FOREIGN KEY (tenant_id, business_unit_id) REFERENCES grace.business_units(tenant_id, id),
  UNIQUE (tenant_id, environment, id), UNIQUE (tenant_id, environment, charge_event_key),
  FOREIGN KEY (tenant_id, environment, corrects_entry_id) REFERENCES grace.cost_entries(tenant_id, environment, id),
  CHECK ((entry_kind = 'correction') = (corrects_entry_id IS NOT NULL))
);
CREATE INDEX cost_bu_time ON grace.cost_entries (tenant_id, environment, business_unit_id, created_at);
CREATE TABLE grace.audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL,
  actor_id uuid,
  environment text NOT NULL,
  action text NOT NULL,
  object_type text NOT NULL,
  object_id uuid NOT NULL,
  request_id uuid NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  sanitized_facts jsonb NOT NULL,
  FOREIGN KEY (tenant_id, actor_id) REFERENCES grace.identities(tenant_id, id),
  FOREIGN KEY (tenant_id, environment) REFERENCES grace.environment_controls(tenant_id, environment)
);
CREATE TABLE grace.reconciliation_incidents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  pool_id uuid NOT NULL,
  reservation_id uuid,
  incident_type text NOT NULL,
  state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'investigating', 'resolved')),
  opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  resolved_at timestamptz,
  details jsonb NOT NULL,
  FOREIGN KEY (tenant_id, environment, pool_id) REFERENCES grace.resource_pools(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id)
);
CREATE TABLE grace.notifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  environment text NOT NULL,
  reservation_id uuid NOT NULL,
  identity_id uuid NOT NULL,
  channel text NOT NULL CHECK (channel IN ('email', 'webhook', 'in_app')),
  event_key text NOT NULL,
  delivery_state text NOT NULL DEFAULT 'pending'
    CHECK (delivery_state IN ('pending', 'sending', 'delivered', 'failed')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  delivered_at timestamptz,
  FOREIGN KEY (tenant_id, environment, reservation_id) REFERENCES grace.reservations(tenant_id, environment, id),
  FOREIGN KEY (tenant_id, identity_id) REFERENCES grace.identities(tenant_id, id),
  UNIQUE (reservation_id, identity_id, channel, event_key)
);

-- The role issuing business mutations must not own these objects. Platform
-- migrations retain owner access; applications receive explicit minimal grants.
REVOKE ALL ON ALL TABLES IN SCHEMA grace FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA grace FROM PUBLIC;
COMMIT;
