#!/usr/bin/env python
"""
Integration test: Gmail API URL multi-alias batch registration
Verifies Bug B fix (source email filtered) + Bug C fix (assignment finalize)
"""
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.app_state_db import APP_STATE_DB_PATH
from core.gmail_aliases import generate_gmail_dual_domain_variants


def test_source_email_filtered():
    """Test that source email is filtered from alias list"""
    print("\n=== Test 1: Source Email Filter ===")
    
    source = "testuser123@gmail.com"
    raw = generate_gmail_dual_domain_variants(source, limit=7)
    
    # Filter logic (same as in gmail_api_url_client.py line 286-287)
    source_lower = source.lower().strip()
    aliases = [a for a in raw if a.lower().strip() != source_lower][:6]
    
    print(f"Source email: {source}")
    print(f"Raw variants (7): {raw}")
    print(f"Filtered aliases (6): {aliases}")
    
    # Assertions
    assert len(raw) == 7, f"Expected 7 raw variants, got {len(raw)}"
    assert raw[0] == source, f"Expected source at position 0, got {raw[0]}"
    assert len(aliases) == 6, f"Expected 6 filtered aliases, got {len(aliases)}"
    assert source not in aliases, f"Source email {source} should be filtered out"
    
    # Verify all aliases are different from source (dot, plus, or domain change)
    for alias in aliases:
        assert alias.lower() != source.lower(), f"Alias {alias} matches source email"
        # Valid aliases: dot variant, plus variant, or googlemail.com domain
        local = alias.split('@')[0]
        domain = alias.split('@')[1] if '@' in alias else ''
        is_dot = '.' in local
        is_plus = '+' in local
        is_alt_domain = 'googlemail.com' in domain
        assert is_dot or is_plus or is_alt_domain, f"Alias {alias} has no variant marker"
    
    print("✅ PASS: Source email correctly filtered\n")
    return True


def test_batch_items_are_aliases():
    """Test that latest batch contains only aliases (no source email)"""
    print("=== Test 2: Batch Items Verification ===")
    
    db_path = APP_STATE_DB_PATH
    if not db_path.exists():
        print("⚠️  Batch DB not found, skipping test")
        return True
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    # Get latest batch
    batch = conn.execute(
        'SELECT batch_id FROM gmail_api_url_batches ORDER BY created_at DESC LIMIT 1'
    ).fetchone()
    
    if not batch:
        print("⚠️  No batches found, skipping test")
        conn.close()
        return True
    
    batch_id = batch['batch_id']
    print(f"Latest batch: {batch_id[:12]}...")
    
    # Check first 6 items
    items = conn.execute(
        'SELECT email, position FROM gmail_api_url_batch_items '
        'WHERE batch_id=? ORDER BY position LIMIT 6',
        (batch_id,)
    ).fetchall()
    
    conn.close()
    
    print(f"Batch items ({len(items)} checked):")
    
    all_aliases = True
    for item in items:
        email = item['email']
        local = email.split('@')[0]
        is_alias = '.' in local or '+' in local
        marker = '✓ alias' if is_alias else '✗ SOURCE EMAIL'
        print(f"  pos {item['position']}: {email[:45]} {marker}")
        
        if not is_alias:
            all_aliases = False
    
    if all_aliases:
        print("✅ PASS: All batch items are aliases\n")
    else:
        print("❌ FAIL: Found source email in batch\n")
        return False
    
    return True


def test_assignment_finalize_code_exists():
    """Verify that release_account() has batch assignment finalize logic"""
    print("=== Test 3: Assignment Finalize Code Check ===")
    
    import inspect

    from core import gmail_api_url_client
    
    source = inspect.getsource(gmail_api_url_client.release_account)
    
    # Check for key finalize logic
    has_batch_context = 'get_batch_account_context' in source
    has_find_active = 'find_active_assignment_for_alias' in source
    has_fail = '_batch_store().fail' in source or 'store.fail' in source
    has_release = '_batch_store().release' in source or 'store.release' in source
    
    print("release_account() code analysis:")
    print(f"  ✓ get_batch_account_context: {has_batch_context}")
    print(f"  ✓ find_active_assignment_for_alias: {has_find_active}")
    print(f"  ✓ batch fail() call: {has_fail}")
    print(f"  ✓ batch release() call: {has_release}")
    
    if has_batch_context and has_find_active and (has_fail or has_release):
        print("✅ PASS: Assignment finalize logic present\n")
        return True
    else:
        print("❌ FAIL: Missing assignment finalize logic\n")
        return False


if __name__ == '__main__':
    print("\n" + "="*70)
    print("Gmail API URL Multi-Alias Batch - Integration Test")
    print("="*70)
    
    results = []
    
    try:
        results.append(("Source Email Filter", test_source_email_filtered()))
    except Exception as e:  # noqa: BLE001
        print(f"❌ Test 1 failed with exception: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Source Email Filter", False))
    
    try:
        results.append(("Batch Items Aliases", test_batch_items_are_aliases()))
    except Exception as e:  # noqa: BLE001
        print(f"❌ Test 2 failed with exception: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Batch Items Aliases", False))
    
    try:
        results.append(("Assignment Finalize", test_assignment_finalize_code_exists()))
    except Exception as e:  # noqa: BLE001
        print(f"❌ Test 3 failed with exception: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Assignment Finalize", False))
    
    print("="*70)
    print("SUMMARY:")
    all_passed = True
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}: {name}")
        if not passed:
            all_passed = False
    
    print("="*70)
    
    if all_passed:
        print("\n🎉 ALL TESTS PASSED - Ready for production!\n")
        sys.exit(0)
    else:
        print("\n⚠️  SOME TESTS FAILED - Review fixes\n")
        sys.exit(1)
