#!/usr/bin/env python
"""Force cleanup ALL active assignments."""
import sqlite3

from core.app_state_db import APP_STATE_DB_PATH

db_path = APP_STATE_DB_PATH
conn = sqlite3.connect(str(db_path))

print("⚠️  Releasing ALL active assignments...")

# Count before
before = conn.execute('SELECT COUNT(*) as cnt FROM gmail_api_url_assignments WHERE state = "active"').fetchone()
print(f"Before: {before[0]} active assignments")

# Release all
conn.execute('''
    UPDATE gmail_api_url_assignments
    SET state = 'released',
        updated_at = CURRENT_TIMESTAMP
    WHERE state = 'active'
''')
conn.commit()

# Count after
after = conn.execute('SELECT COUNT(*) as cnt FROM gmail_api_url_assignments WHERE state = "active"').fetchone()
print(f"After: {after[0]} active assignments")

released = before[0] - after[0]
print(f"\n✅ Released {released} assignments")
print("✅ All APIs are now available for new jobs!")

conn.close()
