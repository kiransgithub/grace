-- Correctness path: serialized accounting bins + interval event sweep.
-- Requires 001_control_plane.sql. Runtime role must use these functions and
-- must not get direct INSERT/UPDATE/DELETE grants on state tables.
BEGIN;

CREATE FUNCTION grace.assert_current_epoch(p_expected_epoch bigint) RETURNS void
LANGUAGE plpgsql VOLATILE AS $$
DECLARE fence grace.dr_fence%ROWTYPE;
BEGIN
  IF current_setting('transaction_isolation') NOT IN ('read committed', 'serializable') THEN
    RAISE EXCEPTION 'Use READ COMMITTED or SERIALIZABLE; retry whole transactions on 40001/40P01'
      USING ERRCODE = 'GRC13';
  END IF;
  SELECT * INTO STRICT fence FROM grace.dr_fence WHERE singleton FOR SHARE;
  IF NOT fence.mutations_enabled OR fence.epoch <> p_expected_epoch THEN
    RAISE EXCEPTION 'DR fence closed or stale epoch' USING ERRCODE = 'GRC01';
  END IF;
END;
$$;

CREATE FUNCTION grace.protect_capacity_lease() RETURNS trigger
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
  gpu grace.physical_gpus%ROWTYPE;
  alloc grace.allocations%ROWTYPE;
  res grace.reservations%ROWTYPE;
  pool grace.resource_pools%ROWTYPE;
  peak_fraction bigint;
  peak_memory bigint;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Capacity leases are retained, never deleted' USING ERRCODE = 'GRC02';
  END IF;
  SELECT * INTO STRICT alloc FROM grace.allocations WHERE id = NEW.allocation_id;
  PERFORM grace.assert_current_epoch(alloc.dr_epoch);
  IF TG_OP = 'UPDATE' AND (
    NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.environment <> OLD.environment
    OR NEW.allocation_id <> OLD.allocation_id OR NEW.accounting_gpu_id <> OLD.accounting_gpu_id
    OR NEW.pool_id <> OLD.pool_id
  ) THEN
    RAISE EXCEPTION 'Lease identity and accounting target are immutable; use a new allocation' USING ERRCODE = 'GRC03';
  END IF;
  IF TG_OP = 'UPDATE' AND OLD.state = 'released' THEN
    RAISE EXCEPTION 'Released lease is immutable' USING ERRCODE = 'GRC03';
  END IF;
  IF NEW.state = 'released' THEN
    IF TG_OP = 'INSERT' THEN
      RAISE EXCEPTION 'Cannot create an already released lease' USING ERRCODE = 'GRC03';
    END IF;
    IF alloc.state <> 'planned' AND alloc.cleanup_observation_id IS NULL THEN
      RAISE EXCEPTION 'Dispatched capacity needs verified cleanup before release' USING ERRCODE = 'GRC04';
    END IF;
    RETURN NEW;
  END IF;
  IF alloc.state IN ('released', 'quarantined') THEN
    RAISE EXCEPTION 'Allocation cannot own a new active lease' USING ERRCODE = 'GRC03';
  END IF;
  -- This row is the serialization point, not a distributed lock / advisory cache.
  SELECT * INTO STRICT gpu FROM grace.physical_gpus WHERE id = NEW.accounting_gpu_id FOR UPDATE;
  SELECT * INTO STRICT pool FROM grace.resource_pools WHERE id = gpu.pool_id;
  SELECT * INTO STRICT res FROM grace.reservations WHERE id = alloc.reservation_id;
  IF NOT gpu.allocatable OR gpu.health <> 'healthy' OR gpu.observed_at IS NULL
    OR gpu.observed_at < clock_timestamp() - make_interval(secs => pool.observations_max_age_seconds)
    OR gpu.observed_at > clock_timestamp() + interval '5 seconds' THEN
    RAISE EXCEPTION 'GPU inventory is unhealthy, stale or has invalid clock' USING ERRCODE = 'GRC05';
  END IF;
  IF NOT pool.enabled OR NEW.fraction_millis <> res.fraction_millis
    OR NEW.memory_mib <> ((gpu.memory_mib * NEW.fraction_millis + 999) / 1000)
    OR NEW.starts_at <> res.starts_at OR NEW.ends_at <> res.ends_at
    OR (pool.sharing_mode = 'exclusive' AND NEW.fraction_millis <> 1000) THEN
    RAISE EXCEPTION 'Lease shape must match authorized reservation and pool policy' USING ERRCODE = 'GRC06';
  END IF;
  IF res.memory_mib > (gpu.memory_mib * NEW.fraction_millis / 1000) THEN
    RAISE EXCEPTION 'Requested memory exceeds the proportional fractional-memory budget' USING ERRCODE = 'GRC06';
  END IF;
  IF pool.maintenance_starts_at IS NOT NULL
    AND NEW.starts_at < pool.maintenance_ends_at AND NEW.ends_at > pool.maintenance_starts_at THEN
    RAISE EXCEPTION 'Requested interval intersects maintenance' USING ERRCODE = 'GRC06';
  END IF;
  -- A SUM of all overlapping rows is wrong: two leases might overlap this new
  -- interval but not each other. Sweep distinct start/end events and inspect
  -- peak simultaneous occupancy. Group ties first: intervals are [start,end).
  WITH intervals AS (
    SELECT greatest(l.starts_at, NEW.starts_at) AS s,
      least(CASE WHEN a.state = 'planned' THEN l.ends_at ELSE 'infinity'::timestamptz END, NEW.ends_at) AS e,
      l.fraction_millis::bigint AS f, l.memory_mib AS m
    FROM grace.capacity_leases l JOIN grace.allocations a ON a.id = l.allocation_id
    WHERE l.accounting_gpu_id = NEW.accounting_gpu_id AND l.id <> NEW.id
      AND l.state <> 'released' AND l.starts_at < NEW.ends_at
      AND (a.state <> 'planned' OR l.ends_at > NEW.starts_at)
    UNION ALL
    SELECT NEW.starts_at, NEW.ends_at, NEW.fraction_millis::bigint, NEW.memory_mib
  ), events AS (
    SELECT s AS t, f AS df, m AS dm FROM intervals
    UNION ALL
    SELECT e, -f, -m FROM intervals
  ), grouped AS (
    SELECT t, sum(df) AS df, sum(dm) AS dm FROM events GROUP BY t
  ), occupancy AS (
    SELECT sum(df) OVER (ORDER BY t ROWS UNBOUNDED PRECEDING) AS f,
      sum(dm) OVER (ORDER BY t ROWS UNBOUNDED PRECEDING) AS m FROM grouped
  ) SELECT coalesce(max(f), 0), coalesce(max(m), 0) INTO peak_fraction, peak_memory FROM occupancy;
  IF peak_fraction + gpu.external_fraction_millis > 1000
    OR peak_memory + gpu.external_memory_mib > gpu.usable_memory_mib THEN
    RAISE EXCEPTION 'GPU fraction or memory capacity is already committed'
      USING ERRCODE = 'GRC07', DETAIL = 'Retry another eligible accounting placement; do not overbook.';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER guard_capacity_lease BEFORE INSERT OR UPDATE OR DELETE ON grace.capacity_leases
FOR EACH ROW EXECUTE FUNCTION grace.protect_capacity_lease();

CREATE FUNCTION grace.acquire_capacity(
  p_reservation_id uuid, p_gpu_ids uuid[], p_expected_dr_epoch bigint
) RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, grace AS $$
DECLARE
  res grace.reservations%ROWTYPE;
  app grace.applications%ROWTYPE;
  pool grace.resource_pools%ROWTYPE;
  alloc_id uuid := gen_random_uuid();
  first_gpu grace.physical_gpus%ROWTYPE;
  gpu grace.physical_gpus%ROWTYPE;
  selected_count integer;
  selected_node uuid;
  selected_fabric text;
BEGIN
  PERFORM grace.assert_current_epoch(p_expected_dr_epoch);
  SELECT * INTO STRICT res FROM grace.reservations WHERE id = p_reservation_id FOR UPDATE;
  IF res.state NOT IN ('requested', 'queued') OR res.ends_at <= clock_timestamp() THEN
    RAISE EXCEPTION 'Reservation is not eligible for a new allocation' USING ERRCODE = 'GRC03';
  END IF;
  IF res.assurance <> 'best_effort' THEN
    RAISE EXCEPTION 'Backend-held assurance requires a verified backend hold adapter' USING ERRCODE = 'GRC08';
  END IF;
  IF res.starts_at > clock_timestamp() THEN
    RAISE EXCEPTION 'Future starts remain queued until activation; no unverified future capacity promise'
      USING ERRCODE = 'GRC08';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM grace.environment_controls c WHERE c.tenant_id = res.tenant_id
    AND c.environment = res.environment AND c.enabled
    AND (res.environment <> 'production' OR c.production_requests_enabled)) THEN
    RAISE EXCEPTION 'Environment is disabled' USING ERRCODE = 'GRC09';
  END IF;
  IF cardinality(p_gpu_ids) IS DISTINCT FROM res.gpu_count OR EXISTS (
    SELECT 1 FROM unnest(p_gpu_ids) AS g(id) GROUP BY id HAVING count(*) > 1 OR id IS NULL
  ) THEN
    RAISE EXCEPTION 'Request must contain exactly gpu_count distinct accounting devices' USING ERRCODE = 'GRC06';
  END IF;
  -- Consistent lock order prevents gang allocations deadlocking on GPU order.
  PERFORM id FROM grace.physical_gpus WHERE id = ANY(p_gpu_ids) ORDER BY id FOR UPDATE;
  SELECT count(*) INTO selected_count FROM grace.physical_gpus
    WHERE id = ANY(p_gpu_ids) AND tenant_id = res.tenant_id AND environment = res.environment;
  IF selected_count <> res.gpu_count THEN
    RAISE EXCEPTION 'Device not found in authorized tenant/environment' USING ERRCODE = 'GRC09';
  END IF;
  SELECT * INTO STRICT first_gpu FROM grace.physical_gpus WHERE id = p_gpu_ids[1];
  SELECT * INTO STRICT pool FROM grace.resource_pools WHERE id = first_gpu.pool_id;
  SELECT * INTO STRICT app FROM grace.applications WHERE id = res.application_id;
  IF NOT EXISTS (SELECT 1 FROM grace.clusters c WHERE c.id = pool.cluster_id AND c.onboarding_state = 'ready')
    OR (res.strict_pool_id IS NOT NULL AND res.strict_pool_id <> pool.id)
    OR app.trust_domain <> pool.trust_domain
    OR NOT EXISTS (SELECT 1 FROM grace.project_pool_access a WHERE a.tenant_id = res.tenant_id
      AND a.environment = res.environment AND a.project_id = app.project_id AND a.pool_id = pool.id
      AND (res.fraction_millis = 1000 OR a.fractional_sharing_approved)) THEN
    RAISE EXCEPTION 'Placement violates readiness, location, project access or trust policy' USING ERRCODE = 'GRC09';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM grace.data_attestations d WHERE d.reservation_id = res.id
      AND d.pool_id = pool.id AND d.available)
    OR EXISTS (SELECT 1 FROM grace.data_attestations d WHERE d.reservation_id = res.id
      AND d.pool_id = pool.id AND NOT d.available) THEN
    RAISE EXCEPTION 'Data availability is not attested for selected pool' USING ERRCODE = 'GRC10';
  END IF;
  selected_node := first_gpu.node_id;
  SELECT fabric_domain INTO selected_fabric FROM grace.gpu_nodes WHERE id = selected_node;
  FOR gpu IN SELECT * FROM grace.physical_gpus WHERE id = ANY(p_gpu_ids) ORDER BY id LOOP
    IF gpu.pool_id <> pool.id OR gpu.model <> res.gpu_model
      OR (res.topology = 'single_node' AND gpu.node_id <> selected_node)
      OR (res.topology = 'same_fabric' AND (selected_fabric IS NULL OR NOT EXISTS (
        SELECT 1 FROM grace.gpu_nodes n WHERE n.id = gpu.node_id AND n.fabric_domain = selected_fabric
      ))) THEN
      RAISE EXCEPTION 'Gang placement violates model, pool or topology requirements' USING ERRCODE = 'GRC06';
    END IF;
  END LOOP;
  INSERT INTO grace.allocations(id, tenant_id, environment, reservation_id, pool_id, dr_epoch)
    VALUES (alloc_id, res.tenant_id, res.environment, res.id, pool.id, p_expected_dr_epoch);
  FOR gpu IN SELECT * FROM grace.physical_gpus WHERE id = ANY(p_gpu_ids) ORDER BY id LOOP
    INSERT INTO grace.capacity_leases(tenant_id, environment, pool_id, allocation_id, accounting_gpu_id,
      fraction_millis, memory_mib, starts_at, ends_at)
    VALUES (res.tenant_id, res.environment, pool.id, alloc_id, gpu.id,
      res.fraction_millis, ((gpu.memory_mib * res.fraction_millis + 999) / 1000), res.starts_at, res.ends_at);
  END LOOP;
  UPDATE grace.reservations SET state = 'held', version = version + 1, updated_at = clock_timestamp()
    WHERE id = res.id RETURNING * INTO res;
  INSERT INTO grace.outbox_events(tenant_id, environment, reservation_id, event_type, aggregate_version, dr_epoch, payload)
    VALUES (res.tenant_id, res.environment, res.id, 'AllocationHeld', res.version, p_expected_dr_epoch,
      jsonb_build_object('allocation_id', alloc_id));
  INSERT INTO grace.ledger_events(tenant_id, environment, reservation_id, aggregate_version, event_type, dr_epoch, facts)
    VALUES (res.tenant_id, res.environment, res.id, res.version, 'AllocationHeld', p_expected_dr_epoch,
      jsonb_build_object('allocation_id', alloc_id, 'assurance', 'best_effort'));
  RETURN alloc_id;
END;
$$;

CREATE FUNCTION grace.mark_submitting(p_allocation_id uuid, p_expected_dr_epoch bigint)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, grace AS $$
DECLARE alloc grace.allocations%ROWTYPE; res grace.reservations%ROWTYPE; pool grace.resource_pools%ROWTYPE;
BEGIN
  PERFORM grace.assert_current_epoch(p_expected_dr_epoch);
  SELECT r.* INTO STRICT res FROM grace.reservations r JOIN grace.allocations a ON a.reservation_id = r.id
    WHERE a.id = p_allocation_id FOR UPDATE OF r;
  SELECT * INTO STRICT alloc FROM grace.allocations WHERE id = p_allocation_id FOR UPDATE;
  IF alloc.dr_epoch <> p_expected_dr_epoch OR alloc.state <> 'planned' OR res.state <> 'held'
    OR res.starts_at > clock_timestamp() OR res.ends_at <= clock_timestamp() THEN
    RAISE EXCEPTION 'Dispatch cannot cross cancellation, reservation time or DR fence' USING ERRCODE = 'GRC03';
  END IF;
  SELECT * INTO STRICT pool FROM grace.resource_pools WHERE id = alloc.pool_id FOR SHARE;
  PERFORM g.id FROM grace.physical_gpus g JOIN grace.capacity_leases l ON l.accounting_gpu_id = g.id
    WHERE l.allocation_id = alloc.id ORDER BY g.id FOR UPDATE OF g;
  IF NOT pool.enabled OR NOT EXISTS (SELECT 1 FROM grace.environment_controls c
      WHERE c.tenant_id = res.tenant_id AND c.environment = res.environment AND c.enabled
        AND (res.environment <> 'production' OR c.production_requests_enabled))
    OR NOT EXISTS (SELECT 1 FROM grace.clusters c WHERE c.id = pool.cluster_id AND c.onboarding_state = 'ready')
    OR NOT EXISTS (SELECT 1 FROM grace.applications a JOIN grace.project_pool_access pa
      ON pa.tenant_id = a.tenant_id AND pa.project_id = a.project_id
      WHERE a.id = res.application_id AND a.trust_domain = pool.trust_domain
        AND pa.environment = res.environment AND pa.pool_id = pool.id
        AND (res.fraction_millis = 1000 OR pa.fractional_sharing_approved))
    OR EXISTS (SELECT 1 FROM grace.capacity_leases l JOIN grace.physical_gpus g ON g.id = l.accounting_gpu_id
      WHERE l.allocation_id = alloc.id AND (NOT g.allocatable OR g.health <> 'healthy'
        OR g.observed_at IS NULL OR g.observed_at < clock_timestamp() - make_interval(secs => pool.observations_max_age_seconds)
        OR g.observed_at > clock_timestamp() + interval '5 seconds')) THEN
    RAISE EXCEPTION 'Dispatch policy, access or observed readiness changed after the hold' USING ERRCODE = 'GRC09';
  END IF;
  IF pool.maintenance_starts_at IS NOT NULL
    AND res.starts_at < pool.maintenance_ends_at AND res.ends_at > pool.maintenance_starts_at THEN
    RAISE EXCEPTION 'Maintenance now intersects dispatch' USING ERRCODE = 'GRC06';
  END IF;
  -- Re-run effective memory/fraction/occupancy checks after the hold: external
  -- occupancy or usable capacity can change before the dispatcher starts.
  UPDATE grace.capacity_leases SET state = 'held' WHERE allocation_id = alloc.id AND state <> 'released';
  UPDATE grace.allocations SET state = 'submitting', dispatch_started_at = clock_timestamp() WHERE id = alloc.id;
  UPDATE grace.reservations SET state = 'activating', version = version + 1,
    updated_at = clock_timestamp() WHERE id = res.id RETURNING * INTO res;
  INSERT INTO grace.ledger_events(tenant_id, environment, reservation_id, aggregate_version, event_type, dr_epoch, facts)
    VALUES (res.tenant_id, res.environment, res.id, res.version, 'DispatchStarted', p_expected_dr_epoch,
      jsonb_build_object('allocation_id', alloc.id, 'fence_token', alloc.fence_token));
  RETURN alloc.fence_token;
END;
$$;

CREATE FUNCTION grace.request_cancellation(p_reservation_id uuid, p_expected_dr_epoch bigint)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, grace AS $$
DECLARE res grace.reservations%ROWTYPE; alloc grace.allocations%ROWTYPE; final_state text;
BEGIN
  PERFORM grace.assert_current_epoch(p_expected_dr_epoch);
  SELECT * INTO STRICT res FROM grace.reservations WHERE id = p_reservation_id FOR UPDATE;
  IF res.state IN ('released', 'rejected', 'failed') THEN RETURN res.state; END IF;
  IF res.state IN ('cancel_requested', 'releasing') THEN RETURN res.state; END IF;
  SELECT * INTO alloc FROM grace.allocations WHERE reservation_id = res.id AND state <> 'released' FOR UPDATE;
  IF NOT FOUND THEN
    final_state := 'released';
  ELSIF alloc.dr_epoch <> p_expected_dr_epoch THEN
    RAISE EXCEPTION 'Old allocation must be reconciled into recovery epoch first' USING ERRCODE = 'GRC01';
  ELSIF alloc.state = 'planned' THEN
    -- Safe because dispatch must transition planned -> submitting while taking
    -- the same reservation/allocation locks, before any provider network call.
    UPDATE grace.capacity_leases SET state = 'released' WHERE allocation_id = alloc.id AND state <> 'released';
    UPDATE grace.allocations SET state = 'released' WHERE id = alloc.id;
    final_state := 'released';
  ELSE
    UPDATE grace.allocations SET state = 'releasing', stop_requested_at = coalesce(stop_requested_at, clock_timestamp())
      WHERE id = alloc.id;
    final_state := 'cancel_requested';
  END IF;
  UPDATE grace.reservations SET state = final_state, version = version + 1, updated_at = clock_timestamp()
    WHERE id = res.id RETURNING * INTO res;
  INSERT INTO grace.outbox_events(tenant_id, environment, reservation_id, event_type, aggregate_version, dr_epoch, payload)
    VALUES (res.tenant_id, res.environment, res.id, 'CancellationRequested', res.version, p_expected_dr_epoch,
      jsonb_build_object('allocation_id', alloc.id, 'state', final_state));
  INSERT INTO grace.ledger_events(tenant_id, environment, reservation_id, aggregate_version, event_type, dr_epoch, facts)
    VALUES (res.tenant_id, res.environment, res.id, res.version, 'CancellationRequested', p_expected_dr_epoch,
      jsonb_build_object('state', final_state));
  RETURN final_state;
END;
$$;

CREATE FUNCTION grace.record_ownership_fence(
  p_allocation_id uuid, p_observation_id uuid, p_expected_dr_epoch bigint
) RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, grace AS $$
DECLARE alloc grace.allocations%ROWTYPE; obs grace.infrastructure_observations%ROWTYPE; pool grace.resource_pools%ROWTYPE;
BEGIN
  PERFORM grace.assert_current_epoch(p_expected_dr_epoch);
  PERFORM r.id FROM grace.reservations r JOIN grace.allocations a ON a.reservation_id = r.id
    WHERE a.id = p_allocation_id FOR UPDATE OF r;
  SELECT * INTO STRICT alloc FROM grace.allocations WHERE id = p_allocation_id FOR UPDATE;
  SELECT * INTO STRICT pool FROM grace.resource_pools WHERE id = alloc.pool_id;
  SELECT * INTO STRICT obs FROM grace.infrastructure_observations WHERE id = p_observation_id;
  IF alloc.dr_epoch <> p_expected_dr_epoch OR alloc.state <> 'releasing' OR alloc.stop_requested_at IS NULL
    OR obs.tenant_id <> alloc.tenant_id OR obs.environment <> alloc.environment OR obs.cluster_id <> pool.cluster_id
    OR obs.source <> 'kubernetes' OR obs.observation_kind <> 'allocation_ownership_fenced'
    OR obs.observed_at < alloc.stop_requested_at
    OR obs.observed_at < clock_timestamp() - make_interval(secs => pool.observations_max_age_seconds)
    OR obs.observed_at > clock_timestamp() + interval '5 seconds'
    OR (obs.facts->>'allocation_id') IS DISTINCT FROM alloc.id::text
    OR (obs.facts->>'dr_epoch') IS DISTINCT FROM p_expected_dr_epoch::text
    OR (obs.facts->>'fence_token') IS DISTINCT FROM alloc.fence_token::text
    OR (obs.facts->'ownership_fenced') IS DISTINCT FROM 'true'::jsonb THEN
    RAISE EXCEPTION 'Authoritative ownership fence evidence is missing or invalid' USING ERRCODE = 'GRC04';
  END IF;
  UPDATE grace.allocations SET ownership_fenced_at = obs.observed_at, fencing_observation_id = obs.id WHERE id = alloc.id;
END;
$$;

CREATE FUNCTION grace.release_after_cleanup(
  p_allocation_id uuid, p_observation_id uuid, p_expected_dr_epoch bigint
) RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, grace AS $$
DECLARE alloc grace.allocations%ROWTYPE; res grace.reservations%ROWTYPE; obs grace.infrastructure_observations%ROWTYPE;
  pool grace.resource_pools%ROWTYPE;
BEGIN
  PERFORM grace.assert_current_epoch(p_expected_dr_epoch);
  SELECT r.* INTO STRICT res FROM grace.reservations r JOIN grace.allocations a ON a.reservation_id = r.id
    WHERE a.id = p_allocation_id FOR UPDATE OF r;
  SELECT * INTO STRICT alloc FROM grace.allocations WHERE id = p_allocation_id FOR UPDATE;
  IF alloc.state = 'released' THEN RETURN; END IF;
  IF alloc.dr_epoch <> p_expected_dr_epoch THEN
    RAISE EXCEPTION 'Stale allocation epoch' USING ERRCODE = 'GRC01';
  END IF;
  SELECT * INTO STRICT pool FROM grace.resource_pools WHERE id = alloc.pool_id;
  SELECT * INTO STRICT obs FROM grace.infrastructure_observations WHERE id = p_observation_id;
  IF alloc.state <> 'releasing' OR alloc.stop_requested_at IS NULL OR alloc.ownership_fenced_at IS NULL
    OR obs.tenant_id <> alloc.tenant_id OR obs.environment <> alloc.environment
    OR obs.cluster_id <> pool.cluster_id
    OR obs.source <> 'kubernetes' OR obs.observation_kind <> 'allocation_cleanup_complete'
    OR NOT obs.is_complete_snapshot
    OR obs.observed_at <= greatest(alloc.stop_requested_at, alloc.ownership_fenced_at)
    OR obs.observed_at < clock_timestamp() - make_interval(secs => pool.observations_max_age_seconds)
    OR obs.observed_at > clock_timestamp() + interval '5 seconds'
    OR (obs.facts->>'allocation_id') IS DISTINCT FROM alloc.id::text
    OR (obs.facts->>'dr_epoch') IS DISTINCT FROM p_expected_dr_epoch::text
    OR (obs.facts->>'fence_token') IS DISTINCT FROM alloc.fence_token::text
    OR (obs.facts->'ownership_fenced') IS DISTINCT FROM 'true'::jsonb
    OR (obs.facts->'scheduler_share_released') IS DISTINCT FROM 'true'::jsonb
    OR (obs.facts->'dispatch_reconciled') IS DISTINCT FROM 'true'::jsonb
    OR (obs.facts->'remaining_resource_uids') IS DISTINCT FROM '[]'::jsonb THEN
    RAISE EXCEPTION 'Cleanup evidence is missing, stale or scoped to another allocation' USING ERRCODE = 'GRC04';
  END IF;
  UPDATE grace.allocations SET cleanup_observation_id = obs.id, state = 'releasing' WHERE id = alloc.id;
  UPDATE grace.capacity_leases SET state = 'released' WHERE allocation_id = alloc.id AND state <> 'released';
  UPDATE grace.allocations SET state = 'released' WHERE id = alloc.id;
  UPDATE grace.reservations SET state = 'released', version = version + 1,
    updated_at = clock_timestamp() WHERE id = res.id RETURNING * INTO res;
  INSERT INTO grace.outbox_events(tenant_id, environment, reservation_id, event_type, aggregate_version, dr_epoch, payload)
    VALUES (res.tenant_id, res.environment, res.id, 'CapacityReleased', res.version, p_expected_dr_epoch,
      jsonb_build_object('allocation_id', alloc.id, 'observation_id', obs.id));
  INSERT INTO grace.ledger_events(tenant_id, environment, reservation_id, aggregate_version, event_type, dr_epoch, facts)
    VALUES (res.tenant_id, res.environment, res.id, res.version, 'CapacityReleased', p_expected_dr_epoch,
      jsonb_build_object('allocation_id', alloc.id, 'observation_id', obs.id));
END;
$$;

CREATE FUNCTION grace.reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Append-only history: write a corrective event instead' USING ERRCODE = 'GRC11'; END;
$$;
CREATE TRIGGER immutable_ledger BEFORE UPDATE OR DELETE ON grace.ledger_events
  FOR EACH ROW EXECUTE FUNCTION grace.reject_history_mutation();
CREATE TRIGGER immutable_audit BEFORE UPDATE OR DELETE ON grace.audit_events
  FOR EACH ROW EXECUTE FUNCTION grace.reject_history_mutation();
CREATE TRIGGER immutable_usage BEFORE UPDATE OR DELETE ON grace.usage_facts
  FOR EACH ROW EXECUTE FUNCTION grace.reject_history_mutation();
CREATE TRIGGER immutable_cost BEFORE UPDATE OR DELETE ON grace.cost_entries
  FOR EACH ROW EXECUTE FUNCTION grace.reject_history_mutation();
CREATE TRIGGER immutable_observation BEFORE UPDATE OR DELETE ON grace.infrastructure_observations
  FOR EACH ROW EXECUTE FUNCTION grace.reject_history_mutation();
CREATE TRIGGER immutable_prices BEFORE UPDATE OR DELETE ON grace.price_rates
  FOR EACH ROW EXECUTE FUNCTION grace.reject_history_mutation();

CREATE FUNCTION grace.check_rate_overlap() RETURNS trigger LANGUAGE plpgsql VOLATILE AS $$
BEGIN
  PERFORM id FROM grace.resource_pools WHERE id = NEW.pool_id FOR UPDATE;
  IF EXISTS (SELECT 1 FROM grace.price_rates r WHERE r.pool_id = NEW.pool_id
    AND r.rate_kind = NEW.rate_kind AND r.currency = NEW.currency
    AND r.effective_from < NEW.effective_until AND r.effective_until > NEW.effective_from) THEN
    RAISE EXCEPTION 'Rate intervals overlap' USING ERRCODE = 'GRC12';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER nonoverlapping_rates BEFORE INSERT ON grace.price_rates
  FOR EACH ROW EXECUTE FUNCTION grace.check_rate_overlap();

CREATE FUNCTION grace.guard_inventory_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.environment <> OLD.environment THEN
    RAISE EXCEPTION 'Inventory identity/ownership changes require an audited migration' USING ERRCODE = 'GRC03';
  END IF;
  IF TG_TABLE_NAME = 'clusters' THEN
    IF (to_jsonb(OLD)->>'immutable_cluster_uid') IS NOT NULL
      AND (to_jsonb(NEW)->>'immutable_cluster_uid') IS DISTINCT FROM (to_jsonb(OLD)->>'immutable_cluster_uid') THEN
      RAISE EXCEPTION 'Registered Kubernetes cluster UID is immutable' USING ERRCODE = 'GRC03';
    END IF;
  ELSIF TG_TABLE_NAME = 'gpu_nodes' THEN
    IF (to_jsonb(NEW)->>'kubernetes_node_uid') IS DISTINCT FROM (to_jsonb(OLD)->>'kubernetes_node_uid') THEN
      RAISE EXCEPTION 'Kubernetes node UID is immutable' USING ERRCODE = 'GRC03';
    END IF;
  ELSIF TG_TABLE_NAME = 'physical_gpus' THEN
    IF (to_jsonb(NEW)->>'hardware_uuid') IS DISTINCT FROM (to_jsonb(OLD)->>'hardware_uuid') THEN
      RAISE EXCEPTION 'Physical GPU UUID is immutable' USING ERRCODE = 'GRC03';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER immutable_cluster_identity BEFORE UPDATE ON grace.clusters
  FOR EACH ROW EXECUTE FUNCTION grace.guard_inventory_identity();
CREATE TRIGGER immutable_node_identity BEFORE UPDATE ON grace.gpu_nodes
  FOR EACH ROW EXECUTE FUNCTION grace.guard_inventory_identity();
CREATE TRIGGER immutable_gpu_identity BEFORE UPDATE ON grace.physical_gpus
  FOR EACH ROW EXECUTE FUNCTION grace.guard_inventory_identity();

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA grace FROM PUBLIC;
COMMIT;
