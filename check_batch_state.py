#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Check current batch database state."""
import sqlite3
import sys

from core.app_state_db import APP_STATE_DB_PATH

db_path = APP_STATE_DB_PATH

if not db_path.exists():
    print("❌ Batch database not found!")
    sys.exit(1)

conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

print("=" * 70)
print("Gmail API URL Batch Database State")
print("=" * 70)

# 1. Batches summary
batches = conn.execute('SELECT * FROM gmail_api_url_batches ORDER BY created_at DESC LIMIT 5').fetchall()
print(f"\n📦 Recent Batches: {len(batches)}")
for b in batches:
    print(f"  {b['batch_id'][:16]}... capacity={b['capacity']} created={b['created_at']}")

# 2. Batch items by state
print("\n📋 Batch Items by State:")
items = conn.execute('SELECT state, COUNT(*) as cnt FROM gmail_api_url_batch_items GROUP BY state').fetchall()
for item in items:
    print(f"  {item['state']:10} {item['cnt']:5}")

# 3. Assignments by state
print("\n🔒 Assignments by State:")
assignments = conn.execute('SELECT state, COUNT(*) as cnt FROM gmail_api_url_assignments GROUP BY state').fetchall()
for a in assignments:
    print(f"  {a['state']:10} {a['cnt']:5}")

# 4. Active assignments detail
active = conn.execute('''
    SELECT a.assignment_id, a.job_id, a.created_at, 
           i.email, i.code_url, i.completed_count, b.capacity
    FROM gmail_api_url_assignments a
    JOIN gmail_api_url_batch_items i ON a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id
    JOIN gmail_api_url_batches b ON a.batch_id = b.batch_id
    WHERE a.state = 'active'
    ORDER BY a.created_at DESC
''').fetchall()

if active:
    print(f"\n⚠️  ACTIVE ASSIGNMENTS ({len(active)}) - HOLDING API LOCKS:")
    for a in active:
        email = a['email']
        code_url = a['code_url'][:60] + '...' if len(a['code_url']) > 60 else a['code_url']
        print(f"\n  Job: {a['job_id']}")
        print(f"    Email: {email}")
        print(f"    Code URL: {code_url}")
        print(f"    Created: {a['created_at']}")
        print(f"    Usage: {a['completed_count']}/{a['capacity']}")
    
    print("\n💡 If these jobs are stuck/failed, they block new jobs from using same API!")
    print("   Solution: Complete or release these assignments.")
else:
    print("\n✅ No active assignments - all APIs are available")

# 5. Check for stuck items (active items with no active assignment)
stuck = conn.execute('''
    SELECT i.email, i.code_url, i.completed_count, b.capacity
    FROM gmail_api_url_batch_items i
    JOIN gmail_api_url_batches b ON i.batch_id = b.batch_id
    WHERE i.state = 'active'
    AND i.completed_count < b.capacity
    AND NOT EXISTS (
        SELECT 1 FROM gmail_api_url_assignments a
        WHERE a.batch_id = i.batch_id AND a.inventory_id = i.inventory_id AND a.state = 'active'
    )
    LIMIT 10
''').fetchall()

if stuck:
    print(f"\n✅ Available items (no active lock): {len(stuck)}")
    for s in stuck[:3]:
        print(f"  {s['email']} - usage {s['completed_count']}/{s['capacity']}")

# 6. Check completed count vs capacity
exhausted = conn.execute('''
    SELECT COUNT(*) as cnt FROM gmail_api_url_batch_items i
    JOIN gmail_api_url_batches b ON i.batch_id = b.batch_id
    WHERE i.completed_count >= b.capacity
''').fetchone()

print(f"\n📊 Statistics:")
print(f"  Exhausted items (reached capacity): {exhausted['cnt']}")

conn.close()
print("\n" + "=" * 70)
