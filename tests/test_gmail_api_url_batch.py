"""
Test Gmail API URL batch assignment system.
"""
import pytest
from unittest.mock import patch
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore, GmailApiUrlBatchConflict
from core.gmail_api_url_client import GmailApiUrlAccount


def test_create_batch_with_capacity(tmp_path):
    """Test batch creation with multi-alias capacity."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    
    # Create batch with 3 aliases per email
    batch_id = store.create_batch(
        [
            ("test1@gmail.com", "https://api.mail.com/code1"),
            ("test2@gmail.com", "https://api.mail.com/code2"),
        ],
        capacity=3
    )
    
    assert batch_id is not None


def test_claim_respects_capacity(tmp_path):
    """Test that only 1 active assignment per API mailbox (code_url) is allowed.

    Gmail API URL constraint: 1 code_url = 1 inbox = 1 OTP source at a time.
    Even if capacity=2, two jobs cannot hold the same code_url simultaneously.
    They must be sequential: claim → complete → next claim.
    """
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")

    batch_id = store.create_batch(
        [("test@gmail.com", "https://api.mail.com/code")],
        capacity=2
    )

    # First claim succeeds
    assignment1 = store.claim(batch_id, "job-1")
    assert assignment1 is not None

    # Second claim blocked while first is active (same code_url = same inbox)
    with pytest.raises(GmailApiUrlBatchConflict):
        store.claim(batch_id, "job-2")

    # After completing first, second can proceed
    store.complete(assignment1.assignment_id)
    assignment2 = store.claim(batch_id, "job-2")
    assert assignment2 is not None


def test_complete_frees_alias_for_reuse(tmp_path):
    """Test that completing an assignment releases the code_url lock for the next job."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")

    batch_id = store.create_batch(
        [("test@gmail.com", "https://api.mail.com/code")],
        capacity=2
    )

    # Claim and complete first job
    assignment1 = store.claim(batch_id, "job-1")
    store.complete(assignment1.assignment_id)

    # Second job can now claim (code_url no longer locked)
    assignment2 = store.claim(batch_id, "job-2")
    assert assignment2 is not None
    store.complete(assignment2.assignment_id)

    # Third job claims the last available slot
    assignment3 = store.claim(batch_id, "job-3")
    assert assignment3 is not None


def test_fail_allows_retry(tmp_path):
    """Test that failing an assignment allows retry."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    
    batch_id = store.create_batch(
        [("test@gmail.com", "https://api.mail.com/code")],
        capacity=1
    )
    
    # Claim and fail
    assignment = store.claim(batch_id, "job-1")
    store.fail(assignment.assignment_id, "Test failure")
    
    # Should be able to claim again
    assignment2 = store.claim(batch_id, "job-2")
    assert assignment2 is not None


def test_discard_exhausts_failed_alias_and_claims_next_alias(tmp_path):
    """A failed alias is retired instead of being handed to the next job."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    batch_id = store.create_batch_multi(
        [
            {
                "source_email": "source@gmail.com",
                "code_url": "https://api.mail.com/code",
                "aliases": ["failed@gmail.com", "next@gmail.com"],
            }
        ]
    )

    first = store.claim_waiting(batch_id, "job-1")
    assert first is not None
    assert store.discard(first.assignment_id, reason="OTP missing")

    next_assignment = store.claim_waiting(batch_id, "job-2")
    assert next_assignment is not None
    assert next_assignment.inventory_id.startswith("next@gmail.com----")
    status = store.batch_status(batch_id)
    assert status["pending"] == 1
    assert status["exhausted"] == 1


def test_has_pending_items_distinguishes_temporary_lock_from_exhaustion(tmp_path):
    """Pending aliases remain visible while their shared code URL is locked."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    batch_id = store.create_batch_multi(
        [
            {
                "source_email": "source@gmail.com",
                "code_url": "https://api.mail.com/code",
                "aliases": ["first@gmail.com", "second@gmail.com"],
            }
        ]
    )

    first = store.claim(batch_id, "job-1")

    assert store.has_pending_items(batch_id)

    store.complete(first.assignment_id)
    second = store.claim(batch_id, "job-2")
    store.complete(second.assignment_id)

    assert not store.has_pending_items(batch_id)


def test_claim_waiting_persists_fifo_and_counts_queued_jobs(tmp_path):
    """Queued jobs survive a conflict and are assigned in request order."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    batch_id = store.create_batch_multi(
        [
            {
                "source_email": "source@gmail.com",
                "code_url": "https://api.mail.com/code",
                "aliases": ["first@gmail.com", "second@gmail.com"],
            }
        ]
    )

    first = store.claim_waiting(batch_id, "job-1")
    assert first is not None
    assert store.claim_waiting(batch_id, "job-2") is None
    assert store.batch_status(batch_id)["waiting_jobs"] == 1
    assert store.batch_status(batch_id)["available_code_urls"] == 0

    store.complete(first.assignment_id)
    second = store.claim_waiting(batch_id, "job-2")
    assert second is not None
    assert store.batch_status(batch_id)["waiting_jobs"] == 0


def test_batch_status_marks_only_consumed_aliases_exhausted(tmp_path):
    """A temporary code URL lock is pending work, not batch exhaustion."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    batch_id = store.create_batch_multi(
        [
            {
                "source_email": "source@gmail.com",
                "code_url": "https://api.mail.com/code",
                "aliases": ["first@gmail.com", "second@gmail.com"],
            }
        ]
    )

    first = store.claim_waiting(batch_id, "job-1")
    locked = store.batch_status(batch_id)
    assert locked["pending"] == 2
    assert locked["completed"] == 0
    assert locked["exhausted_batch"] is False

    store.complete(first.assignment_id)
    remaining = store.batch_status(batch_id)
    assert remaining["pending"] == 1
    assert remaining["completed"] == 1
    assert remaining["exhausted_batch"] is False


def test_batch_status_counts_legacy_capacity_slots(tmp_path):
    """Legacy capacity batches count remaining slots, not only item rows."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    batch_id = store.create_batch(
        [("source@gmail.com", "https://api.mail.com/code")], capacity=3
    )

    status = store.batch_status(batch_id)

    assert status["total"] == 9
    assert status["pending"] == 9


def test_three_sources_with_twelve_aliases_queue_all_jobs_without_conflict(tmp_path):
    """Regression: 36 aliases use three concurrent URL slots and queue the rest."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    groups = [
        {
            "source_email": f"source-{index}@gmail.com",
            "code_url": f"https://api.mail.com/code-{index}",
            "aliases": [f"alias-{index}-{slot}@gmail.com" for slot in range(12)],
        }
        for index in range(3)
    ]
    batch_id = store.create_batch_multi(groups)

    active = [store.claim_waiting(batch_id, f"job-{index}") for index in range(3)]
    assert all(active)
    queued = [
        store.claim_waiting(batch_id, f"job-{index}")
        for index in range(3, 36)
    ]
    assert all(assignment is None for assignment in queued)
    assert store.batch_status(batch_id)["waiting_jobs"] == 33

    next_job = 3
    for assignment in active:
        store.complete(assignment.assignment_id)
        replacement = store.claim_waiting(batch_id, f"job-{next_job}")
        assert replacement is not None
        store.complete(replacement.assignment_id)
        next_job += 1

    while next_job < 36:
        replacement = store.claim_waiting(batch_id, f"job-{next_job}")
        assert replacement is not None
        store.complete(replacement.assignment_id)
        next_job += 1

    status = store.batch_status(batch_id)
    assert status["total"] == 36
    assert status["completed"] == 36
    assert status["pending"] == 0
    assert status["waiting_jobs"] == 0
    assert status["exhausted_batch"] is True


def test_idempotent_claim(tmp_path):
    """Test that claiming with same job_id is idempotent."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    
    batch_id = store.create_batch(
        [("test@gmail.com", "https://api.mail.com/code")],
        capacity=1
    )
    
    # Claim twice with same job_id
    first = store.claim(batch_id, "job-x")
    second = store.claim(batch_id, "job-x")
    
    assert first.assignment_id == second.assignment_id


def test_multiple_emails_round_robin(tmp_path):
    """Test round-robin assignment across multiple emails."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    
    batch_id = store.create_batch(
        [
            ("email1@gmail.com", "https://api.mail.com/code1"),
            ("email2@gmail.com", "https://api.mail.com/code2"),
            ("email3@gmail.com", "https://api.mail.com/code3"),
        ],
        capacity=1
    )
    
    # Claim 3 times should get 3 different emails
    assignments = [store.claim(batch_id, f"job-{i}") for i in range(3)]
    inventory_ids = [a.inventory_id for a in assignments]
    
    # All should be different
    assert len(set(inventory_ids)) == 3


def test_capacity_validation(tmp_path):
    """Test that capacity must be between 1 and 12."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    
    # Valid capacities should work
    store.create_batch([("test@gmail.com", "https://api.mail.com/code")], capacity=1)
    store.create_batch([("test@gmail.com", "https://api.mail.com/code")], capacity=12)
    
    # Invalid capacities should fail
    with pytest.raises(Exception):
        store.create_batch([("test@gmail.com", "https://api.mail.com/code")], capacity=0)
    
    with pytest.raises(Exception):
        store.create_batch([("test@gmail.com", "https://api.mail.com/code")], capacity=13)


# ── Regression tests: Bug 1-3 (HTTP 500 on Gmail API URL submit) ────────────

def test_create_batch_multi_creates_alias_items(tmp_path):
    """Regression: create_batch_multi must exist and store alias----code_url inventory_ids.
    
    Gmail API URL constraint: only 1 active assignment per code_url at a time.
    Two aliases from the same code_url must be claimed sequentially.
    """
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    groups = [
        {
            "source_email": "source@gmail.com",
            "code_url": "https://api.mail.com/code1",
            "aliases": ["alias1@gmail.com", "alias1@googlemail.com"],
        }
    ]
    # Bug 1: AttributeError 'GmailApiUrlBatchStore' object has no attribute 'create_batch_multi'
    batch_id = store.create_batch_multi(groups)
    assert batch_id is not None

    # Only 1 active per code_url: claim, verify, complete, then claim second
    a1 = store.claim(batch_id, "job-1")
    alias1, code_url1 = a1.inventory_id.split("----", 1)
    assert alias1 in ("alias1@gmail.com", "alias1@googlemail.com")
    assert code_url1 == "https://api.mail.com/code1"

    # Second claim blocked while first is still active (same inbox)
    with pytest.raises(GmailApiUrlBatchConflict):
        store.claim(batch_id, "job-2")

    # Complete first → second can proceed
    store.complete(a1.assignment_id)
    a2 = store.claim(batch_id, "job-2")
    alias2, code_url2 = a2.inventory_id.split("----", 1)
    assert alias2 in ("alias1@gmail.com", "alias1@googlemail.com")
    assert alias2 != alias1  # different alias slot
    assert code_url2 == "https://api.mail.com/code1"

    # Pool fully exhausted after both used
    store.complete(a2.assignment_id)
    with pytest.raises(GmailApiUrlBatchConflict):
        store.claim(batch_id, "job-3")


def test_create_batch_multi_multi_group(tmp_path):
    """create_batch_multi handles multiple source emails each with distinct aliases.
    
    Gmail API URL constraint: 1 active assignment per code_url at a time.
    - s1 has 1 alias (c1) → 1 concurrent slot
    - s2 has 2 aliases (c2) → 1 concurrent slot (must be sequential)
    Total concurrent = 2 (one per distinct code_url)
    """
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    groups = [
        {"source_email": "s1@gmail.com", "code_url": "https://api.test/c1",
         "aliases": ["a1@gmail.com"]},
        {"source_email": "s2@gmail.com", "code_url": "https://api.test/c2",
         "aliases": ["a2@gmail.com", "a2@googlemail.com"]},
    ]
    batch_id = store.create_batch_multi(groups)

    # Can claim 2 simultaneously (one per distinct code_url)
    j0 = store.claim(batch_id, "j-0")
    j1 = store.claim(batch_id, "j-1")
    aliases_concurrent = {j0.inventory_id.split("----", 1)[0], j1.inventory_id.split("----", 1)[0]}
    # Each from a different code_url
    assert "a1@gmail.com" in aliases_concurrent or j0.inventory_id.startswith("a1@")
    assert len(aliases_concurrent) == 2

    # 3rd claim blocked (both code_urls occupied)
    with pytest.raises(GmailApiUrlBatchConflict):
        store.claim(batch_id, "j-2")

    # Complete both → can claim remaining a2@googlemail.com
    store.complete(j0.assignment_id)
    store.complete(j1.assignment_id)

    # Now c1 is exhausted (only 1 alias), c2 still has 1 slot left
    j2 = store.claim(batch_id, "j-2")
    remaining = j2.inventory_id.split("----", 1)[0]
    assert remaining in ("a2@gmail.com", "a2@googlemail.com")
    all_aliases = aliases_concurrent | {remaining}
    assert "a2@gmail.com" in all_aliases
    assert "a2@googlemail.com" in all_aliases


def test_find_item_by_alias_returns_alias_and_code_url(tmp_path):
    """Regression: find_item_by_alias must exist and parse inventory_id correctly."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    groups = [
        {"source_email": "s@gmail.com", "code_url": "https://api.test/c",
         "aliases": ["found@gmail.com"]},
    ]
    store.create_batch_multi(groups)

    # Bug 3 (store side): get_account_context / find_item_by_alias missing
    result = store.find_item_by_alias("found@gmail.com")
    assert result is not None
    alias, code_url = result
    assert alias == "found@gmail.com"
    assert code_url == "https://api.test/c"

    assert store.find_item_by_alias("nonexistent@gmail.com") is None


def test_poll_otp_uses_gmail_api_url_client(tmp_path):
    """The batch adapter must delegate to the client's public polling API."""
    store = GmailApiUrlBatchStore(tmp_path / "batch.db")
    batch_id = store.create_batch_multi([
        {"source_email": "source@gmail.com", "code_url": "https://api.test/c",
         "aliases": ["alias@gmail.com"]},
    ])
    assignment = store.claim(batch_id, "job-1")

    with patch(
        "core.gmail_api_url_client.poll_verification_code",
        return_value="123456",
    ) as poll:
        result = store.poll_otp(assignment, after_ts=10.0, timeout=17.0, poll_interval=4.0)

    assert result == "123456"
    account = poll.call_args.args[0]
    assert account == GmailApiUrlAccount(
        email="alias@gmail.com", code_url="https://api.test/c"
    )
    assert poll.call_args.kwargs == {
        "after_ts": 10.0,
        "max_wait": 17.0,
        "poll_interval": 4.0,
    }
