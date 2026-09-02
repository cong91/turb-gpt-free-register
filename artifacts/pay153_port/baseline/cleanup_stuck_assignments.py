#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Clean up stuck active assignments in batch database."""
import sqlite3
import sys
from datetime import datetime, timedelta

from core.app_state_db import APP_STATE_DB_PATH

db_path = APP_STATE_DB_PATH

if not db_path.exists():
    print("❌ Database not found!")
    sys.exit(1)

conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

print("=" * 70)
print("Cleanup Stuck Active Assignments")
print("=" * 70)

# Find stuck active assignments (older than 10 minutes)
cutoff = (datetime.now() - timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')

stuck = conn.execute('''
    SELECT assignment_id, job_id, created_at
    FROM gmail_api_url_assignments
    WHERE state = 'active'
    AND created_at < ?
    ORDER BY created_at
''', (cutoff,)).fetchall()

print(f"\n📋 Found {len(stuck)} stuck active assignments (older than 10 min)")

if not stuck:
    print("✅ No stuck assignments to clean up!")
    conn.close()
    sys.exit(0)

print("\nSample stuck jobs:")
for s in stuck[:10]:
    print(f"  Job {s['job_id']:15} - created {s['created_at']}")

print(f"\n⚠️  About to release {len(stuck)} assignments...")
confirm = input("Type 'yes' to proceed: ")

if confirm.lower() != 'yes':
    print("❌ Aborted")
    conn.close()
    sys.exit(1)

# Release all stuck assignments
conn.execute('''
    UPDATE gmail_api_url_assignments
    SET state = 'released',
        updated_at = CURRENT_TIMESTAMP
    WHERE state = 'active'
    AND created_at < ?
''', (cutoff,))

conn.commit()

released_count = conn.total_changes
print(f"\n✅ Released {released_count} stuck assignments")

# Verify
remaining = conn.execute('SELECT COUNT(*) as cnt FROM gmail_api_url_assignments WHERE state = "active"').fetchone()
print(f"✅ Remaining active assignments: {remaining['cnt']}")

conn.close()
print("\n" + "=" * 70)
print("✅ Cleanup complete! New jobs can now claim these APIs.")
print("=" * 70)
