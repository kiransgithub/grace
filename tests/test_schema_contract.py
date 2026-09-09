"""Source contracts only; these tests do not execute PostgreSQL or prove locks."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "migrations/001_control_plane.sql").read_text()
FUNCTIONS = (ROOT / "migrations/002_capacity_transactions.sql").read_text()


class MigrationSourceContract(unittest.TestCase):
    def test_migrations_are_transactional_and_non_destructive(self):
        for sql in (SCHEMA, FUNCTIONS):
            self.assertIn("BEGIN;", sql)
            self.assertTrue(sql.rstrip().endswith("COMMIT;"))
            self.assertNotRegex(sql, r"(?im)^\s*(DROP\s|TRUNCATE\s)")

    def test_fractional_capacity_uses_integer_and_event_sweep(self):
        self.assertIn("fraction_millis BETWEEN 1 AND 1000", SCHEMA)
        self.assertIn("GROUP BY t", FUNCTIONS)
        self.assertIn("ROWS UNBOUNDED PRECEDING", FUNCTIONS)
        self.assertIn("FOR UPDATE", FUNCTIONS)
        self.assertIn("external_memory_mib > gpu.usable_memory_mib", FUNCTIONS)
        self.assertIn("gpu.memory_mib * res.fraction_millis + 999", FUNCTIONS)
        self.assertNotIn("EXCLUDE", SCHEMA)

    def test_execution_and_usage_cannot_reference_other_reservation(self):
        allocation_fk = "REFERENCES grace.allocations(tenant_id, environment, reservation_id, id)"
        self.assertEqual(SCHEMA.count(allocation_fk), 2)
        self.assertIn("REFERENCES grace.workloads(tenant_id, environment, reservation_id, id)", SCHEMA)

    def test_future_guarantee_and_expiry_are_conservative(self):
        self.assertIn("Future starts remain queued until activation", FUNCTIONS)
        self.assertIn("ELSE 'infinity'::timestamptz", FUNCTIONS)
        self.assertIn("ownership_fenced_at", FUNCTIONS)
        self.assertIn("scheduler_share_released", FUNCTIONS)
        self.assertIn("dispatch_reconciled", FUNCTIONS)
        self.assertIn("NOT obs.is_complete_snapshot", FUNCTIONS)

    def test_external_mapping_is_not_uuid_pinning(self):
        self.assertIn("accounting_gpu_id", SCHEMA)
        self.assertIn("actual_gpu_id", SCHEMA)
        self.assertIn("workload_gpu_bindings", SCHEMA)
        self.assertIn("This never claims KAI will pin this hardware UUID", SCHEMA)

    def test_strict_roles_and_append_only_ledger(self):
        self.assertIn("REVOKE ALL ON ALL TABLES IN SCHEMA grace FROM PUBLIC", SCHEMA)
        self.assertIn("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA grace FROM PUBLIC", FUNCTIONS)
        self.assertGreaterEqual(FUNCTIONS.count("SECURITY DEFINER SET search_path = pg_catalog, grace"), 5)
        for name in ("immutable_ledger", "immutable_audit", "immutable_usage", "immutable_cost", "immutable_observation"):
            self.assertIn(name, FUNCTIONS)

    def test_at_least_once_and_idempotency_structures(self):
        for name in ("outbox_events", "idempotency_records", "consumer_receipts", "execution_attempts"):
            self.assertIn(f"CREATE TABLE grace.{name}", SCHEMA)
        self.assertIn("UNIQUE (skypilot_realm, command_key)", SCHEMA)
        self.assertIn("UNIQUE (tenant_id, environment, charge_event_key)", SCHEMA)

    def test_no_mig_or_float_allocation_type(self):
        self.assertNotRegex(SCHEMA, r"(?i)\b(double precision|real|float)\b")
        self.assertNotRegex(SCHEMA, r"CREATE TABLE grace\.(mig|accelerator_slice)")


if __name__ == "__main__":
    unittest.main()
