# -*- coding: utf-8 -*-
"""
Tests for Gmail batch store polymorphism and base class abstraction.
"""
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional

import pytest

from core.gmail_batch_store_base import (
    GmailBatchStoreBase,
    Assignment,
    GmailBatchError,
    GmailBatchConflict,
)
from core.gmail_cdk_batch_store import GmailCdkBatchStore
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore


# Mock concrete implementation for base class testing
class MockBatchStore(GmailBatchStoreBase):
    """Mock implementation for testing base class."""
    
    def __init__(self, path, *, busy_timeout_ms=3000):
        super().__init__(path, busy_timeout_ms=busy_timeout_ms)
        self.polled_assignments = []
    
    def _table_prefix(self) -> str:
        return "mock"
    
    def _get_schema_sql(self) -> str:
        return """
        CREATE TABLE IF NOT EXISTS mock_batches (
            batch_id TEXT PRIMARY KEY,
            capacity INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS mock_batch_items (
            batch_id TEXT NOT NULL,
            inventory_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            completed_count INTEGER NOT NULL DEFAULT 0,
            failure_reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (batch_id, inventory_id),
            FOREIGN KEY (batch_id) REFERENCES mock_batches(batch_id)
        );
        CREATE TABLE IF NOT EXISTS mock_assignments (
            assignment_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            inventory_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    
    def poll_otp(
        self,
        assignment: Assignment,
        *,
        after_ts: Optional[float] = None,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> Optional[str]:
        self.polled_assignments.append(assignment.assignment_id)
        return f"OTP-{assignment.inventory_id}"
    
    def create_batch(self, items: list[str], capacity: int = 1) -> str:
        """Helper to create mock batch."""
        import uuid
        batch_id = uuid.uuid4().hex
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO mock_batches(batch_id, capacity) VALUES (?, ?)",
                (batch_id, capacity)
            )
            for i, item_id in enumerate(items):
                conn.execute(
                    "INSERT INTO mock_batch_items(batch_id, inventory_id, position) "
                    "VALUES (?, ?, ?)",
                    (batch_id, item_id, i)
                )
        return batch_id


class TestPolymorphism:
    """Test polymorphic behavior across different store implementations."""
    
    def test_base_class_abstraction(self):
        """Verify base class cannot be instantiated directly."""
        with pytest.raises(TypeError):
            GmailBatchStoreBase("test.db")
    
    def test_mock_store_lifecycle(self):
        """Test complete lifecycle with mock store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MockBatchStore(Path(tmpdir) / "mock.db")
            batch_id = store.create_batch(["item-1", "item-2"], capacity=2)
            
            # Claim
            assignment = store.claim(batch_id, "job-1")
            assert assignment.inventory_id in ["item-1", "item-2"]
            assert assignment.state == "active"
            
            # Poll OTP
            otp = store.poll_otp(assignment)
            assert otp == f"OTP-{assignment.inventory_id}"
            assert assignment.assignment_id in store.polled_assignments
            
            # Complete
            assert store.complete(assignment.assignment_id)
            # Verify completion via query
            with store._transaction() as conn:
                row = conn.execute(
                    "SELECT state FROM mock_assignments WHERE assignment_id = ?",
                    (assignment.assignment_id,)
                ).fetchone()
                assert row["state"] == "completed"
    
    def test_cdk_store_implements_base(self):
        """Verify CDK store properly implements base interface."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GmailCdkBatchStore(Path(tmpdir) / "cdk.db")
            assert isinstance(store, GmailBatchStoreBase)
            assert store._table_prefix() == "gmail_cdk"
    
    def test_api_url_store_implements_base(self):
        """Verify API URL store properly implements base interface."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GmailApiUrlBatchStore(Path(tmpdir) / "api_url.db")
            assert isinstance(store, GmailBatchStoreBase)
            assert store._table_prefix() == "gmail_api_url"
    
    def test_polymorphic_assignment_handling(self):
        """Test Assignment works identically across implementations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Mock store
            mock = MockBatchStore(Path(tmpdir) / "mock.db")
            mock_batch = mock.create_batch(["mock-1"], capacity=1)
            mock_assignment = mock.claim(mock_batch, "job-m")
            
            # CDK store (use dict instead of GmailAccount to avoid import)
            cdk = GmailCdkBatchStore(Path(tmpdir) / "cdk.db")
            cdk_batch = cdk.create_batch(
                [{
                    "email": "test@gmail.com",
                    "auth_token": "auth-token",
                    "refresh_token": "refresh-token"
                }],
                capacity=1
            )
            cdk_assignment = cdk.claim(cdk_batch, "job-c")
            
            # Both return Assignment with same structure
            assert isinstance(mock_assignment, Assignment)
            assert isinstance(cdk_assignment, Assignment)
            assert hasattr(mock_assignment, "assignment_id")
            assert hasattr(cdk_assignment, "assignment_id")
            assert hasattr(mock_assignment, "inventory_id")
            assert hasattr(cdk_assignment, "inventory_id")
    
    def test_different_schemas_same_interface(self):
        """Verify different schemas work through common interface."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Mock uses simple inventory_id
            mock = MockBatchStore(Path(tmpdir) / "mock.db")
            mock_batch = mock.create_batch(["item-A", "item-B"], capacity=1)
            mock_claim = mock.claim(mock_batch, "job-1")
            assert mock_claim.inventory_id in ["item-A", "item-B"]
            
            # API URL uses composite "email----url::N"
            api_url = GmailApiUrlBatchStore(Path(tmpdir) / "api.db")
            api_batch = api_url.create_batch(
                [("test@gmail.com", "https://code.url")],
                capacity=2
            )
            api_claim = api_url.claim(api_batch, "job-2")
            assert "----" in api_claim.inventory_id
            assert "::" in api_claim.inventory_id
            
            # Both use same claim/complete/fail interface
            assert mock.complete(mock_claim.assignment_id)
            assert api_url.complete(api_claim.assignment_id)


class TestBaseClassMethods:
    """Test inherited base class methods work correctly."""
    
    def test_required_validation(self):
        """Test _required() validates non-empty values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MockBatchStore(Path(tmpdir) / "test.db")
            
            # Valid
            result = store._required("a", "b")
            assert result == ("a", "b")
            
            # Invalid
            with pytest.raises(GmailBatchError):
                store._required("", "b")
            with pytest.raises(GmailBatchError):
                store._required("a", "")
            with pytest.raises(GmailBatchError):
                store._required("", "")
    
    def test_transaction_context(self):
        """Test transaction context manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MockBatchStore(Path(tmpdir) / "test.db")
            
            # Successful transaction
            with store._transaction() as conn:
                conn.execute(
                    "INSERT INTO mock_batches(batch_id, capacity) VALUES ('test', 1)"
                )
            
            # Verify committed
            with store._transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM mock_batches WHERE batch_id = 'test'"
                ).fetchone()
                assert row is not None
            
            # Rollback on exception
            try:
                with store._transaction() as conn:
                    conn.execute(
                        "INSERT INTO mock_batches(batch_id, capacity) VALUES ('rollback', 1)"
                    )
                    raise ValueError("Test error")
            except ValueError:
                pass
            
            # Verify rolled back
            with store._transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM mock_batches WHERE batch_id = 'rollback'"
                ).fetchone()
                assert row is None
    
    def test_claim_prevents_double_assignment(self):
        """Test claim prevents assigning same item twice."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MockBatchStore(Path(tmpdir) / "test.db")
            batch_id = store.create_batch(["item-1"], capacity=1)
            
            # First claim succeeds
            assignment1 = store.claim(batch_id, "job-1")
            assert assignment1.inventory_id == "item-1"
            
            # Second claim to same job returns existing
            assignment2 = store.claim(batch_id, "job-1")
            assert assignment2.assignment_id == assignment1.assignment_id
            
            # Different job fails (no available items)
            with pytest.raises(GmailBatchConflict):
                store.claim(batch_id, "job-2")
    
    def test_complete_increments_counter(self):
        """Test complete increments completed_count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MockBatchStore(Path(tmpdir) / "test.db")
            batch_id = store.create_batch(["item-1"], capacity=3)
            
            # Claim and complete 3 times
            for i in range(3):
                assignment = store.claim(batch_id, f"job-{i}")
                store.complete(assignment.assignment_id)
            
            # Verify exhausted
            with store._transaction() as conn:
                row = conn.execute(
                    "SELECT completed_count, state FROM mock_batch_items "
                    "WHERE batch_id = ? AND inventory_id = 'item-1'",
                    (batch_id,)
                ).fetchone()
                assert row["completed_count"] == 3
                assert row["state"] == "exhausted"
    
    def test_fail_marks_assignment_failed(self):
        """Test fail() marks assignment as failed but keeps item active for retry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MockBatchStore(Path(tmpdir) / "test.db")
            batch_id = store.create_batch(["item-1"], capacity=1)
            
            assignment = store.claim(batch_id, "job-1")
            store.fail(assignment.assignment_id, "Test failure")
            
            # Verify assignment marked failed
            with store._transaction() as conn:
                row = conn.execute(
                    "SELECT state FROM mock_assignments WHERE assignment_id = ?",
                    (assignment.assignment_id,)
                ).fetchone()
                assert row["state"] == "failed"
            
            # Verify item stays active (allows retry)
            with store._transaction() as conn:
                row = conn.execute(
                    "SELECT state FROM mock_batch_items WHERE batch_id = ? AND inventory_id = 'item-1'",
                    (batch_id,)
                ).fetchone()
                assert row["state"] == "active"
    
    def test_release_allows_reclaim(self):
        """Test release() returns item to pool."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MockBatchStore(Path(tmpdir) / "test.db")
            batch_id = store.create_batch(["item-1"], capacity=2)
            
            # Claim and release
            assignment1 = store.claim(batch_id, "job-1")
            assert store.release(assignment1.assignment_id, "Released for testing")
            
            # Can reclaim
            assignment2 = store.claim(batch_id, "job-2")
            assert assignment2.inventory_id == "item-1"
            assert assignment2.assignment_id != assignment1.assignment_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
