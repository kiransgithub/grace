"""Opt-in real PostgreSQL tests; requires a fresh, explicitly disposable DB.

Run with GRACE_TEST_POSTGRES_DISPOSABLE=1 and GRACE_TEST_POSTGRES_DSN set.
No database is started and no existing schema is dropped by these tests.
The role must own the empty test DB. Do not point this at a production endpoint.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from uuid import uuid4

try:
    import psycopg
except ImportError:
    psycopg = None

ROOT = Path(__file__).resolve().parents[1]
DSN = os.getenv("GRACE_TEST_POSTGRES_DSN")
ENABLED = bool(DSN and os.getenv("GRACE_TEST_POSTGRES_DISPOSABLE") == "1" and psycopg)


@unittest.skipUnless(ENABLED, "Requires psycopg and an explicitly disposable fresh PostgreSQL database")
class PostgreSQLCapacityIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(DSN, autocommit=True) as c:
            if c.execute("SELECT to_regnamespace('grace')").fetchone()[0] is not None:
                raise RuntimeError("Refusing to modify an existing grace schema; use a fresh disposable test DB")
            for path in sorted((ROOT / "migrations").glob("*.sql")):
                c.execute(path.read_text(), prepare=False)
            c.execute("UPDATE grace.dr_fence SET mutations_enabled=true, active_site='test'")

    def setUp(self):
        self.c = psycopg.connect(DSN, autocommit=True)
        self.ids = {key: str(uuid4()) for key in ("tenant", "bu", "project", "app", "identity", "cluster", "pool", "node", "gpu", "gpu2")}
        i = self.ids
        with self.c.transaction():
            self.c.execute("INSERT INTO grace.organizations(id,name) VALUES (%s,'test')", (i["tenant"],))
            self.c.execute("INSERT INTO grace.business_units(id,tenant_id,name,cost_center) VALUES (%s,%s,'bu','cc')", (i["bu"], i["tenant"]))
            self.c.execute("INSERT INTO grace.projects(id,tenant_id,business_unit_id,name) VALUES (%s,%s,%s,'project')", (i["project"], i["tenant"], i["bu"]))
            self.c.execute("INSERT INTO grace.applications(id,tenant_id,project_id,name,trust_domain) VALUES (%s,%s,%s,'app','trusted')", (i["app"], i["tenant"], i["project"]))
            self.c.execute("INSERT INTO grace.identities(id,tenant_id,issuer,subject,identity_kind) VALUES (%s,%s,'okta-test','test-user','human')", (i["identity"], i["tenant"]))
            self.c.execute("INSERT INTO grace.environment_controls(tenant_id,environment,enabled) VALUES (%s,'dev',true)", (i["tenant"],))
            self.c.execute("INSERT INTO grace.clusters(id,tenant_id,environment,name,provider,region,connector_ref,trust_domain,onboarding_state,policy_version,immutable_cluster_uid) VALUES (%s,%s,'dev','cluster','onprem','lab','configured-ref','trusted','ready','test',%s)", (i["cluster"], i["tenant"],i["cluster"]))
            self.c.execute("INSERT INTO grace.resource_pools(id,tenant_id,environment,cluster_id,name,gpu_model,memory_class_mib,sharing_mode,trust_domain,kai_queue,enabled,observations_max_age_seconds) VALUES (%s,%s,'dev',%s,'pool','A100-40GB',40960,'kai_fractional','trusted','test-queue',true,60)", (i["pool"], i["tenant"], i["cluster"]))
            self.c.execute("INSERT INTO grace.project_pool_access(tenant_id,environment,project_id,pool_id,fractional_sharing_approved) VALUES (%s,'dev',%s,%s,true)", (i["tenant"], i["project"], i["pool"]))
            self.c.execute("INSERT INTO grace.gpu_nodes(id,tenant_id,environment,cluster_id,kubernetes_node_uid,name,zone,fabric_domain,cpu_millicores,host_memory_mib) VALUES (%s,%s,'dev',%s,'node-uid','node','zone-a','fabric-a',16000,65536)", (i["node"], i["tenant"], i["cluster"]))
            for key in ("gpu", "gpu2"):
                self.c.execute("INSERT INTO grace.physical_gpus(id,tenant_id,environment,cluster_id,pool_id,node_id,hardware_uuid,model,memory_mib,usable_memory_mib,health,allocatable,observed_at) VALUES (%s,%s,'dev',%s,%s,%s,%s,'A100-40GB',40960,40960,'healthy',true,clock_timestamp())", (i[key], i["tenant"], i["cluster"], i["pool"], i["node"], f"GPU-{i[key]}"))

    def tearDown(self):
        self.c.close()

    def reservation(self, fraction=250, memory=1024, count=1, starts=None, ends=None):
        i = self.ids
        rid = str(uuid4())
        now = datetime.now(timezone.utc)
        starts = starts or now - timedelta(seconds=1)
        ends = ends or now + timedelta(hours=1)
        with self.c.transaction():
            self.c.execute("INSERT INTO grace.reservations(id,tenant_id,environment,application_id,requested_by,gpu_model,gpu_count,fraction_millis,memory_mib,starts_at,ends_at,workload_class,policy_version,state) VALUES (%s,%s,'dev',%s,%s,'A100-40GB',%s,%s,%s,%s,%s,'batch','test','queued')", (rid, i["tenant"], i["app"], i["identity"], count, fraction, memory, starts, ends))
            self.c.execute("INSERT INTO grace.data_attestations(tenant_id,environment,reservation_id,pool_id,attested_by,dataset_ref,classification,available) VALUES (%s,'dev',%s,%s,%s,'catalog:test','internal',true)", (i["tenant"], rid, i["pool"], i["identity"]))
        return rid

    def acquire(self, rid, gpu_ids=None, conn=None):
        return (conn or self.c).execute("SELECT grace.acquire_capacity(%s,%s::uuid[],1)", (rid, gpu_ids or [self.ids["gpu"]])).fetchone()[0]

    def expect_error(self, sqlstate, callback):
        with self.assertRaises(psycopg.Error) as raised:
            with self.c.transaction():
                callback()
        self.assertEqual(raised.exception.sqlstate, sqlstate)

    def test_four_quarters_fit_and_fifth_rejected(self):
        for _ in range(4):
            self.acquire(self.reservation())
        self.expect_error("GRC07", lambda: self.acquire(self.reservation()))

    def test_memory_minimum_and_effective_entitlement(self):
        allocation = self.acquire(self.reservation(fraction=250, memory=512))
        reserved = self.c.execute("SELECT memory_mib FROM grace.capacity_leases WHERE allocation_id=%s", (allocation,)).fetchone()[0]
        self.assertEqual(reserved, 10240)
        self.expect_error("GRC06", lambda: self.acquire(self.reservation(fraction=250, memory=11000)))

    def test_unmanaged_memory_can_exhaust_before_compute_fraction(self):
        self.c.execute("UPDATE grace.physical_gpus SET external_memory_mib=32000 WHERE id=%s", (self.ids["gpu"],))
        self.expect_error("GRC07", lambda: self.acquire(self.reservation(fraction=250, memory=512)))

    def test_atomic_gang_rolls_back_all_leases(self):
        self.acquire(self.reservation(fraction=1000), [self.ids["gpu2"]])
        candidate = self.reservation(fraction=1000, count=2)
        self.expect_error("GRC07", lambda: self.acquire(candidate, [self.ids["gpu"], self.ids["gpu2"]]))
        count = self.c.execute("SELECT count(*) FROM grace.allocations WHERE reservation_id=%s", (candidate,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_stale_inventory_and_dr_epoch_fail_closed(self):
        self.c.execute("UPDATE grace.physical_gpus SET observed_at=clock_timestamp()-interval '5 minutes' WHERE id=%s", (self.ids["gpu"],))
        self.expect_error("GRC05", lambda: self.acquire(self.reservation()))
        self.expect_error("GRC01", lambda: self.c.execute("SELECT grace.acquire_capacity(%s,%s::uuid[],99)", (self.reservation(), [self.ids["gpu"]])))

    def test_future_start_remains_unpromised(self):
        now = datetime.now(timezone.utc)
        candidate = self.reservation(starts=now + timedelta(hours=1), ends=now + timedelta(hours=2))
        self.expect_error("GRC08", lambda: self.acquire(candidate))

    def test_unlaunched_cancellation_releases_but_dispatched_retains(self):
        rid = self.reservation(fraction=1000)
        self.acquire(rid)
        self.assertEqual(self.c.execute("SELECT grace.request_cancellation(%s,1)", (rid,)).fetchone()[0], "released")
        second = self.reservation(fraction=1000)
        allocation = self.acquire(second)
        self.c.execute("SELECT grace.mark_submitting(%s,1)", (allocation,))
        self.assertEqual(self.c.execute("SELECT grace.request_cancellation(%s,1)", (second,)).fetchone()[0], "cancel_requested")
        self.expect_error("GRC07", lambda: self.acquire(self.reservation(fraction=1000)))

    def test_policy_revocation_blocks_held_dispatch(self):
        allocation = self.acquire(self.reservation())
        self.c.execute("UPDATE grace.environment_controls SET enabled=false WHERE tenant_id=%s", (self.ids["tenant"],))
        self.expect_error("GRC09", lambda: self.c.execute("SELECT grace.mark_submitting(%s,1)", (allocation,)))

    def test_cleanup_requires_fence_then_fresh_complete_absence(self):
        rid = self.reservation(fraction=1000)
        allocation = self.acquire(rid)
        token = self.c.execute("SELECT grace.mark_submitting(%s,1)", (allocation,)).fetchone()[0]
        facts = {"allocation_id": str(allocation), "dr_epoch": 1, "fence_token": token,
                 "ownership_fenced": True, "scheduler_share_released": True,
                 "dispatch_reconciled": True, "remaining_resource_uids": []}

        def observation(kind, complete=True):
            return self.c.execute("INSERT INTO grace.infrastructure_observations(tenant_id,environment,cluster_id,source,observation_kind,observed_at,is_complete_snapshot,facts) VALUES (%s,'dev',%s,'kubernetes',%s,clock_timestamp(),%s,%s::jsonb) RETURNING id", (self.ids["tenant"],self.ids["cluster"],kind,complete,json.dumps(facts))).fetchone()[0]

        too_early = observation("allocation_cleanup_complete")
        self.expect_error("GRC04", lambda: self.c.execute("SELECT grace.release_after_cleanup(%s,%s,1)", (allocation,too_early)))
        self.c.execute("SELECT grace.request_cancellation(%s,1)", (rid,))
        ownership = observation("allocation_ownership_fenced")
        self.c.execute("SELECT grace.record_ownership_fence(%s,%s,1)", (allocation,ownership))
        self.expect_error("GRC04", lambda: self.c.execute("SELECT grace.release_after_cleanup(%s,%s,1)", (allocation,too_early)))
        partial = observation("allocation_cleanup_complete", complete=False)
        self.expect_error("GRC04", lambda: self.c.execute("SELECT grace.release_after_cleanup(%s,%s,1)", (allocation,partial)))
        final = observation("allocation_cleanup_complete")
        self.c.execute("SELECT grace.release_after_cleanup(%s,%s,1)", (allocation,final))
        self.assertEqual(self.c.execute("SELECT state FROM grace.allocations WHERE id=%s", (allocation,)).fetchone()[0], "released")
        self.acquire(self.reservation(fraction=1000))

    def test_dispatched_expiry_is_not_free_capacity(self):
        rid = self.reservation(fraction=1000)
        allocation = self.acquire(rid)
        self.c.execute("SELECT grace.mark_submitting(%s,1)", (allocation,))
        # Test-only migration-owner update simulates contractual expiry while
        # retaining an actually dispatched allocation. There is no API shortcut.
        with self.c.transaction():
            end = datetime.now(timezone.utc) - timedelta(milliseconds=10)
            self.c.execute("UPDATE grace.reservations SET ends_at=%s WHERE id=%s", (end, rid))
            self.c.execute("UPDATE grace.capacity_leases SET ends_at=%s WHERE allocation_id=%s", (end, allocation))
        self.expect_error("GRC07", lambda: self.acquire(self.reservation(fraction=1000)))

    def test_interval_sweep_does_not_sum_disjoint_holds(self):
        now = datetime.now(timezone.utc)
        for start, end in ((now-timedelta(hours=2), now-timedelta(hours=1)), (now+timedelta(hours=1), now+timedelta(hours=2))):
            rid = self.reservation(fraction=500, starts=start, ends=end)
            aid = str(uuid4())
            # Owner-only fixture directly creates planned calendar rows to
            # exercise the trigger; the MVP API cannot promise future holds.
            with self.c.transaction():
                self.c.execute("INSERT INTO grace.allocations(id,tenant_id,environment,reservation_id,pool_id,dr_epoch) VALUES (%s,%s,'dev',%s,%s,1)", (aid,self.ids["tenant"],rid,self.ids["pool"]))
                self.c.execute("INSERT INTO grace.capacity_leases(tenant_id,environment,pool_id,allocation_id,accounting_gpu_id,fraction_millis,memory_mib,starts_at,ends_at) VALUES (%s,'dev',%s,%s,%s,500,20480,%s,%s)", (self.ids["tenant"],self.ids["pool"],aid,self.ids["gpu"],start,end))
        self.acquire(self.reservation(fraction=500, starts=now-timedelta(hours=2), ends=now+timedelta(hours=2)))

    def test_cross_environment_fk_rejects_wrong_gpu(self):
        self.c.execute("INSERT INTO grace.environment_controls(tenant_id,environment,enabled) VALUES (%s,'qa',true)", (self.ids["tenant"],))
        rid = self.reservation()
        self.c.execute("DELETE FROM grace.data_attestations WHERE reservation_id=%s", (rid,))
        self.c.execute("UPDATE grace.reservations SET environment='qa' WHERE id=%s", (rid,))
        self.expect_error("GRC09", lambda: self.acquire(rid))

    def test_concurrent_quarter_requests_cannot_overbook(self):
        reservations = [self.reservation() for _ in range(20)]

        def run(rid):
            try:
                with psycopg.connect(DSN, autocommit=True) as conn:
                    self.acquire(rid, conn=conn)
                return "accepted"
            except psycopg.Error as error:
                return error.sqlstate

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(run, reservations))
        self.assertEqual(results.count("accepted"), 4, results)
        self.assertEqual(results.count("GRC07"), 16, results)


if __name__ == "__main__":
    unittest.main()
