# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

from core.cdk_inventory_store import CdkInventoryStore


class PaymeshBatchAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = CdkInventoryStore(Path(self.temp_dir.name) / "cdk-inventory.sqlite3")
        self.inventory_ids = [
            self.store.import_cdk("paymesh", f"CARD-{index}", configured_limit=6)[0].inventory_id
            for index in range(1, 6)
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_job_assignments_cycle_across_all_cards_before_reuse(self):
        from core.paymesh_batch_assignment import assign_paymesh_jobs

        assignments = assign_paymesh_jobs(self.inventory_ids, count=12)

        self.assertEqual(
            assignments,
            [
                self.inventory_ids[0],
                self.inventory_ids[1],
                self.inventory_ids[2],
                self.inventory_ids[3],
                self.inventory_ids[4],
                self.inventory_ids[0],
                self.inventory_ids[1],
                self.inventory_ids[2],
                self.inventory_ids[3],
                self.inventory_ids[4],
                self.inventory_ids[0],
                self.inventory_ids[1],
            ],
        )

    def test_assignments_are_independent_of_earlier_job_failures(self):
        from core.paymesh_batch_assignment import assign_paymesh_jobs

        assignments = assign_paymesh_jobs(self.inventory_ids, count=30)
        failed_jobs = {0, 1, 2, 5, 6, 7}
        completed = [
            inventory_id
            for job_index, inventory_id in enumerate(assignments)
            if job_index not in failed_jobs
        ]

        self.assertEqual(assignments[3:5], self.inventory_ids[3:5])
        self.assertIn(self.inventory_ids[3], completed)
        self.assertIn(self.inventory_ids[4], completed)
        self.assertEqual(assignments.count(self.inventory_ids[4]), 6)


if __name__ == "__main__":
    unittest.main()
