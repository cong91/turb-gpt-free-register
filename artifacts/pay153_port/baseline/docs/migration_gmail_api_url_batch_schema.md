# Migration: Gmail API URL Multi-Alias Batch Schema

> Runtime note: the batch tables now live in the canonical root
> `turb.sqlite3`. The historical `data/gmail_api_url_batches.db` path below is
> retained only as legacy reference material; do not create a second runtime
> database from this document.

## Database Changes

### assignments table: code_url column

**Change**: Add `code_url` column to `assignments` table to support per-alias code URL in multi-source batches.

**Before**:
```sql
CREATE TABLE assignments (
    assignment_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    job_id TEXT,
    status TEXT NOT NULL,
    claimed_at REAL,
    updated_at REAL NOT NULL,
    UNIQUE(batch_id, alias)
)
```

**After**:
```sql
CREATE TABLE assignments (
    assignment_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    code_url TEXT NOT NULL,      -- NEW: per-alias code URL
    job_id TEXT,
    status TEXT NOT NULL,
    claimed_at REAL,
    updated_at REAL NOT NULL,
    UNIQUE(batch_id, alias)
)
```

## Migration Script

Run in Python shell or add to migration runner:

```python
import sqlite3
from pathlib import Path

db_path = Path("data/gmail_api_url_batches.db")
conn = sqlite3.connect(db_path)

# Check if code_url column exists
cursor = conn.execute("PRAGMA table_info(assignments)")
columns = [row[1] for row in cursor.fetchall()]

if "code_url" not in columns:
    print("Adding code_url column to assignments...")
    
    # Create new table with code_url
    conn.execute("""
        CREATE TABLE assignments_new (
            assignment_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            alias TEXT NOT NULL,
            code_url TEXT NOT NULL DEFAULT '',
            job_id TEXT,
            status TEXT NOT NULL,
            claimed_at REAL,
            updated_at REAL NOT NULL,
            UNIQUE(batch_id, alias)
        )
    """)
    
    # Copy data: populate code_url from batches.code_url via batch_id join
    conn.execute("""
        INSERT INTO assignments_new
        SELECT 
            a.assignment_id,
            a.batch_id,
            a.alias,
            b.code_url,
            a.job_id,
            a.status,
            a.claimed_at,
            a.updated_at
        FROM assignments a
        JOIN batches b ON a.batch_id = b.batch_id
    """)
    
    # Replace old table
    conn.execute("DROP TABLE assignments")
    conn.execute("ALTER TABLE assignments_new RENAME TO assignments")
    
    conn.commit()
    print("Migration complete.")
else:
    print("code_url column already exists, skipping.")

conn.close()
```

## Automatic Migration

The migration runs automatically on first access when `GmailApiUrlBatchStore` detects missing `code_url` column:

```python
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore

# First call after upgrade will trigger auto-migration
store = GmailApiUrlBatchStore()  # Migrates if needed
```

## Backward Compatibility

- **Old batches**: Existing assignments inherit `code_url` from `batches.code_url` (single-source batch)
- **New batches**: Multi-source batches write per-alias `code_url` directly
- **No downtime**: Schema is backward-compatible; old code reads `batches.code_url`, new code reads `assignments.code_url`

## Rollback

To rollback (not recommended after multi-source batches are created):

```python
conn.execute("ALTER TABLE assignments DROP COLUMN code_url")
conn.commit()
```

**Warning**: Rolling back after creating multi-source batches will lose per-alias code URL mapping. Only rollback if no multi-source batches have been created.
