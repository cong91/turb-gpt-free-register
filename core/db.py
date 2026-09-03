"""
SQLite 持久化层。

运行时数据全部存储在根目录 `turb.sqlite3`；旧 JSON/TXT/Codex 文件不再作为
运行时输入。临时测试路径仍保留旧文件布局以隔离 fixture。
"""
import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from core import app_state_db
from core.gmail_aliases import (
    MAX_GMAIL_DUAL_DOMAIN_VARIANTS,
    GmailAliasError,
    generate_gmail_dual_domain_aliases,
)
from core.gmail_api_url_batch_store import (
    GmailApiUrlBatchConflict,
    GmailApiUrlBatchStore,
)
from core.openai_auth import account_unusable_message

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT
_LEGACY_DATA_DIR = _PROJECT_ROOT / "data"
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_PLAN_CHECK_STALE_SECONDS = 120
_PLAN_CHECK_QUEUE_STALE_SECONDS = 1800

_OUTLOOK_JSON = _PROJECT_ROOT / "用于注册的邮箱.json"
_OUTLOOK_TXT = _PROJECT_ROOT / "用于注册的邮箱.txt"
_GENERIC_API_EMAIL_JSON = _PROJECT_ROOT / "用于注册的API邮箱.json"
_GENERIC_API_EMAIL_TXT = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_GMAIL_API_URL_EMAIL_JSON = _PROJECT_ROOT / "用于注册的Gmail API邮箱.json"
_GMAIL_API_URL_EMAIL_TXT = _PROJECT_ROOT / "用于注册的Gmail API邮箱.txt"
_ACCOUNTS_JSON = _PROJECT_ROOT / "注册成功的邮箱.json"
_ACCOUNTS_TXT = _PROJECT_ROOT / "注册成功的邮箱.txt"
_TOKENS_TXT = _PROJECT_ROOT / "注册成功的token.txt"
_JOBS_JSON = _PROJECT_ROOT / "注册任务.json"
# 兼容旧测试/外部调用方；静态查看器已停用，不会再写入此路径。
_VIEWER_HTML = _PROJECT_ROOT / "accounts_viewer.html"
_CODEX_DIR = _PROJECT_ROOT / "codex_accounts"
_CODEX_AGENT_DIR = _PROJECT_ROOT / "codex_agent_accounts"
# 仅供一次性迁移旧导出状态，运行期间不再读取该文件。
_LEGACY_CODEX_EXPORT_STATE = _PROJECT_ROOT / "codex_导出状态.json"
# SQLite 是运行时唯一业务数据主存储；fork-added provider state 也写入这里。
_SQLITE_PATH = _PROJECT_ROOT / "turb.sqlite3"
_SQLITE_LOCK = threading.RLock()
_SQLITE_READY = False
_VIEWER_DEBOUNCE_SECONDS = 3.0
_VIEWER_REFRESH_LOCK = threading.Lock()
_VIEWER_REFRESH_TIMER: threading.Timer | None = None
_VIEWER_REFRESH_REASON = ""
_REQUIRED_SQLITE_TABLES = frozenset({
    "accounts",
    "email_pool",
    "registration_jobs",
    "codex_accounts",
    "codex_agent_accounts",
    "storage_meta",
})
_TABLES = {
    "accounts": "accounts",
    "outlook": "email_pool",
    "generic_api": "email_pool",
    "jobs": "registration_jobs",
    "domain": "email_pool",
    "codex": "codex_accounts",
}
_EMAIL_SOURCES = {"outlook": "outlook", "generic_api": "generic_api", "domain": "cloudflare_domain"}
_LEGACY_TABLES = {"outlook": "outlook_pool", "generic_api": "generic_api_pool", "domain": "domain_email_pool"}
_CODEX_EXPORT_STATE = _LEGACY_CODEX_EXPORT_STATE
_PERSONAL_INFO_CHANGE_STATE_KEY = "personal_info_change_batches"
_MAX_PERSONAL_INFO_CHANGE_BATCHES = 32
_REGISTRATION_JOB_SEQUENCE_KEY = "registration_job_next_id"

_LEGACY_SQLITE = _LEGACY_DATA_DIR / "registrations.db"
_LEGACY_OUTLOOK_JSON = _LEGACY_DATA_DIR / "outlook_accounts.json"
_LEGACY_ACCOUNTS_JSON = _LEGACY_DATA_DIR / "registered_accounts.json"
_LEGACY_JOBS_JSON = _LEGACY_DATA_DIR / "registration_jobs.json"
_LOCK = threading.RLock()
_DEFAULT_SQLITE_PATH = _SQLITE_PATH
_DEFAULT_ACCOUNTS_JSON = _ACCOUNTS_JSON
_DEFAULT_OUTLOOK_JSON = _OUTLOOK_JSON
_DEFAULT_JOBS_JSON = _JOBS_JSON
_DEFAULT_VIEWER_HTML = _VIEWER_HTML
_SQLITE_READY_PATH: Path | None = None


def _now() -> str:
    return datetime.now(tz=timezone.utc).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def _local_now() -> datetime:
    """Return the local wall-clock time as a naive value for legacy fields."""
    return datetime.now(tz=timezone.utc).astimezone().replace(tzinfo=None)


def _local_fromtimestamp(value: float) -> datetime:
    """Convert a filesystem timestamp to the local naive representation."""
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().replace(tzinfo=None)


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _sqlite_conn() -> sqlite3.Connection:
    """创建短生命周期中央连接，使用 provider stores 兼容的 rollback journal。"""
    _ensure_storage()
    return app_state_db.connect(_active_sqlite_path(), busy_timeout_ms=30000)


def _active_sqlite_path() -> Path:
    """测试替换旧 JSON 路径时使用同目录数据库，避免污染正式库。"""
    for configured_path, default_path in (
        (_ACCOUNTS_JSON, _DEFAULT_ACCOUNTS_JSON),
        (_OUTLOOK_JSON, _DEFAULT_OUTLOOK_JSON),
        (_JOBS_JSON, _DEFAULT_JOBS_JSON),
    ):
        if configured_path != default_path:
            return configured_path.parent / "turb.sqlite3"
    return _DEFAULT_SQLITE_PATH


def _sqlite_schema_ready(path: Path) -> bool:
    """Validate the core tables before trusting the process-local ready flag."""
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(str(path), timeout=30)) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        return _REQUIRED_SQLITE_TABLES.issubset(tables)
    except sqlite3.Error:
        return False


def _read_legacy_sqlite_collection(collection: str) -> list[dict] | None:
    """读取旧 data/registrations.db 的数据，仅在一次性迁移阶段调用。"""
    if not _LEGACY_SQLITE.exists():
        return None
    try:
        with closing(sqlite3.connect(str(_LEGACY_SQLITE))) as legacy_conn:
            legacy_conn.row_factory = sqlite3.Row
            table = "registered_accounts" if collection == "accounts" else "outlook_pool" if collection == "outlook" else ""
            if not table or not _table_exists(legacy_conn, table):
                return None
            return [dict(row) for row in legacy_conn.execute(f"SELECT * FROM {table}").fetchall()]
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        logger.debug("读取旧 SQLite 集合失败: %s", exc, exc_info=True)
        return None


def _ensure_sqlite() -> None:
    """Ensure core tables exist without importing central exports after cutover."""
    global _SQLITE_READY, _SQLITE_READY_PATH
    active_path = _active_sqlite_path()
    if _SQLITE_READY and _SQLITE_READY_PATH == active_path and _sqlite_schema_ready(active_path):
        return
    with _SQLITE_LOCK:
        active_path = _active_sqlite_path()
        if _SQLITE_READY and _SQLITE_READY_PATH == active_path and _sqlite_schema_ready(active_path):
            return
        conn = _sqlite_conn()
        canonical_path = app_state_db.is_app_state_path(active_path)
        app_state_db.ensure_schema(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                PRIMARY KEY (id)
            );
            CREATE TABLE IF NOT EXISTS email_pool (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registration_jobs AS SELECT * FROM accounts WHERE 0;
            CREATE TABLE IF NOT EXISTS codex_accounts (
                id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL UNIQUE, email TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS codex_agent_accounts (
                account_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS storage_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        for table in ("accounts", "email_pool", "registration_jobs"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_status ON {table}(status, id DESC)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_archived ON {table}(archived, id DESC)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_email ON {table}(email COLLATE NOCASE)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_created ON {table}(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_pool_source_status ON email_pool(source, status, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_archived ON codex_accounts(archived, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_email ON codex_accounts(email COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_accounts_created ON codex_accounts(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_agent_accounts_email ON codex_agent_accounts(email COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_codex_agent_accounts_updated ON codex_agent_accounts(updated_at DESC)")
        migration_done = canonical_path or conn.execute(
            "SELECT 1 FROM storage_meta WHERE key='legacy_import_completed' LIMIT 1"
        ).fetchone()
        # 迁移标记写入 SQLite，而不是依赖“表是否为空”。这样用户删除全部数据后，
        # 重启也不会再次从旧 JSON 恢复已删除的数据。
        if not migration_done:
            sources = {
                "accounts": (_ACCOUNTS_JSON, _LEGACY_ACCOUNTS_JSON),
                "outlook": (_OUTLOOK_JSON, _LEGACY_OUTLOOK_JSON),
                "generic_api": (_GENERIC_API_EMAIL_JSON,),
                "jobs": (_JOBS_JSON, _LEGACY_JOBS_JSON),
                "domain": (_DOMAIN_EMAIL_JSON,),
            }
            for collection, paths in sources.items():
                table = _TABLES[collection]
                exists = conn.execute(
                    f"SELECT 1 FROM {table}" + (" WHERE source=?" if table == "email_pool" else " LIMIT 1"),
                    ((_EMAIL_SOURCES[collection],) if table == "email_pool" else ()),
                ).fetchone()
                if exists:
                    continue
                rows = None
            # 兼容上一版“records 单表 + collection”实现。
                if _table_exists(conn, "records"):
                    legacy = conn.execute("SELECT payload FROM records WHERE collection=? ORDER BY id", (collection,)).fetchall()
                    if legacy:
                        rows = [json.loads(item["payload"]) for item in legacy]
            # 兼容上一版按邮箱来源拆分的三张表。
                if collection in _EMAIL_SOURCES and rows is None:
                    old_table = _LEGACY_TABLES[collection]
                    if _table_exists(conn, old_table):
                        legacy = conn.execute(f"SELECT payload FROM {old_table} ORDER BY id").fetchall()
                        if legacy:
                            rows = [json.loads(item["payload"]) for item in legacy]
                for path in paths:
                    if rows is None and path.exists():
                        candidate = _read_json(path, None)
                        if isinstance(candidate, list):
                            rows = candidate
                            break
                if rows is None:
                    rows = _read_legacy_sqlite_collection(collection)
                if not rows:
                    continue
                next_email_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM email_pool").fetchone()[0]) + 1 if table == "email_pool" else 0
                for pos, row in enumerate(rows, 1):
                    row = dict(row)
                    rid = next_email_id if table == "email_pool" else int(row.get("id") or pos)
                    if table == "email_pool":
                        next_email_id += 1
                    row["id"] = rid
                    conn.execute(
                        f"INSERT OR REPLACE INTO {table}(id,email,source,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?)" if table == "email_pool" else
                        f"INSERT OR REPLACE INTO {table}(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                        ((rid, str(row.get("email") or ""), _EMAIL_SOURCES[collection], str(row.get("status") or ""),
                          int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                          str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False)) if table == "email_pool" else
                         (rid, str(row.get("email") or ""), str(row.get("status") or ""),
                          int(bool(row.get("archived"))), str(row.get("created_at") or row.get("imported_at") or ""),
                          str(row.get("updated_at") or ""), json.dumps(row, ensure_ascii=False))),
                    )
        # CPA Codex 凭证首次导入数据库；后续列表查询不再扫描 codex_accounts/ 文件。
        if not migration_done and not conn.execute("SELECT 1 FROM codex_accounts LIMIT 1").fetchone() and _CODEX_DIR.exists():
            state = _read_json(_LEGACY_CODEX_EXPORT_STATE, {})
            state = state if isinstance(state, dict) else {}
            for pos, path in enumerate(sorted(_CODEX_DIR.glob("codex-*.json")), 1):
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                    stat = path.stat()
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    logger.debug("无法迁移 Codex 凭证 %s：%s: %s", path, type(exc).__name__, exc)
                    continue
                filename = path.name
                meta = dict(content)
                meta["_filename"] = filename
                meta["_size"] = stat.st_size
                meta["_mtime"] = _local_fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                es = state.get(filename) or {}
                meta["_exported_at"] = es.get("exported_at")
                meta["_exported_count"] = es.get("exported_count", 0)
                meta["_archived"] = bool(es.get("archived"))
                conn.execute(
                    "INSERT OR IGNORE INTO codex_accounts(id,filename,email,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                    (pos, filename, str(content.get("email") or ""), int(meta["_archived"]), meta["_mtime"], meta["_mtime"], json.dumps(meta, ensure_ascii=False)),
                )
        # Agent 凭证也只在首次迁移时读取；运行期间完整内容保存在 SQLite。
        if not migration_done and not conn.execute("SELECT 1 FROM codex_agent_accounts LIMIT 1").fetchone() and _CODEX_AGENT_DIR.exists():
            for path in sorted(_CODEX_AGENT_DIR.glob("codex-agent-*.json")):
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                    stat = path.stat()
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    logger.debug("无法迁移 Codex Agent 凭证 %s：%s: %s", path, type(exc).__name__, exc)
                    continue
                identity = content.get("agent_identity") if isinstance(content.get("agent_identity"), dict) else {}
                email = str(content.get("email") or identity.get("email") or "").strip()
                account = conn.execute("SELECT id, payload FROM accounts WHERE lower(email)=lower(?) LIMIT 1", (email,)).fetchone() if email else None
                if not account:
                    continue
                account_id = int(account["id"])
                stamp = _local_fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                conn.execute(
                    "INSERT OR IGNORE INTO codex_agent_accounts(account_id,email,filename,created_at,updated_at,payload) VALUES(?,?,?,?,?,?)",
                    (account_id, email or str(json.loads(account["payload"]).get("email") or ""), path.name, stamp, stamp, json.dumps(content, ensure_ascii=False)),
                )
                account_payload = json.loads(account["payload"])
                account_payload.setdefault("codex_agent_token", json.dumps(content, ensure_ascii=False))
                account_payload.pop("codex_agent_auth_path", None)
                conn.execute("UPDATE accounts SET payload=?, updated_at=? WHERE id=?", (json.dumps(account_payload, ensure_ascii=False), stamp, account_id))
        conn.commit()
        # 迁移完成后删除旧的通用表，避免运行时继续依赖它。
        for old_table in (*_LEGACY_TABLES.values(), "records"):
            if _table_exists(conn, old_table) and old_table not in _TABLES.values():
                conn.execute(f"DROP TABLE {old_table}")
        if not migration_done:
            conn.execute("INSERT OR REPLACE INTO storage_meta(key, value) VALUES('legacy_import_completed', ?)", (_now(),))
        conn.commit()
        conn.close()
        _SQLITE_READY = True
        _SQLITE_READY_PATH = active_path


def _load_collection(collection: str) -> list[dict]:
    _ensure_sqlite()
    table = _TABLES[collection]
    with closing(_sqlite_conn()) as conn, conn:
        sql = f"SELECT payload FROM {table}"
        params: tuple[str, ...] = ()
        if table == "email_pool":
            sql += " WHERE source=?"; params = (_EMAIL_SOURCES[collection],)
        sql += " ORDER BY id"
        return [json.loads(row["payload"]) for row in conn.execute(sql, params)]


def _save_collection(collection: str, rows: list[dict], *, replace_existing: bool = True) -> None:
    _ensure_sqlite()
    table = _TABLES[collection]
    with closing(_sqlite_conn()) as conn, conn:
        if replace_existing:
            if table == "email_pool":
                conn.execute("DELETE FROM email_pool WHERE source=?", (_EMAIL_SOURCES[collection],))
            else:
                conn.execute(f"DELETE FROM {table}")
        for pos, raw in enumerate(rows, 1):
            row = dict(raw)
            rid = int(row.get("id") or pos)
            row["id"] = rid
            if table == "email_pool" and conn.execute("SELECT 1 FROM email_pool WHERE id=?", (rid,)).fetchone():
                rid = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM email_pool").fetchone()[0])
                row["id"] = rid
            values = (
                rid,
                str(row.get("email") or ""),
                str(row.get("status") or ""),
                int(bool(row.get("archived"))),
                str(row.get("created_at") or row.get("imported_at") or ""),
                str(row.get("updated_at") or ""),
                json.dumps(row, ensure_ascii=False),
            )
            if replace_existing:
                conn.execute(
                    f"INSERT INTO {table}(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                    values,
                )
            else:
                conn.execute(
                    f"INSERT INTO {table}(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET email=excluded.email, status=excluded.status, "
                    "archived=excluded.archived, created_at=excluded.created_at, "
                    "updated_at=excluded.updated_at, payload=excluded.payload",
                    values,
                )


def _query_collection(collection: str, *, status: str | None = None, archived: str | bool | None = None,
                       q: str | None = None, date_from: str | None = None, date_to: str | None = None,
                       limit: int | None = None, offset: int = 0) -> list[dict]:
    """利用索引分页读取，避免 WebUI 为一个页面加载整个 JSON 文件。"""
    _ensure_sqlite()
    table = _TABLES[collection]
    where = ["1=1"]
    params: list[Any] = []
    if table == "email_pool":
        where.append("source=?"); params.append(_EMAIL_SOURCES[collection])
    if status:
        where.append("status=?"); params.append(status)
    if archived not in (None, "all", "include"):
        where.append("archived=?"); params.append(int(archived in (True, "1", "true", "yes", "only")))
    if q and str(q).strip():
        where.append("lower(payload) LIKE ?"); params.append("%" + str(q).strip().lower() + "%")
    if date_from:
        where.append("created_at >= ?"); params.append(str(date_from) + ("T00:00:00" if len(str(date_from)) == 10 else ""))
    if date_to:
        value = str(date_to)
        where.append("created_at <= ?"); params.append(value + ("T23:59:59.999999" if len(value) == 10 else ""))
    sql = f"SELECT payload FROM {table} WHERE " + " AND ".join(where) + " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"; params.extend([max(0, int(limit)), max(0, int(offset))])
    with closing(_sqlite_conn()) as conn:
        return [json.loads(row["payload"]) for row in conn.execute(sql, params)]


def _query_collection_page(collection: str, *, status: str | None = None,
                           archived: str | bool | None = None, q: str | None = None,
                           date_from: str | None = None, date_to: str | None = None,
                           extra_where: list[str] | None = None,
                           extra_params: list[Any] | None = None,
                           limit: int = 50, offset: int = 0) -> tuple[list[dict], int, str]:
    """执行真正的 SQL COUNT/LIMIT/OFFSET 分页，并返回最新更新时间。"""
    _ensure_sqlite()
    table = _TABLES[collection]
    where = ["1=1"]
    params: list[Any] = []
    if table == "email_pool":
        where.append("source=?"); params.append(_EMAIL_SOURCES[collection])
    if status:
        where.append("status=?"); params.append(status)
    if archived not in (None, "all", "include"):
        where.append("archived=?"); params.append(int(archived in (True, "1", "true", "yes", "only")))
    if q and str(q).strip():
        where.append("lower(payload) LIKE ?"); params.append("%" + str(q).strip().lower() + "%")
    if date_from:
        value = str(date_from)
        where.append("created_at >= ?"); params.append(value + ("T00:00:00" if len(value) == 10 else ""))
    if date_to:
        value = str(date_to)
        where.append("created_at <= ?"); params.append(value + ("T23:59:59.999999" if len(value) == 10 else ""))
    if extra_where:
        where.extend(extra_where)
        params.extend(extra_params or [])
    clause = " AND ".join(where)
    with closing(_sqlite_conn()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", params).fetchone()[0])
        latest = str(conn.execute(f"SELECT COALESCE(MAX(updated_at), '') FROM {table} WHERE {clause}", params).fetchone()[0] or "")
        rows = [json.loads(row["payload"]) for row in conn.execute(
            f"SELECT payload FROM {table} WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, max(1, int(limit)), max(0, int(offset))],
        )]
    return rows, total, latest


def _account_filter_sql(
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    free_plus_export_filter: str | None = None,
    totp_filter: str | None = None,
) -> tuple[list[str], list[Any]]:
    """把账号列表的套餐、Codex、2FA 过滤条件下推到 SQLite。

    套餐、Codex、2FA 状态仍保存在账号 payload 中，因此这里使用 SQLite JSON1
    直接过滤，而不是先把整张 accounts 表反序列化到 Python 再切页。
    """
    where: list[str] = []
    params: list[Any] = []
    plan = str(plan_filter or "").strip().lower()
    codex = str(codex_filter or "").strip().lower()
    free_plus_export = str(free_plus_export_filter or "").strip().lower()
    totp = str(totp_filter or "").strip().lower()

    plan_expr = (
        "lower(COALESCE(NULLIF(CAST(json_extract(payload, '$.current_plan_type') AS TEXT), ''), "
        "CAST(json_extract(payload, '$.plan_type') AS TEXT), ''))"
    )
    if plan and plan not in {"all", "any"}:
        if plan in {"free_plus", "free_plus_trial", "plus_trial_eligible"}:
            where.extend([
                f"{plan_expr} = ?",
                "COALESCE(json_extract(payload, '$.plus_trial_eligible'), 0) IN (1, '1', 'true')",
            ])
            params.append("free")
        elif plan == "plus":
            # 与 _account_matches_plan_filter 保持一致：free(可试用)不算已开通 Plus。
            where.extend([f"{plan_expr} LIKE ?", f"{plan_expr} NOT LIKE ?"])
            params.extend(["%plus%", "%free%"])
        elif plan == "free":
            where.append(f"{plan_expr} = ?")
            params.append("free")
        else:
            where.append(f"{plan_expr} = ?")
            params.append(plan)

    if free_plus_export and free_plus_export not in {"all", "any"}:
        exported_expr = "json_extract(payload, '$.free_plus_exported_at')"
        if free_plus_export in {"unexported", "pending", "未导出"}:
            where.append(f"({exported_expr} IS NULL OR CAST({exported_expr} AS TEXT) = '')")
        elif free_plus_export in {"exported", "done", "已导出"}:
            where.append(f"({exported_expr} IS NOT NULL AND CAST({exported_expr} AS TEXT) != '')")

    status_expr = "lower(COALESCE(CAST(json_extract(payload, '$.codex_status') AS TEXT), ''))"
    live_status_expr = "lower(COALESCE(CAST(json_extract(payload, '$.live_check_status') AS TEXT), ''))"
    if codex and codex not in {"all", "*"}:
        if codex == "deactivated":
            where.append(f"{live_status_expr} = ?")
        else:
            where.append(f"{status_expr} = ?")
        params.append(codex)

    totp_secret_expr = "lower(COALESCE(CAST(json_extract(payload, '$.totp_secret') AS TEXT), ''))"
    totp_setup_expr = "lower(COALESCE(CAST(json_extract(payload, '$.totp_setup_status') AS TEXT), ''))"
    if totp and totp not in {"all", "*"}:
        if totp in {"enabled", "on", "active"}:
            where.append(f"length(trim({totp_secret_expr})) > 0")
        elif totp in {"disabled", "off", "not_enabled", "unset"}:
            where.append(f"length(trim({totp_secret_expr})) = 0")
        elif totp in {"pending", "setup", "setting", "queued", "running"}:
            where.append(f"{totp_setup_expr} IN (?, ?)")
            params.extend(["queued", "running"])
        elif totp == "failed":
            where.append(f"{totp_setup_expr} = ?")
            params.append("failed")
        elif totp == "stopped":
            where.append(f"{totp_setup_expr} = ?")
            params.append("stopped")
        else:
            where.append(f"{totp_setup_expr} = ?")
            params.append(totp)
    return where, params


def _pool_summary_sql(collection: str) -> dict:
    _ensure_sqlite()
    table = _TABLES[collection]
    with closing(_sqlite_conn()) as conn:
        where = " WHERE source=?" if table == "email_pool" else ""
        params = (_EMAIL_SOURCES[collection],) if table == "email_pool" else ()
        counts = {str(r["status"] or "available"): int(r["n"]) for r in conn.execute(
            f"SELECT status, COUNT(*) AS n FROM {table}{where} GROUP BY status", params
        )}
    out = {"available": counts.get("available", 0), "used": counts.get("used", 0), "failed": counts.get("failed", 0)}
    out.update({k: v for k, v in counts.items() if k not in out})
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def _is_project_runtime_document(path: Path) -> bool:
    """Return whether *path* is a root-level runtime export without dereferencing it.

    Docker maps these files from the writable runtime volume back into `/app`
    as symlinks. Resolving first would make a project-owned document appear to
    live outside the project and bypass the canonical SQLite store.
    """
    return path.absolute().parent == _PROJECT_ROOT.absolute()


def _read_json(path: Path, default: Any) -> Any:
    _ensure_storage()
    if _is_project_runtime_document(path):
        return app_state_db.get_document(path, default)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.debug("读取 JSON 文件失败: %s", path, exc_info=True)
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_storage()
    if _is_project_runtime_document(path):
        app_state_db.set_document(path, data)
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _next_id(items: list[dict]) -> int:
    ids = [int(item.get("id") or 0) for item in items]
    return (max(ids) if ids else 0) + 1


def _next_registration_job_id(rows: list[dict]) -> int:
    """Reserve a job ID that remains unique after job records are deleted."""
    current_max = max((int(item.get("id") or 0) for item in rows), default=0)
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = conn.execute(
                "SELECT value FROM storage_meta WHERE key = ?",
                (_REGISTRATION_JOB_SEQUENCE_KEY,),
            ).fetchone()
            try:
                stored_next = int(stored["value"]) if stored else 1
            except (TypeError, ValueError):
                stored_next = 1

            qan8_max = 0
            if _table_exists(conn, "qan8_assignments"):
                for item in conn.execute("SELECT job_id FROM qan8_assignments"):
                    try:
                        qan8_max = max(qan8_max, int(str(item["job_id"])))
                    except (TypeError, ValueError):
                        continue

            next_id = max(stored_next, current_max + 1, qan8_max + 1, 1)
            conn.execute(
                "INSERT INTO storage_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_REGISTRATION_JOB_SEQUENCE_KEY, str(next_id + 1)),
            )
            conn.commit()
            return next_id
        except Exception:
            conn.rollback()
            raise


def _outlook_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("password") or "",
        row.get("client_id") or "",
        row.get("refresh_token") or "",
    ])


def _generic_api_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("code_url") or "",
    ])


def _extract_registration_password(row: dict) -> str:
    extra_raw = row.get("extra_json")
    if isinstance(extra_raw, str) and extra_raw.strip():
        try:
            extra = json.loads(extra_raw)
        except (TypeError, json.JSONDecodeError):
            logger.debug("解析注册密码扩展字段失败", exc_info=True)
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
    elif isinstance(extra_raw, dict):
        extra = extra_raw
    else:
        extra = {}
    return str(extra.get("registration_password") or row.get("registration_password") or "").strip()


def _looks_like_email_material_segment(segment: str) -> bool:
    seg = str(segment or "").strip()
    if not seg:
        return False
    if seg.startswith(("M.", "m.")):
        return True
    if len(seg) >= 32 and "-" in seg and seg.count("-") >= 4:
        return True
    return any(ch in seg for ch in ("@", ":", "/", "\\"))


def _ensure_password_in_material_line(base: str, password: str) -> str:
    base = str(base or "").strip()
    password = str(password or "").strip()
    if not password:
        return base
    parts = [p for p in base.split("----") if p != ""] if base else []
    if not parts:
        return password
    if len(parts) == 1:
        if parts[0] == password:
            return base
        return "----".join([parts[0], password])
    if parts[1] == password:
        return base
    if _looks_like_email_material_segment(parts[1]):
        parts.insert(1, password)
        return "----".join(parts)
    return base


def _account_line(row: dict) -> str:
    return account_line(row, "modern")


def _gmail_api_url_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("code_url") or "",
    ])


def _registration_password(row: dict) -> str:
    value = row.get("registration_password")
    if value is not None:
        return str(value or "")
    extra_json = row.get("extra_json")
    if extra_json:
        try:
            extra = json.loads(extra_json) if isinstance(extra_json, str) else extra_json
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = {}
        if isinstance(extra, dict):
            return str(extra.get("registration_password") or "")
    return ""


def _normalize_account_line_format(format_name: str | None = None) -> str:
    value = str(format_name or "modern").strip().lower()
    if value in {"legacy", "old", "current_legacy"}:
        return "legacy"
    if value in {"modern", "current", "email_pass_2fa"}:
        return "modern"
    raise ValueError("format 仅支持 modern 或 legacy")


def account_line(row: dict, format_name: str | None = None) -> str:
    """生成账号导出行；格式仅影响导出，不改变账号存储。"""
    output_format = _normalize_account_line_format(format_name)
    if output_format == "legacy":
        material = row.get("original_email_line") or row.get("email") or ""
        token = row.get("access_token") or ""
        totp = row.get("totp_secret") or ""
        return f"{material}----{token}----{totp}" if totp else f"{material}----{token}"
    email = str(row.get("email") or "")
    password = _registration_password(row)
    totp = str(row.get("totp_secret") or "")
    return f"{email} | {password} | {totp}"




def _registered_email_line(row: dict) -> str:
    """生成注册成功邮箱 TXT 的行内容；token 由注册成功的token.txt 单独保存。"""
    return row.get("original_email_line") or row.get("email") or ""


def _load_outlook() -> list[dict]:
    return _load_collection("outlook")
def _sync_outlook_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_outlook_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _OUTLOOK_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_generic_api_email_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_generic_api_email_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _GENERIC_API_EMAIL_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_gmail_api_url_email_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_gmail_api_url_email_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _GMAIL_API_URL_EMAIL_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_accounts_txt(rows: list[dict]) -> None:
    lines = [_registered_email_line(r) for r in sorted(rows, key=lambda x: int(x.get("id") or 0))]
    _ACCOUNTS_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_tokens_txt(rows: list[dict]) -> None:
    tokens = [
        r.get("access_token") or ""
        for r in sorted(rows, key=lambda x: int(x.get("id") or 0))
        if r.get("access_token")
    ]
    _TOKENS_TXT.write_text(("\n".join(tokens) + ("\n" if tokens else "")), encoding="utf-8")


def _viewer_snapshot(outlook_rows: list[dict], account_rows: list[dict]) -> dict:
    account_by_email = {
        (a.get("email") or "").lower(): a
        for a in account_rows
    }
    return {
        "generated_at": _now(),
        "accounts": [
            _decorate_account(r)
            for r in sorted(account_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "outlook": [
            _decorate_outlook(r, account_by_email)
            for r in sorted(outlook_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "summary": {
            "accounts": len(account_rows),
            "outlook_total": len(outlook_rows),
            "outlook_available": sum(1 for r in outlook_rows if r.get("status") == "available"),
            "outlook_used": sum(1 for r in outlook_rows if r.get("status") == "used"),
            "outlook_failed": sum(1 for r in outlook_rows if r.get("status") == "failed"),
        },
    }


def _render_static_viewer(outlook_rows: list[dict] | None = None, account_rows: list[dict] | None = None) -> Path:
    """生成可直接双击打开的静态账号查看页。"""
    outlook_rows = _load_outlook() if outlook_rows is None else outlook_rows
    account_rows = _load_accounts() if account_rows is None else account_rows
    snapshot = _viewer_snapshot(outlook_rows, account_rows)
    data_json = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    title = escape(f"账号查看器 - {snapshot['generated_at']}")
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    :root {{
      --bg: #eef3f8;
      --surface: #ffffff;
      --soft: #f7f9fc;
      --text: #172033;
      --muted: #667085;
      --line: #d9e2ec;
      --blue: #2563eb;
      --green: #16803c;
      --red: #c2413a;
      --amber: #b7791f;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 22px 28px;
      background: #101827;
      color: #fff;
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: center;
      flex-wrap: wrap;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    .meta {{ margin-top: 6px; color: #b8c7d9; font-size: 13px; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .stat {{
      min-width: 116px;
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 8px;
      background: rgba(255,255,255,.08);
    }}
    .stat span {{ display: block; color: #b8c7d9; font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 18px; }}
    main {{ width: min(1500px, calc(100vw - 32px)); margin: 16px auto 30px; display: grid; gap: 16px; }}
    .toolbar, section {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 8px 22px rgba(15,23,42,.06);
    }}
    .toolbar {{ padding: 14px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .search {{ min-width: min(520px, 100%); flex: 1; }}
    input {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
    }}
    .buttons {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    button {{
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--soft); }}
    button.primary {{ border-color: var(--blue); background: var(--blue); color: #fff; }}
    button.good {{ border-color: #2f855a; background: #edf8f1; color: #166534; }}
    button:disabled {{ color: #98a2b3; cursor: not-allowed; background: #f2f4f7; }}
    .head {{ padding: 14px 16px; border-bottom: 1px solid var(--line); background: var(--soft); }}
    .head p {{ margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .table-wrap {{ overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: left; white-space: nowrap; vertical-align: middle; }}
    th {{ position: sticky; top: 0; background: #fbfcfe; color: #475467; z-index: 1; font-size: 12px; }}
    tr:hover td {{ background: #fbfdff; }}
    .main-cell {{ font-weight: 700; }}
    .sub-cell {{ margin-top: 3px; color: var(--muted); font-size: 12px; }}
    .mono {{ font-family: ui-monospace, "JetBrains Mono", Consolas, monospace; font-size: 12px; }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; min-width: 48px; justify-content: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .status-available {{ color: var(--blue); background: #eef4ff; }}
    .status-used {{ color: #475467; background: #f2f4f7; }}
    .status-failed {{ color: var(--red); background: #fff0ef; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    #toast {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      padding: 10px 14px;
      border-radius: 8px;
      background: #101827;
      color: #fff;
      box-shadow: 0 14px 30px rgba(15,23,42,.24);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }}
    #toast.show {{ opacity: 1; transform: translateY(0); }}
    @media (max-width: 820px) {{
      header {{ align-items: flex-start; }}
      .stats {{ width: 100%; }}
      .stat {{ flex: 1; }}
    }}
  </style>
</head>
<body>
<header>
  <div>
    <h1>账号查看器</h1>
    <p class="meta">静态快照，无需启动 Web Server。生成时间：<span id="generated"></span></p>
  </div>
  <div class="stats">
    <div class="stat"><span>已完成</span><strong id="statAccounts">0</strong></div>
    <div class="stat"><span>邮箱总数</span><strong id="statOutlook">0</strong></div>
    <div class="stat"><span>可用邮箱</span><strong id="statAvailable">0</strong></div>
  </div>
</header>
<main>
  <div class="toolbar">
    <div class="search"><input id="q" placeholder="搜索邮箱、token、clientId、状态"></div>
    <div class="buttons">
      <button class="primary" id="copyAllTokens">复制全部 Token</button>
      <button class="good" id="copyAllLines">复制全部整行</button>
      <button id="copyAllEmails">复制全部邮箱素材</button>
    </div>
  </div>
  <section>
    <div class="head">
      <h2>已完成账号</h2>
      <p>整行格式：邮箱----密码----clientId----邮箱刷新令牌----accessToken----totpSecret（如有）</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>邮箱</th><th>来源</th><th>Token</th><th>备注</th><th>2FA</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody id="accountsBody"></tbody>
      </table>
    </div>
  </section>
  <section>
    <div class="head">
      <h2>邮箱素材库</h2>
      <p>原始格式：邮箱----密码----clientId----邮箱刷新令牌；注册完成后可直接复制对应 Token 或整行。</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>邮箱</th><th>状态</th><th>Token</th><th>导入时间</th><th>已用时间</th><th>操作</th></tr></thead>
        <tbody id="outlookBody"></tbody>
      </table>
    </div>
  </section>
</main>
<div id="toast"></div>
<script id="snapshot" type="application/json">{data_json}</script>
<script>
const SNAPSHOT = JSON.parse(document.getElementById('snapshot').textContent);
const $ = (s) => document.querySelector(s);
let copySeq = 0;
const copyStore = new Map();

function fmt(v) {{ return v == null || v === '' ? '-' : String(v); }}
function esc(v) {{
  return fmt(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
function short(v, n = 34) {{
  const s = v || '';
  return s.length > n ? `${{s.slice(0, n)}}...` : s;
}}
function copyId(v) {{
  if (!v) return '';
  const id = `c${{++copySeq}}`;
  copyStore.set(id, v);
  return id;
}}
function btn(label, value, cls = '') {{
  const id = copyId(value);
  return `<button class="${{cls}}" data-copy-id="${{id}}" ${{id ? '' : 'disabled'}}>${{label}}</button>`;
}}
function pill(status) {{
  const map = {{ available: '可用', used: '已用', failed: '失败' }};
  const label = map[status] || status || '-';
  return `<span class="pill status-${{esc(status)}}">${{esc(label)}}</span>`;
}}
function showToast(text) {{
  const toast = $('#toast');
  toast.textContent = text;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove('show'), 1400);
}}
async function copyText(text) {{
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
  }} else {{
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }}
  showToast('已复制');
}}
function haystack(row) {{
  return Object.values(row).join('\\n').toLowerCase();
}}
function render() {{
  copyStore.clear();
  copySeq = 0;
  const q = $('#q').value.trim().toLowerCase();
  const accounts = SNAPSHOT.accounts.filter((r) => !q || haystack(r).includes(q));
  const outlook = SNAPSHOT.outlook.filter((r) => !q || haystack(r).includes(q));
  $('#generated').textContent = SNAPSHOT.generated_at;
  $('#statAccounts').textContent = SNAPSHOT.summary.accounts;
  $('#statOutlook').textContent = SNAPSHOT.summary.outlook_total;
  $('#statAvailable').textContent = SNAPSHOT.summary.outlook_available;
  $('#accountsBody').innerHTML = accounts.map((r) => `
    <tr>
      <td class="muted">#${{esc(r.id)}}</td>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell">${{esc(r.user_name || '-')}}</div></td>
      <td>${{esc(r.email_source || '-')}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 42))}}</span></td>
      <td title="${{esc(r.note || '')}}">${{r.note ? esc(short(r.note, 60)) : '<span class="muted">-</span>'}}</td>
      <td>${{r.totp_secret ? '已启用' : '<span class="muted">未启用</span>'}}</td>
      <td class="muted">${{esc(r.created_at || '-')}}</td>
      <td class="actions">${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.copy_line, 'good')}}</td>
    </tr>`).join('');
  $('#outlookBody').innerHTML = outlook.map((r) => `
    <tr>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell mono">${{esc(short(r.copy_line, 76))}}</div></td>
      <td>${{pill(r.status)}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 36) || '未生成')}}</span></td>
      <td class="muted">${{esc(r.imported_at || r.created_at || '-')}}</td>
      <td class="muted">${{esc(r.used_at || '-')}}</td>
      <td class="actions">${{btn('复制邮箱', r.copy_line)}} ${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.account_copy_line, 'good')}}</td>
    </tr>`).join('');
}}
document.addEventListener('click', (e) => {{
  const target = e.target.closest('[data-copy-id]');
  if (!target) return;
  copyText(copyStore.get(target.dataset.copyId));
}});
$('#q').addEventListener('input', render);
$('#copyAllTokens').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.access_token).filter(Boolean).join('\\n')));
$('#copyAllLines').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.copy_line).filter(Boolean).join('\\n')));
$('#copyAllEmails').addEventListener('click', () => copyText(SNAPSHOT.outlook.map((r) => r.copy_line).filter(Boolean).join('\\n')));
render();
</script>
</body>
</html>
"""
    # The Docker image links this compatibility export from /app into
    # /var/lib/turb. Create the atomic-write temp file beside its real target.
    export_target = _VIEWER_HTML.resolve()
    tmp = export_target.with_suffix(".html.tmp")
    try:
        tmp.write_text(html_text, encoding="utf-8")
        tmp.replace(export_target)
        return _VIEWER_HTML
    except PermissionError:
        # Windows 下如果目标 HTML 正被浏览器或编辑器短暂占用，原子替换可能失败。
        # 先尝试直接覆盖；仍失败时写一个时间戳快照，避免注册流程被查看页刷新阻断。
        try:
            export_target.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return _VIEWER_HTML
        except PermissionError:
            fallback = _DATA_DIR / f"accounts_viewer_{_local_now().strftime('%Y%m%d_%H%M%S')}.html"
            fallback.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return fallback


def _run_debounced_static_viewer_refresh() -> None:
    """后台刷新静态账号查看页。

    WebUI 的实时列表不依赖 accounts_viewer.html；把它从保存路径上移走，
    避免注册/查活/Codex/套餐状态高频写入时，反复生成大 HTML 并阻塞查询 API。
    """
    global _VIEWER_REFRESH_TIMER, _VIEWER_REFRESH_REASON
    with _VIEWER_REFRESH_LOCK:
        _VIEWER_REFRESH_TIMER = None
        reason = _VIEWER_REFRESH_REASON
        _VIEWER_REFRESH_REASON = ""
    try:
        with _LOCK:
            outlook_rows = _load_outlook()
            account_rows = _load_accounts()
            _render_static_viewer(outlook_rows=outlook_rows, account_rows=account_rows)
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, AttributeError) as exc:
        # 静态查看页只是旁路产物，失败不应影响主流程。
        logger.exception("后台刷新 accounts_viewer.html 失败: %s", reason or "-", exc_info=exc)


def _schedule_static_viewer_refresh(reason: str = "") -> None:
    """防抖刷新静态查看页：短时间内多次保存只生成一次 HTML。"""
    global _VIEWER_REFRESH_TIMER, _VIEWER_REFRESH_REASON
    if _ACCOUNTS_JSON != _DEFAULT_ACCOUNTS_JSON or _VIEWER_HTML != _DEFAULT_VIEWER_HTML:
        return
    with _VIEWER_REFRESH_LOCK:
        _VIEWER_REFRESH_REASON = reason or _VIEWER_REFRESH_REASON
        if _VIEWER_REFRESH_TIMER is not None:
            _VIEWER_REFRESH_TIMER.cancel()
        timer = threading.Timer(_VIEWER_DEBOUNCE_SECONDS, _run_debounced_static_viewer_refresh)
        timer.daemon = True
        _VIEWER_REFRESH_TIMER = timer
        timer.start()


def _load_outlook_legacy_files() -> list[dict]:
    rows = _read_json(_OUTLOOK_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_outlook(rows: list[dict]) -> None:
    _save_collection("outlook", rows)


def _load_generic_api_emails() -> list[dict]:
    return _load_collection("generic_api")


def _save_generic_api_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _generic_api_email_line(row)
    _save_collection("generic_api", rows)


def _load_gmail_api_url_emails() -> list[dict]:
    rows = _read_json(_GMAIL_API_URL_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_gmail_api_url_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _gmail_api_url_email_line(row)
    _write_json(_GMAIL_API_URL_EMAIL_JSON, rows)
    _sync_gmail_api_url_email_txt(rows)


def _load_accounts() -> list[dict]:
    return _load_collection("accounts")


def _save_accounts(rows: list[dict], *, allow_delete: bool = False) -> None:
    for row in rows:
        row["copy_line"] = _account_line(row)
    _save_collection("accounts", rows, replace_existing=allow_delete)
    persisted_rows = rows if allow_delete else _load_accounts()
    _write_json(_ACCOUNTS_JSON, persisted_rows)
    _sync_accounts_txt(persisted_rows)
    _sync_tokens_txt(persisted_rows)
    _schedule_static_viewer_refresh("save_accounts")


def _load_jobs() -> list[dict]:
    return _load_collection("jobs")


def _save_jobs(rows: list[dict]) -> None:
    _save_collection("jobs", rows)


def _find_by_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def _decorate_account(row: dict) -> dict:
    out = dict(row)
    out["email_domain"] = _account_email_domain(out.get("email")) or "unknown"
    if not out.get("account_locale") and not out.get("account_country"):
        try:
            extra = json.loads(out.get("extra_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = {}
        if isinstance(extra, dict):
            from core.account_locale import derive_account_locale

            out.update(derive_account_locale(extra=extra))
    out["note"] = out.get("note") or ""
    out["note_updated_at"] = out.get("note_updated_at") or ""
    plan_status = out.get("plan_check_status")
    if plan_status in {"queued", "running"}:
        try:
            stamp_key = "plan_check_queued_at" if plan_status == "queued" else "plan_check_started_at"
            stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if plan_status == "queued" else _PLAN_CHECK_STALE_SECONDS
            started_at = datetime.fromisoformat(str(out.get(stamp_key) or ""))
            if (_local_now() - started_at).total_seconds() >= stale_after:
                out["plan_check_status"] = "failed"
                out["plan_check_error"] = "上次套餐查询状态已超时，可重新查询"
                out["plan_check_stale"] = True
        except (TypeError, ValueError):
            out["plan_check_status"] = "failed"
            out["plan_check_error"] = "上次套餐查询状态异常，可重新查询"
            out["plan_check_stale"] = True
    out["copy_line"] = _account_line(out)
    return out


def _account_matches_plan_filter(row: dict, plan_filter: str | None = None) -> bool:
    """账号套餐过滤。plus 表示已开通 Plus；free_plus 表示可用 Plus 试用。"""
    f = str(plan_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if f in {"unknown", "unresolved", "未查询", "未识别"}:
        return not plan or plan in {"unknown", "unresolved", "none", "null"}
    if f in {"free_plus", "free_plus_trial", "plus_trial_eligible"}:
        return plan == "free" and bool(row.get("plus_trial_eligible"))
    if f == "plus":
        # “free(可Plus试用)”只是可试用，不算已开通 Plus。
        return "plus" in plan and "free" not in plan
    if f == "free":
        return plan == "free"
    return plan == f


def _account_matches_free_plus_export_filter(row: dict, export_filter: str | None = None) -> bool:
    value = str(export_filter or "").strip().lower()
    if not value or value in {"all", "any"}:
        return True
    exported = bool(row.get("free_plus_exported_at"))
    if value in {"unexported", "pending", "未导出"}:
        return not exported
    if value in {"exported", "done", "已导出"}:
        return exported
    return True


def _account_matches_email_source_filter(row: dict, source_filter: str | None = None) -> bool:
    """按注册邮箱来源精确过滤账号，并把空值/未知值归入 unknown。"""
    value = str(source_filter or "").strip().lower()
    if not value or value in {"all", "any", "*"}:
        return True

    raw_source = str(row.get("email_source") or "").strip().lower()
    from core.email_provider import is_valid_email_source, normalize_email_source

    normalized_source = normalize_email_source(raw_source)
    if value in {"unknown", "unresolved", "none", "null", "未识别"}:
        return not raw_source or not is_valid_email_source(raw_source)
    return is_valid_email_source(normalized_source) and normalized_source == normalize_email_source(value)


def _account_email_domain(email: str | None) -> str:
    """Return the normalized domain portion of an account email."""
    value = str(email or "").strip().lower()
    if "@" not in value:
        return ""
    domain = value.rsplit("@", 1)[1].strip().rstrip(".")
    return domain if domain and "@" not in domain and " " not in domain else ""


def _account_matches_email_domain_filter(row: dict, domain_filter: str | None = None) -> bool:
    value = str(domain_filter or "").strip().lower().lstrip("@").rstrip(".")
    if not value or value in {"all", "any", "*"}:
        return True
    domain = _account_email_domain(row.get("email"))
    if value in {"unknown", "unresolved", "none", "null", "未识别"}:
        return not domain
    return domain == value



def _decorate_outlook(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _outlook_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_generic_api_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _generic_api_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def list_email_pool_page(
    source: str = "all",
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """从统一邮箱库直接执行 COUNT + LIMIT/OFFSET。

    ``email_pool`` 是三个邮箱来源共用的表。source 为具体来源时按 id 倒序，
    source=all 时按入库时间合并倒序；两种情况都只从 SQLite 取当前页，
    不再先加载全部邮箱再由 WebUI 切片。
    """
    source = str(source or "outlook").strip().lower()
    if source not in {"all", "outlook", "generic_api", "cloudflare_domain"}:
        source = "outlook"
    collection = "domain" if source == "cloudflare_domain" else source
    db_source = None if source == "all" else _EMAIL_SOURCES[collection]
    limit = max(1, int(limit))
    offset = max(0, int(offset or 0))
    where = ["1=1"]
    params: list[Any] = []
    if db_source is not None:
        where.append("ep.source=?")
        params.append(db_source)
    if status:
        where.append("ep.status=?")
        params.append(status)
    if q and str(q).strip():
        like = "%" + str(q).strip().lower() + "%"
        # payload 覆盖邮箱池自身字段；source 和关联账号 payload 保持旧 WebUI
        # 的搜索能力（例如搜索 generic_api 或已注册账号 token）。
        where.append(
            "(lower(ep.payload) LIKE ? OR lower(ep.source) LIKE ? OR EXISTS ("
            "SELECT 1 FROM accounts AS a "
            "WHERE a.email = ep.email COLLATE NOCASE AND lower(a.payload) LIKE ?))"
        )
        params.extend([like, like, like])
    clause = " AND ".join(where)
    order_by = "ep.created_at DESC, ep.id DESC" if source == "all" else "ep.id DESC"
    with _LOCK, closing(_sqlite_conn()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM email_pool AS ep WHERE {clause}", params).fetchone()[0])
        latest = str(conn.execute(
            f"SELECT COALESCE(MAX(ep.updated_at), '') FROM email_pool AS ep WHERE {clause}",
            params,
        ).fetchone()[0] or "")
        rows = conn.execute(
            f"SELECT ep.payload, ep.source, "
            f"(SELECT a.payload FROM accounts AS a "
            f" WHERE a.email = ep.email COLLATE NOCASE ORDER BY a.id DESC LIMIT 1) AS account_payload "
            f"FROM email_pool AS ep WHERE {clause} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

    source_names = {value: key for key, value in _EMAIL_SOURCES.items()}
    items: list[dict] = []
    for row in rows:
        item = json.loads(row["payload"])
        account_payload = row["account_payload"]
        account = None
        if account_payload:
            try:
                account = json.loads(account_payload)
            except (TypeError, ValueError):
                account = None
        item_source = source_names.get(str(row["source"]), str(row["source"]))
        if item_source == "outlook":
            item = _decorate_outlook(item, {str(item.get("email") or "").lower(): account} if account else {})
        elif item_source == "generic_api":
            item = _decorate_generic_api_email(item, {str(item.get("email") or "").lower(): account} if account else {})
        else:
            item = dict(item)
        item["source"] = item_source
        if not item.get("copy_line"):
            item["copy_line"] = item.get("email") or ""
        items.append(item)
    return {"items": items, "total": total, "offset": offset, "limit": limit, "latest": latest}


def _get_conn() -> sqlite3.Connection:
    """兼容旧入口：返回 SQLite 连接。"""
    return _sqlite_conn()
def _decorate_gmail_api_url_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    # OTP cache is an internal polling baseline; never expose it through pool APIs/UI.
    out.pop("last_otp", None)
    out.pop("last_otp_at", None)
    out["copy_line"] = _gmail_api_url_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _attach_gmail_api_url_alias_stats(rows: list[dict]) -> list[dict]:
    """Attach alias inventory counts without exposing the source code URL."""
    result = [dict(row) for row in rows]
    from core.qan8_gmail_api_store import Qan8GmailApiStore

    qan8_store = Qan8GmailApiStore(_SQLITE_PATH, initialize_schema=False)
    code_urls = {
        str(row.get("code_url") or "").strip()
        for row in result
        if str(row.get("code_url") or "").strip()
    }
    usage_by_url = GmailApiUrlBatchStore(_SQLITE_PATH).alias_usage_for_code_urls(code_urls)
    empty_usage = {"allocated": set(), "consumed": set(), "failed": set(), "reserved": set()}
    for row in result:
        qan8_usage = qan8_store.alias_usage_for_source(
            str(row.get("email") or ""),
            str(row.get("code_url") or ""),
        )
        if qan8_usage is not None:
            row.update({
                "alias_total": int(qan8_usage["total"]),
                "alias_allocated": int(qan8_usage["total"]),
                "alias_available": int(qan8_usage["available"]),
                "alias_used": int(qan8_usage["used"]),
                "alias_failed": int(qan8_usage["failed"]),
                "alias_reserved": int(qan8_usage["reserved"]),
            })
            continue
        try:
            candidates = generate_gmail_dual_domain_aliases(
                row.get("email"), limit=MAX_GMAIL_DUAL_DOMAIN_VARIANTS
            )
        except GmailAliasError:
            candidates = []
        candidate_set = {str(alias).strip().casefold() for alias in candidates if alias}
        usage = usage_by_url.get(str(row.get("code_url") or "").strip(), empty_usage)
        consumed = candidate_set & usage["consumed"]
        failed = (candidate_set & usage["failed"]) - consumed
        reserved = (candidate_set & usage["reserved"]) - consumed - failed
        row.update({
            "alias_total": len(candidate_set),
            "alias_allocated": len(candidate_set & usage["allocated"]),
            "alias_available": max(0, len(candidate_set) - len(consumed) - len(failed) - len(reserved)),
            "alias_used": len(consumed),
            "alias_failed": len(failed),
            "alias_reserved": len(reserved),
        })
    return result


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


# ============================================================
# registered_accounts
# ============================================================

def insert_account(
    *,
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    proxy_used: str | None = None,
    registration_ip: str | None = None,
    account_locale: str | None = None,
    account_country: str | None = None,
    account_locale_source: str | None = None,
    email_source: str | None = None,
    source_cdk: str | None = None,
    registration_password: str | None = None,
    twofa_status: str | None = None,
    twofa_error: str | None = None,
    extra: dict | None = None,
    codex_status: str | None = None,   # success / failed / skipped / missing
    codex_error: str | None = None,    # 失败原因（仅 codex_status=failed 时有意义）
) -> int:
    """插入或更新注册成功账号，返回本地文件中的 id。"""
    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        existing = _find_by_email(accounts, email)
        outlook_row = _find_by_email(outlook_rows, email)
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
        registration_driver = str((extra or {}).get("registration_driver") or "").strip().lower()
        registration_password = (
            str(registration_password)
            if registration_password is not None
            else str((extra or {}).get("registration_password") or "")
        )

        if existing is None:
            row_id = _next_id(accounts)
            row = {
                "id": row_id,
                "email": email,
                "created_at": _now(),
            }
            accounts.append(row)
        else:
            row = existing
            row_id = int(row["id"])

        row.update({
            "access_token": access_token,
            "totp_secret": totp_secret if totp_secret is not None else row.get("totp_secret"),
            "user_id": user_id if user_id is not None else row.get("user_id"),
            "user_name": user_name if user_name is not None else row.get("user_name"),
            "plan_type": plan_type if plan_type is not None else row.get("plan_type"),
            "expires_at": expires_at if expires_at is not None else row.get("expires_at"),
            "proxy_used": proxy_used if proxy_used is not None else row.get("proxy_used"),
            "registration_ip": (
                registration_ip if registration_ip is not None else row.get("registration_ip")
            ),
            "account_locale": account_locale if account_locale else row.get("account_locale"),
            "account_country": account_country if account_country else row.get("account_country"),
            "account_locale_source": (
                account_locale_source if account_locale_source else row.get("account_locale_source")
            ),
            "email_source": email_source if email_source is not None else row.get("email_source"),
            "registration_driver": (
                registration_driver
                if registration_driver
                else row.get("registration_driver")
            ),
            "source_cdk": source_cdk if source_cdk is not None else row.get("source_cdk"),
            "registration_password": (
                registration_password
                if registration_password
                else row.get("registration_password") or _registration_password(row)
            ),
            "twofa_status": twofa_status if twofa_status is not None else row.get("twofa_status"),
            "twofa_error": twofa_error if twofa_error is not None else row.get("twofa_error"),
            "extra_json": extra_json if extra_json is not None else row.get("extra_json"),
            "codex_status": codex_status if codex_status is not None else row.get("codex_status"),
            "codex_error": codex_error if codex_error is not None else row.get("codex_error"),
            "updated_at": _now(),
        })

        if outlook_row:
            row["password"] = outlook_row.get("password")
            row["client_id"] = outlook_row.get("client_id")
            row["refresh_token"] = outlook_row.get("refresh_token")
            row["original_email_line"] = _outlook_line(outlook_row)
            outlook_row["status"] = "used"
            outlook_row["used_at"] = outlook_row.get("used_at") or _now()
            outlook_row["registered_account_id"] = row_id
            outlook_row["access_token"] = access_token
            outlook_row["completed_at"] = _now()
            if totp_secret:
                outlook_row["totp_secret"] = totp_secret

        row["copy_line"] = _account_line(row)
        _save_accounts(accounts)
        _save_outlook(outlook_rows)
        return row_id


def update_account_2fa(
    acc_id: int | None = None,
    email: str | None = None,
    *,
    status: str,
    totp_secret: str | None = None,
    error: str | None = None,
) -> bool:
    """原子更新账号 2FA 字段，并同步账号导出文件。"""
    target_id = int(acc_id) if acc_id is not None else None
    target_email = str(email or "").strip().lower()
    with _LOCK:
        rows = _load_accounts()
        row = None
        if target_id is not None:
            row = next((item for item in rows if int(item.get("id") or 0) == target_id), None)
        if row is None and target_email:
            row = _find_by_email(rows, target_email)
        if row is None:
            return False
        now = _now()
        row["totp_secret"] = str(totp_secret or "") or None
        row["twofa_status"] = str(status or "failed").strip() or "failed"
        row["twofa_error"] = str(error or "").strip() or None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_access_token(
    acc_id: int | None = None,
    email: str | None = None,
    *,
    access_token: str,
) -> bool:
    """Atomically replace an account access token and synchronize exports."""
    token = str(access_token or "").strip()
    if not token:
        return False
    target_id = int(acc_id) if acc_id is not None else None
    target_email = str(email or "").strip().lower()
    with _LOCK:
        rows = _load_accounts()
        row = None
        if target_id is not None:
            row = next((item for item in rows if int(item.get("id") or 0) == target_id), None)
        if row is None and target_email:
            row = _find_by_email(rows, target_email)
        if row is None:
            return False
        row["access_token"] = token
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def mark_account_plan_login_pending(
    acc_id: int,
    *,
    trigger: str = "manual_import",
) -> bool:
    """Mark an account as waiting for credential login before plan check."""
    target_id = int(acc_id)
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == target_id), None)
        if row is None:
            return False
        now = _now()
        row["plan_check_status"] = "login_pending"
        row["plan_check_ok"] = None
        row["plan_check_trigger"] = str(trigger or "manual_import")
        row["plan_check_queued_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def update_account_login_credentials(
    acc_id: int,
    *,
    password: str,
    totp_secret: str,
) -> bool:
    """Persist credentials supplied for a tokenless account without replacing its other data."""
    target_id = int(acc_id)
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == target_id), None)
        if row is None:
            return False
        row["registration_password"] = str(password or "")
        row["totp_secret"] = str(totp_secret or "") or None
        row["twofa_status"] = "active"
        row["twofa_error"] = None
        row["codex_login_mode"] = "credentials"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True



def update_account_codex_status(email: str, codex_status: str, codex_error: str | None = None) -> bool:
    """
    单独更新某账号的 codex_status / codex_error（手动补跑 Codex 时用）。
    返回是否找到该账号。
    """
    with _LOCK:
        accounts = _load_accounts()
        row = _find_by_email(accounts, email)
        if row is None:
            return False
        is_deactivated = str(codex_status or "").strip().lower() == "deactivated"
        row["codex_status"] = codex_status
        row["codex_error"] = account_unusable_message("account_deactivated") if is_deactivated else codex_error
        if is_deactivated:
            # Codex 授权阶段判定为 deactivated，按账号废号处理，便于账号列表统一筛选。
            row["live_check_status"] = "deactivated"
            row["live_check_ok"] = False
            row["live_check_error"] = account_unusable_message("account_deactivated")
            row["live_checked_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_codex_agent(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 Codex Agent Token 生成任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("codex_agent_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "codex_agent_queued_at" if current_status == "queued" else "codex_agent_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (_local_now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["codex_agent_status"] = "queued"
        row["codex_agent_ok"] = False
        row["codex_agent_trigger"] = str(trigger or "manual")
        row["codex_agent_queued_at"] = now
        row["codex_agent_started_at"] = None
        row["codex_agent_completed_at"] = None
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_codex_agent_running(acc_id: int) -> bool:
    """把 Codex Agent Token 生成任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("codex_agent_status") not in {"queued", "running"}:
            return False
        row["codex_agent_status"] = "running"
        row["codex_agent_started_at"] = _now()
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "正在生成 Codex Agent Token"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_codex_agent(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 Codex Agent Token 生成结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["codex_agent_status"] = status
        row["codex_agent_ok"] = ok
        row["codex_agent_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["codex_agent_completed_at"] = _now()
        row["codex_agent_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["codex_agent_message"] = result.get("message")
        if result.get("agent_runtime_id") is not None:
            row["codex_agent_runtime_id"] = result.get("agent_runtime_id")
        if result.get("auth_path") is not None:
            row["codex_agent_auth_path"] = result.get("auth_path")
        if isinstance(result.get("auth_json"), dict):
            auth_json = result.get("auth_json")
            row["codex_agent_token"] = json.dumps(auth_json, ensure_ascii=False)
            agent_filename = f"codex-agent-{row.get('email') or acc_id!s}.json"
            stamp = _now()
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.execute(
                    "INSERT INTO codex_agent_accounts(account_id,email,filename,created_at,updated_at,payload) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET email=excluded.email, filename=excluded.filename, updated_at=excluded.updated_at, payload=excluded.payload",
                    (int(acc_id), str(row.get("email") or ""), agent_filename, stamp, stamp, json.dumps(auth_json, ensure_ascii=False)),
                )
                conn.commit()
            row.pop("codex_agent_auth_path", None)
        for _k in (
            "codex_agent_network_route",
            "codex_agent_proxy_mode",
            "codex_agent_proxy_used",
            "codex_agent_proxy_fallback_reason",
            "codex_agent_attempt_count",
            "codex_agent_max_attempts",
            "codex_agent_request_timeout",
            "codex_agent_sub2api_path",
            "codex_agent_sub2api_url",
            "codex_agent_sub2api_mode",
            "codex_agent_sub2api_total",
        ):
            src_key = _k.replace("codex_agent_", "", 1)
            if result.get(src_key) is not None:
                row[_k] = result.get(src_key)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def get_codex_agent_credential(acc_id: int) -> tuple[str, str] | None:
    """从 SQLite 获取 Agent 凭证，返回 JSON 文本和下载文件名。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        row = conn.execute("SELECT filename, payload FROM codex_agent_accounts WHERE account_id=?", (int(acc_id),)).fetchone()
    if not row:
        return None
    return json.dumps(json.loads(row["payload"]), ensure_ascii=False, indent=2) + "\n", row["filename"]


def recover_interrupted_codex_agents() -> int:
    """服务启动时恢复上次进程中断的 Codex Agent 任务状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("codex_agent_status") not in {"queued", "running"}:
                continue
            row["codex_agent_status"] = "failed"
            row["codex_agent_ok"] = False
            row["codex_agent_error"] = "WebUI 重启导致 Codex Agent Token 任务中断，请重新生成"
            row["codex_agent_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
) -> bool:
    """原子占用账号的套餐查询；已有未超时查询时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        current_status = row.get("plan_check_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "plan_check_queued_at" if current_status == "queued" else "plan_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (_local_now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = _now()
        row["plan_check_status"] = "queued"
        row["plan_check_trigger"] = str(trigger or "manual")
        row["plan_check_queued_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_plan_check_running(acc_id: int) -> bool:
    """把已排队的套餐查询标记为执行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("plan_check_status") not in {"queued", "running"}:
            return False
        row["plan_check_status"] = "running"
        row["plan_check_started_at"] = _now()
        row["plan_check_error"] = None
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_plan_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("plan_check_status") not in {"queued", "running"}:
                continue
            row["plan_check_status"] = "failed"
            row["plan_check_ok"] = False
            row["plan_check_error"] = "WebUI 重启导致套餐查询中断，请重新查询"
            row["plan_check_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def update_account_plan_check(acc_id: int | None = None, email: str | None = None, result: dict | None = None) -> bool:
    """更新账号套餐/Plus 试用资格查询结果。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        ok = bool(result.get("ok"))
        row["plan_check_status"] = "success" if ok else "failed"
        row["plan_check_ok"] = ok
        row["plan_checked_at"] = result.get("checked_at") or _now()
        row["plan_check_completed_at"] = _now()
        row["plan_check_http_status"] = result.get("http_status")
        row["plan_check_error"] = None if ok else result.get("error")

        if result.get("account_id"):
            row["account_id"] = result.get("account_id")
        # 查询失败只更新本次错误和网络信息，不覆盖上一次成功拿到的套餐、
        # 试用资格、优惠及有效期，避免临时网络故障把真实权益清空。
        if ok:
            if result.get("current_plan_type"):
                row["current_plan_type"] = result.get("current_plan_type")
                row["plan_type"] = result.get("current_plan_type")
            if result.get("subscription_plan") is not None:
                row["subscription_plan"] = result.get("subscription_plan")
            if result.get("has_active_subscription") is not None:
                row["has_active_subscription"] = bool(result.get("has_active_subscription"))
            if result.get("expires_at") is not None:
                row["plan_expires_at"] = result.get("expires_at")
            if result.get("renews_at") is not None:
                row["plan_renews_at"] = result.get("renews_at")
            if result.get("cancels_at") is not None:
                row["plan_cancels_at"] = result.get("cancels_at")
            if result.get("billing_period") is not None:
                row["billing_period"] = result.get("billing_period")
            if result.get("billing_currency") is not None:
                row["billing_currency"] = result.get("billing_currency")
            if result.get("is_delinquent") is not None:
                row["is_delinquent"] = bool(result.get("is_delinquent"))
            for _k in (
                "discount_type",
                "discount_amount",
                "discount_duration_num_periods",
                "discount_expires_at",
                "discount_cancellation_policy",
                "discount_promo_campaign_id",
                "last_purchase_origin_platform",
                "last_will_renew",
            ):
                if result.get(_k) is not None:
                    row[_k] = result.get(_k)

            row["plus_trial_eligible"] = bool(result.get("plus_trial_eligible"))
            row["plus_trial_campaign_id"] = result.get("plus_trial_campaign_id")
            row["plus_trial_title"] = result.get("plus_trial_title")
            row["plus_trial_discount_percentage"] = result.get("plus_trial_discount_percentage")
            row["plus_trial_duration_num_periods"] = result.get("plus_trial_duration_num_periods")
            row["plus_trial_duration_period"] = result.get("plus_trial_duration_period")
            row["eligible_offer_ids"] = result.get("eligible_offer_ids") or []
            row["plan_last_success_at"] = result.get("checked_at") or _now()
            row["plan_last_success_result_json"] = json.dumps(result, ensure_ascii=False)

            if (
                str(result.get("current_plan_type") or "").strip().lower() == "free"
                and result.get("plus_trial_eligible") is False
                and row.get("free_plus_exported_at")
            ):
                # A fresh plan check confirms that a previously exported account no
                # longer has the Plus trial; clear the stale export marker and archive flag.
                row["free_plus_exported_at"] = None
                row["free_plus_export_count"] = 0
                row["free_plus_export_format"] = None
                row["free_plus_export_source"] = None
                if row.get("archived"):
                    row["archived"] = False
                    row["archived_at"] = None
        row["plan_check_proxy_mode"] = result.get("proxy_mode")
        row["plan_check_network_route"] = result.get("network_route")
        row["plan_check_proxy_used"] = result.get("proxy_used")
        row["plan_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        row["token_expired"] = result.get("token_expired")
        row["token_expires_at"] = result.get("token_expires_at")
        row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_extract(acc_id: int, trigger: str = "manual", link_type: str = "pix") -> bool:
    """原子占用账号提链任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("extract_link_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "extract_link_queued_at" if current_status == "queued" else "extract_link_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (_local_now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["extract_link_status"] = "queued"
        row["extract_link_ok"] = False
        row["extract_link_trigger"] = str(trigger or "manual")
        row["extract_link_type"] = str(link_type or "pix").lower()
        row["extract_link_queued_at"] = now
        row["extract_link_started_at"] = None
        row["extract_link_completed_at"] = None
        row["extract_link_error"] = None
        row["extract_link_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_extract_running(acc_id: int) -> bool:
    """把提链任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("extract_link_status") not in {"queued", "running"}:
            return False
        row["extract_link_status"] = "running"
        row["extract_link_started_at"] = _now()
        row["extract_link_error"] = None
        row["extract_link_message"] = "任务运行中"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_extract(acc_id: int, result: dict | None = None) -> bool:
    """更新账号提链任务结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["extract_link_status"] = status
        row["extract_link_ok"] = ok
        row["extract_link_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["extract_link_completed_at"] = _now()
        row["extract_link_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["extract_link_message"] = result.get("message")
        if result.get("job_id") is not None:
            row["extract_link_job_id"] = result.get("job_id")
        if result.get("link_type") is not None:
            row["extract_link_type"] = result.get("link_type")
        if result.get("cdk_remaining") is not None:
            row["extract_link_cdk_remaining"] = result.get("cdk_remaining")
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if payload:
            row["extract_link_long_url"] = payload.get("long_url")
            row["extract_link_copy_paste"] = payload.get("copy_paste")
            row["extract_link_image_url_png"] = payload.get("image_url_png")
            row["extract_link_image_url_svg"] = payload.get("image_url_svg")
            row["extract_link_payment_method"] = payload.get("payment_method")
            row["extract_link_payment_link_type"] = payload.get("payment_link_type")
            row["extract_link_expires_at"] = payload.get("expires_at")
            if payload.get("cdk_remaining") is not None:
                row["extract_link_cdk_remaining"] = payload.get("cdk_remaining")
            row["extract_link_result_json"] = json.dumps(payload, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_extract_links() -> int:
    """服务启动时恢复上次进程中断的提链状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("extract_link_status") not in {"queued", "running"}:
                continue
            row["extract_link_status"] = "failed"
            row["extract_link_ok"] = False
            row["extract_link_error"] = "WebUI 重启导致提链任务中断，请重新提链"
            row["extract_link_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def _account_matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except (AttributeError, TypeError, ValueError):
        return False


def _parse_iso_dt(value: str | None, end_of_day: bool = False) -> datetime | None:
    """宽松解析 ISO 日期/时间字符串；支持 YYYY-MM-DD 或完整 ISO；解析失败返回 None。

    end_of_day=True 时，纯日期（YYYY-MM-DD）按当天 23:59:59.999999 解析，
    用于 date_to 过滤（保证包含截止当天）；完整时间串原样返回。
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10 and text[4] == "-":
            if end_of_day:
                return datetime.fromisoformat(text + "T23:59:59.999999")
            return datetime.fromisoformat(text + "T00:00:00")
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _matches_codex_status_filter(row: dict, codex_filter: str | None) -> bool:
    codex_filter = str(codex_filter or "").strip().lower()
    if not codex_filter:
        return True
    status = str(row.get("codex_status") or "").strip().lower()
    live_status = str(row.get("live_check_status") or "").strip().lower()
    if codex_filter in {"all", "*"}:
        return True
    if codex_filter == "deactivated":
        return live_status == "deactivated"
    return status == codex_filter


def _account_matches_twofa_filter(row: dict, twofa_filter: str | None = None) -> bool:
    """按 2FA 设置状态过滤账号；兼容没有显式状态的旧账号。"""
    value = str(twofa_filter or "").strip().lower()
    if not value or value in {"all", "any", "*"}:
        return True
    status = str(row.get("twofa_status") or "").strip().lower()
    if not status:
        status = "active" if row.get("totp_secret") else "disabled"
    aliases = {
        "enabled": "active",
        "on": "active",
        "success": "active",
        "failure": "failed",
        "error": "failed",
        "processing": "pending",
        "not_enabled": "disabled",
        "none": "disabled",
        "未启用": "disabled",
        "未设置": "disabled",
        "失败": "failed",
        "已启用": "active",
        "处理中": "pending",
    }
    return status == aliases.get(value, value)


def _matches_totp_status_filter(row: dict, totp_filter: str | None = None) -> bool:
    """按 TOTP 密钥及设置任务状态过滤账号。"""
    value = str(totp_filter or "").strip().lower()
    if not value or value in {"all", "*"}:
        return True
    enabled = bool(str(row.get("totp_secret") or "").strip())
    setup_status = str(row.get("totp_setup_status") or "").strip().lower()
    if value in {"enabled", "on", "active"}:
        return enabled
    if value in {"disabled", "off", "not_enabled", "unset"}:
        return not enabled
    if value in {"pending", "setup", "setting", "queued", "running"}:
        return setup_status in {"queued", "running"}
    if value in {"failed", "stopped"}:
        return setup_status == value
    return setup_status == value


def _account_matches_locale_filter(row: dict, locale_filter: str | None = None) -> bool:
    value = str(locale_filter or "").strip().lower()
    if not value or value in {"all", "any", "*"}:
        return True
    if value in {"unknown", "unresolved", "未识别"}:
        return not str(row.get("account_locale") or row.get("account_country") or "").strip()
    candidates = {
        str(row.get("account_locale") or "").strip().lower(),
        str(row.get("account_country") or "").strip().lower(),
    }
    return value in candidates


def _filtered_decorated_accounts(
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    free_plus_export_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    twofa_filter: str | None = None,
    account_locale_filter: str | None = None,
    email_source_filter: str | None = None,
    email_domain_filter: str | None = None,
    totp_filter: str | None = None,
) -> list[dict]:
    rows = _load_accounts()
    if archived in (True, "1", "true", "yes", "only"):
        rows = [r for r in rows if bool(r.get("archived"))]
    elif archived in ("all", "include"):
        pass
    else:
        rows = [r for r in rows if not bool(r.get("archived"))]
    decorated = [_decorate_account(r) for r in rows]
    decorated = [r for r in decorated if _account_matches_plan_filter(r, plan_filter)]
    decorated = [r for r in decorated if _matches_codex_status_filter(r, codex_filter)]
    decorated = [r for r in decorated if _account_matches_twofa_filter(r, twofa_filter)]
    decorated = [r for r in decorated if _matches_totp_status_filter(r, totp_filter)]
    decorated = [r for r in decorated if _account_matches_locale_filter(r, account_locale_filter)]
    decorated = [r for r in decorated if _account_matches_email_source_filter(r, email_source_filter)]
    decorated = [r for r in decorated if _account_matches_email_domain_filter(r, email_domain_filter)]
    decorated = [r for r in decorated if _account_matches_free_plus_export_filter(r, free_plus_export_filter)]
    decorated = [r for r in decorated if _account_matches_query(r, q)]
    # 按创建时间筛选（date_from/date_to 为 ISO 字符串或 YYYY-MM-DD）
    if date_from or date_to:
        d_from = _parse_iso_dt(date_from)
        d_to = _parse_iso_dt(date_to, end_of_day=True)
        if d_from or d_to:
            filtered = []
            for r in decorated:
                ct = _parse_iso_dt(str(r.get("created_at") or ""))
                if ct is None:
                    continue
                if d_from and ct < d_from:
                    continue
                if d_to and ct > d_to:
                    continue
                filtered.append(r)
            decorated = filtered
    return sorted(decorated, key=lambda x: int(x.get("id") or 0), reverse=True)


def list_account_plan_check_statuses(
    limit: int = 5000,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    free_plus_export_filter: str | None = None,
    twofa_filter: str | None = None,
    account_locale_filter: str | None = None,
    email_source_filter: str | None = None,
    email_domain_filter: str | None = None,
    totp_filter: str | None = None,
) -> dict:
    """返回不含 Token/邮箱密码的套餐查询轻量状态快照。"""
    fields = (
        "id", "email", "email_domain", "archived",
        "account_locale", "account_country", "account_locale_source",
        "twofa_status", "twofa_error",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "plan_check_status", "plan_check_ok", "plan_check_error",
        "plan_check_trigger", "plan_check_queued_at", "plan_check_started_at",
        "plan_check_completed_at", "plan_checked_at", "plan_last_success_at",
        "plan_check_network_route", "plan_check_proxy_used", "plan_check_proxy_fallback_reason",
        "live_check_proxy_used", "live_check_fingerprint_text",
        "expires_at", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "extract_link_status", "extract_link_ok", "extract_link_type",
        "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste",
        "extract_link_image_url_png", "extract_link_image_url_svg",
        "extract_link_expires_at",
        "free_plus_exported_at", "free_plus_export_count", "free_plus_export_format", "free_plus_export_source",
        "codex_status", "codex_error",
        "codex_agent_status", "codex_agent_message",
        "codex_agent_runtime_id", "codex_agent_sub2api_url",
        "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
        "totp_setup_status", "totp_setup_ok", "totp_setup_error",
        "totp_setup_message", "totp_setup_trigger", "totp_setup_queued_at",
        "totp_setup_started_at", "totp_setup_completed_at", "totp_setup_checked_at",
    )
    with _LOCK:
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        extended_filters = any(
            str(value or "").strip()
            for value in (twofa_filter, account_locale_filter, email_source_filter, email_domain_filter, totp_filter)
        )
        if extended_filters:
            all_rows = _filtered_decorated_accounts(
                archived=archived,
                plan_filter=plan_filter,
                codex_filter=codex_filter,
                q=q,
                free_plus_export_filter=free_plus_export_filter,
                date_from=date_from,
                date_to=date_to,
                twofa_filter=twofa_filter,
                account_locale_filter=account_locale_filter,
                email_source_filter=email_source_filter,
                email_domain_filter=email_domain_filter,
                totp_filter=totp_filter,
            )
            total = len(all_rows)
            latest = max((str(row.get("updated_at") or "") for row in all_rows), default="")
            candidates = all_rows[offset: offset + limit]
        else:
            extra_where, extra_params = _account_filter_sql(
                plan_filter,
                codex_filter,
                free_plus_export_filter,
                totp_filter,
            )
            candidates, total, latest = _query_collection_page(
                "accounts",
                archived=archived,
                q=q,
                date_from=date_from,
                date_to=date_to,
                extra_where=extra_where,
                extra_params=extra_params,
                limit=limit,
                offset=offset,
            )
        rows = [_decorate_account(row) for row in candidates]
        items = []
        for row in rows:
            item = {"id": row.get("id"), "email": row.get("email")}
            for key in fields:
                value = row.get(key)
                if key in ("id", "email"):
                    continue
                if value is not None and value != "":
                    item[key] = value
            item["totp_enabled"] = bool(str(row.get("totp_secret") or "").strip())
            plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
            if not any(x in plan for x in ("plus", "pro", "team", "go")):
                for expire_key in ("expires_at", "plan_expires_at", "plan_renews_at", "renews_at"):
                    item.pop(expire_key, None)
            item["codex_agent_has_token"] = bool(str(row.get("codex_agent_token") or "").strip())
            item["has_access_token"] = bool(str(row.get("access_token") or "").strip())
            items.append(item)
        # updated_at 目前只有秒级精度；一次快速查询可能在同一秒内完成
        # queued -> running -> success/failed，导致 revision 不变，前端跳过合并状态，
        # 页面就会一直停在“查询中”。把轻量状态本身纳入签名，保证状态变化可被轮询发现。
        revision_payload = json.dumps(
            [
                {
                    "id": row.get("id"),
                    "updated_at": row.get("updated_at"),
                    "plan_check_status": row.get("plan_check_status"),
                    "plan_check_ok": row.get("plan_check_ok"),
                    "plan_check_error": row.get("plan_check_error"),
                    "current_plan_type": row.get("current_plan_type"),
                    "plan_type": row.get("plan_type"),
                    "plus_trial_eligible": row.get("plus_trial_eligible"),
                    "twofa_status": row.get("twofa_status") or ("active" if row.get("totp_secret") else "disabled"),
                    "twofa_error": row.get("twofa_error"),
                    "extract_link_status": row.get("extract_link_status"),
                    "codex_status": row.get("codex_status"),
                    "codex_agent_status": row.get("codex_agent_status"),
                    "totp_setup_status": row.get("totp_setup_status"),
                    "totp_setup_ok": row.get("totp_setup_ok"),
                    "totp_setup_error": row.get("totp_setup_error"),
                    "totp_setup_message": row.get("totp_setup_message"),
                    "totp_setup_checked_at": row.get("totp_setup_checked_at"),
                    "totp_setup_started_at": row.get("totp_setup_started_at"),
                    "totp_setup_completed_at": row.get("totp_setup_completed_at"),
                    "totp_enabled": bool(str(row.get("totp_secret") or "").strip()),
                }
                for row in rows
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision_sig = hashlib.sha1(revision_payload.encode("utf-8")).hexdigest()[:12]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}:{revision_sig}"}


def list_accounts(
    limit: int = 500,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    free_plus_export_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    twofa_filter: str | None = None,
    account_locale_filter: str | None = None,
    email_source_filter: str | None = None,
    email_domain_filter: str | None = None,
    totp_filter: str | None = None,
) -> list[dict]:
    with _LOCK:
        rows = _filtered_decorated_accounts(archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, free_plus_export_filter=free_plus_export_filter, date_from=date_from, date_to=date_to, twofa_filter=twofa_filter, account_locale_filter=account_locale_filter, email_source_filter=email_source_filter, email_domain_filter=email_domain_filter, totp_filter=totp_filter)
        return rows[max(0, int(offset or 0)): max(0, int(offset or 0)) + max(1, int(limit))]


def list_accounts_page(
    limit: int = 50,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    codex_filter: str | None = None,
    q: str | None = None,
    free_plus_export_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    twofa_filter: str | None = None,
    account_locale_filter: str | None = None,
    email_source_filter: str | None = None,
    email_domain_filter: str | None = None,
    totp_filter: str | None = None,
) -> dict:
    with _LOCK:
        rows = _filtered_decorated_accounts(archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, free_plus_export_filter=free_plus_export_filter, date_from=date_from, date_to=date_to, twofa_filter=twofa_filter, account_locale_filter=account_locale_filter, email_source_filter=email_source_filter, email_domain_filter=email_domain_filter, totp_filter=totp_filter)
        total = len(rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        items = rows[offset: offset + limit]
        latest = max((str(row.get("updated_at") or "") for row in rows), default="")
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}"}


def get_account(acc_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_accounts() if int(r.get("id") or 0) == int(acc_id)), None)
        return _decorate_account(row) if row else None


def get_account_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_accounts(), email)
        return _decorate_account(row) if row else None


def save_personal_info_change_batch(
    batch_id: str,
    mode: str,
    results: list[dict],
) -> dict:
    """Persist one personal-information run for later display and export."""
    normalized_id = str(batch_id or "").strip()
    normalized_mode = str(mode or "").strip().lower()
    if not normalized_id:
        raise ValueError("personal information batch_id is required")
    if normalized_mode not in {"email", "twofa"}:
        raise ValueError("personal information mode is invalid")

    safe_results: list[dict] = []
    for raw in results if isinstance(results, list) else []:
        if not isinstance(raw, dict):
            continue
        account_id = None
        try:
            candidate_id = int(raw.get("account_id"))
            if candidate_id > 0:
                account_id = candidate_id
        except (TypeError, ValueError):
            pass
        entry = {
            "account_id": account_id,
            "email": str(raw.get("email") or "").strip(),
            "old_email": str(raw.get("old_email") or "").strip(),
            "new_email": str(raw.get("new_email") or "").strip(),
            "change_status": str(raw.get("change_status") or "failed").strip(),
            "error": str(raw.get("error") or "").strip()[:500],
            "warning": str(raw.get("warning") or "").strip()[:500],
        }
        safe_results.append(entry)

    batch = {
        "batch_id": normalized_id,
        "mode": normalized_mode,
        "created_at": _now(),
        "results": safe_results,
        "exportable_count": sum(
            1 for item in safe_results
            if item["change_status"] == "success" and item["account_id"] is not None
        ),
    }
    with _LOCK:
        state = app_state_db.get_named_document(_PERSONAL_INFO_CHANGE_STATE_KEY, {})
        if not isinstance(state, dict):
            state = {}
        batches = state.get("batches")
        if not isinstance(batches, dict):
            batches = {}
        batches[normalized_id] = batch
        if len(batches) > _MAX_PERSONAL_INFO_CHANGE_BATCHES:
            ordered_ids = sorted(
                batches,
                key=lambda value: str((batches[value] or {}).get("created_at") or ""),
                reverse=True,
            )
            batches = {value: batches[value] for value in ordered_ids[:_MAX_PERSONAL_INFO_CHANGE_BATCHES]}
        state = {"latest_batch_id": normalized_id, "batches": batches}
        app_state_db.set_named_document(_PERSONAL_INFO_CHANGE_STATE_KEY, state)
    return batch


def get_personal_info_change_batch(batch_id: str | None = None) -> dict | None:
    """Return a persisted personal-information batch, defaulting to the latest."""
    requested_id = str(batch_id or "").strip()
    with _LOCK:
        state = app_state_db.get_named_document(_PERSONAL_INFO_CHANGE_STATE_KEY, {})
        if not isinstance(state, dict):
            return None
        batches = state.get("batches")
        if not isinstance(batches, dict):
            return None
        selected_id = requested_id or str(state.get("latest_batch_id") or "").strip()
        batch = batches.get(selected_id)
        return dict(batch) if isinstance(batch, dict) else None


def get_personal_info_change_export_rows(batch_id: str | None = None) -> list[dict]:
    """Resolve successful account rows for a persisted personal-information batch."""
    batch = get_personal_info_change_batch(batch_id)
    if not batch:
        return []
    rows: list[dict] = []
    for result in batch.get("results") or []:
        if not isinstance(result, dict) or result.get("change_status") != "success":
            continue
        account_id = result.get("account_id")
        if account_id is None:
            continue
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            continue
        row = get_account(account_id)
        if row is not None:
            rows.append(row)
    return rows


def update_account_email(old_email: str, new_email: str) -> bool:
    """Atomically replace an account email and refresh its persisted export line."""
    old_key = str(old_email or "").strip().casefold()
    new_value = str(new_email or "").strip()
    new_key = new_value.casefold()
    if not old_key or not new_key:
        raise ValueError("account email is required")
    if old_key == new_key:
        raise ValueError("target email must differ from source email")
    with _LOCK:
        rows = _load_accounts()
        row = _find_by_email(rows, old_key)
        if row is None:
            return False
        if any(str(item.get("email") or "").strip().casefold() == new_key for item in rows):
            raise ValueError("target email already exists")
        row["previous_email"] = str(row.get("email") or old_email)
        row["email"] = new_value
        row["original_email_line"] = new_value
        row["updated_at"] = _now()
        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def update_account_note(acc_id: int, note: str) -> bool:
    """更新单个已注册账号备注。note 为空字符串时表示清空备注。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["note"] = str(note or "")
        row["note_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_liveness(acc_id: int, result: dict | None = None) -> bool:
    """写回账号查活结果；成功时同步刷新最新 access_token 和账号基础信息。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False

        now = _now()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("live" if ok else "failed"))
        row["live_check_status"] = status
        row["live_check_ok"] = ok
        row["live_checked_at"] = result.get("checked_at") or now
        row["live_check_error"] = None if ok else (
            account_unusable_message("account_deactivated")
            if status == "deactivated"
            else result.get("error")
        )
        row["updated_at"] = now

        if ok:
            token = str(result.get("access_token") or "").strip()
            if token:
                row["access_token"] = token
            session = result.get("session") or {}
            user = session.get("user") or {}
            account = session.get("account") or {}
            if user.get("id"):
                row["user_id"] = user.get("id")
            if user.get("name") is not None:
                row["user_name"] = user.get("name")
            if account.get("planType"):
                row["plan_type"] = account.get("planType")
            if session.get("expires"):
                row["expires_at"] = session.get("expires")
            row["live_check_proxy_used"] = result.get("proxy_used") or row.get("live_check_proxy_used")
            row["live_check_fingerprint_text"] = result.get("fingerprint_text") or row.get("live_check_fingerprint_text")
            if result.get("fingerprint"):
                row["live_check_fingerprint"] = result.get("fingerprint")
            row["live_check_error"] = None

        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def claim_account_totp_setup(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 2FA 设置任务；已有未超时任务时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("totp_setup_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "totp_setup_queued_at" if current_status == "queued" else "totp_setup_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (_local_now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["totp_setup_status"] = "queued"
        row["totp_setup_ok"] = False
        row["totp_setup_trigger"] = str(trigger or "manual")
        row["totp_setup_queued_at"] = now
        row["totp_setup_started_at"] = None
        row["totp_setup_completed_at"] = None
        row["totp_setup_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def mark_account_totp_setup_running(acc_id: int) -> bool:
    """把 2FA 设置任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("totp_setup_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["totp_setup_status"] = "running"
        row["totp_setup_started_at"] = now
        row["totp_setup_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_totp_secret(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 2FA/TOTP 设置结果。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["totp_setup_status"] = status
        row["totp_setup_ok"] = ok
        row["totp_setup_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["totp_setup_completed_at"] = _now()
        row["totp_setup_error"] = None if ok or status == "running" else result.get("error")
        secret = str(result.get("totp_secret") or "").strip()
        if ok and secret:
            row["totp_secret"] = secret
        if result.get("message") is not None:
            row["totp_setup_message"] = result.get("message")
        row["copy_line"] = _account_line(row)
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True


def recover_interrupted_totp_setups() -> int:
    """服务启动时恢复上次进程中断的 2FA 设置状态。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("totp_setup_status") not in {"queued", "running"}:
                continue
            row["totp_setup_status"] = "failed"
            row["totp_setup_ok"] = False
            row["totp_setup_error"] = "WebUI 重启导致 2FA 设置中断，请重新开启"
            row["totp_setup_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def claim_account_live_check(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号查活任务；已有 queued/running 时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if row.get("live_check_status") in {"queued", "running"}:
            try:
                stamp_key = "live_check_queued_at" if row.get("live_check_status") == "queued" else "live_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if row.get("live_check_status") == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (_local_now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["live_check_status"] = "queued"
        row["live_check_ok"] = False
        row["live_check_trigger"] = str(trigger or "manual")
        row["live_check_queued_at"] = now
        row["live_check_started_at"] = None
        row["live_checked_at"] = None
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def recover_interrupted_live_checks() -> int:
    """服务启动时恢复上次进程中断的查活状态，避免 queued/running 卡死。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("live_check_status") not in {"queued", "running"}:
                continue
            row["live_check_status"] = "failed"
            row["live_check_ok"] = False
            row["live_check_error"] = "WebUI 重启或任务异常中断，请重新查活"
            row["live_checked_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def mark_account_live_check_running(acc_id: int) -> bool:
    """把账号查活任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("live_check_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["live_check_status"] = "running"
        row["live_check_started_at"] = now
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_accounts_note(account_ids: list[int] | None, note: str) -> tuple[list[dict], list[dict]]:
    """
    批量更新已注册账号备注。
    返回 (updated, skipped)，updated/skipped 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        text = str(note or "")
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["note"] = text
            row["note_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "note": text, "note_updated_at": now})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def mark_accounts_free_plus_exported(
    account_ids: list[int],
    *,
    format_name: str,
    source: str = "free_plus_txt",
) -> tuple[list[dict], list[dict]]:
    """记录 Free Plus 导出并归档；普通手动归档不会调用此入口。"""
    ids = {int(item) for item in account_ids}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        targets = [row for row in rows if int(row.get("id") or 0) in ids]
        missing_ids = ids - {int(row.get("id") or 0) for row in targets}
        invalid: list[dict] = []
        for row in targets:
            row_id = int(row.get("id") or 0)
            if not _account_matches_plan_filter(row, "free_plus"):
                invalid.append({"id": row_id, "email": row.get("email"), "reason": "不是可用 Plus 试用账号"})
            elif row.get("free_plus_exported_at"):
                invalid.append({"id": row_id, "email": row.get("email"), "reason": "已导出"})
        if missing_ids:
            invalid.extend({"id": item, "reason": "账号不存在"} for item in missing_ids)
        if invalid:
            return [], invalid
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            seen_ids.add(row_id)
            row["free_plus_exported_at"] = now
            row["free_plus_export_count"] = int(row.get("free_plus_export_count") or 0) + 1
            row["free_plus_export_format"] = _normalize_account_line_format(format_name)
            row["free_plus_export_source"] = str(source or "free_plus_txt")
            row["archived"] = True
            row["archived_at"] = now
            row["updated_at"] = now
            updated.append({
                "id": row_id,
                "email": row.get("email"),
                "free_plus_exported_at": now,
                "free_plus_export_count": row["free_plus_export_count"],
                "archived": True,
            })
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def set_accounts_free_plus_export_state(
    account_ids: list[int],
    *,
    exported: bool,
) -> tuple[list[dict], list[dict]]:
    """手动校准历史导出状态；不改变账号的归档状态。"""
    ids = {int(item) for item in account_ids}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            seen_ids.add(row_id)
            if exported:
                row["free_plus_exported_at"] = row.get("free_plus_exported_at") or now
                row["free_plus_export_count"] = max(1, int(row.get("free_plus_export_count") or 0))
                row["free_plus_export_source"] = "manual"
            else:
                row["free_plus_exported_at"] = None
                row["free_plus_export_count"] = 0
                row["free_plus_export_format"] = None
                row["free_plus_export_source"] = None
            row["updated_at"] = now
            updated.append({
                "id": row_id,
                "email": row.get("email"),
                "free_plus_exported_at": row.get("free_plus_exported_at"),
                "free_plus_export_count": row.get("free_plus_export_count") or 0,
                "archived": bool(row.get("archived")),
            })
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def archive_account(acc_id: int, archived: bool = True) -> bool:
    """归档/取消归档单个已注册账号。归档不会删除 token，只影响默认账号列表查询。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["archived"] = bool(archived)
        row["archived_at"] = now if archived else None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def archive_accounts(account_ids: list[int] | None, archived: bool = True) -> tuple[list[dict], list[dict]]:
    """批量归档/取消归档账号。返回 (updated, skipped)。"""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["archived"] = bool(archived)
            row["archived_at"] = now if archived else None
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "archived": bool(archived), "archived_at": row.get("archived_at")})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def count_accounts() -> int:
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])


def delete_account(acc_id: int | None = None, email: str | None = None) -> bool:
    """从 SQLite 删除一个已注册账号记录，并清理关联的 Agent 凭证。"""
    with _LOCK:
        rows = _load_accounts()
        target_email = (email or "").lower()
        new_rows = []
        deleted_ids = []
        deleted = False
        for row in rows:
            match_id = acc_id is not None and int(row.get("id") or 0) == int(acc_id)
            match_email = bool(target_email) and (row.get("email") or "").lower() == target_email
            if match_id or match_email:
                deleted = True
                deleted_ids.append(int(row.get("id") or 0))
                continue
            new_rows.append(row)
        if not deleted:
            return False
        _save_accounts(new_rows, allow_delete=True)
        if deleted_ids:
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.executemany("DELETE FROM codex_agent_accounts WHERE account_id=?", [(x,) for x in deleted_ids])
                conn.commit()
        return True


def delete_accounts(account_ids: list[int] | None = None, emails: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """
    批量删除已注册账号。
    返回 (deleted, skipped)，deleted 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().isdigit()}
    email_set = {(e or "").lower() for e in (emails or []) if e}
    deleted: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        new_rows = []
        seen_ids: set[int] = set()
        seen_emails: set[str] = set()
        for row in rows:
            row_id = int(row.get("id") or 0)
            row_email = (row.get("email") or "").lower()
            if row_id in ids or row_email in email_set:
                deleted.append({"id": row_id, "email": row.get("email")})
                seen_ids.add(row_id)
                seen_emails.add(row_email)
                continue
            new_rows.append(row)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        for item in email_set - seen_emails:
            skipped.append({"email": item, "reason": "账号不存在"})
        if deleted:
            _save_accounts(new_rows, allow_delete=True)
            _ensure_sqlite()
            with closing(_sqlite_conn()) as conn:
                conn.executemany("DELETE FROM codex_agent_accounts WHERE account_id=?", [(x["id"],) for x in deleted])
                conn.commit()
    return deleted, skipped


# ============================================================
# outlook_pool
# ============================================================

def import_outlook_accounts(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。
    records 元素：{email, password, client_id, refresh_token}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_outlook()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": (raw.get("password") or "").strip(),
                "client_id": (raw.get("client_id") or raw.get("clientId") or "").strip(),
                "refresh_token": (raw.get("refresh_token") or raw.get("refreshToken") or "").strip(),
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _outlook_line(row)
            rows.append(row)
            inserted += 1
        _save_outlook(rows)
        return inserted, skipped


def import_registered_email_accounts(records: list[dict], source: str | None) -> tuple[int, int]:
    """
    把邮箱素材直接导入为“已注册成功账号”，用于跳过注册、直接在账号页补跑 Codex 授权。

    source:
      - outlook: records 元素 {email,password,client_id,refresh_token[,access_token,totp_secret]}
      - generic_api: records 元素 {email,code_url[,access_token,totp_secret]}

    返回 (新增账号数, 跳过数)。已存在账号会跳过；邮箱池中已存在的素材会复用并标记 used。
    """
    source = (source or "").strip().lower()
    if source not in ("outlook", "generic_api"):
        raise ValueError("source 必须显式传入 outlook / generic_api")

    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        inserted = skipped = 0

        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            original_line = email
            pool_row = None

            if source == "generic_api":
                code_url = (raw.get("code_url") or raw.get("url") or "").strip()
                if not code_url:
                    skipped += 1
                    continue
                pool_row = _find_by_email(generic_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(generic_rows),
                        "email": email,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    generic_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _generic_api_email_line(pool_row)
                original_line = _generic_api_email_line(pool_row)
            else:
                password = (raw.get("password") or "").strip()
                client_id = (raw.get("client_id") or raw.get("clientId") or "").strip()
                refresh_token = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                if not (password and client_id and refresh_token):
                    skipped += 1
                    continue
                pool_row = _find_by_email(outlook_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(outlook_rows),
                        "email": email,
                        "password": password,
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    outlook_rows.append(pool_row)
                else:
                    pool_row["password"] = password or pool_row.get("password")
                    pool_row["client_id"] = client_id or pool_row.get("client_id")
                    pool_row["refresh_token"] = refresh_token or pool_row.get("refresh_token")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _outlook_line(pool_row)
                original_line = _outlook_line(pool_row)

            row_id = _next_id(accounts)
            access_token = (raw.get("access_token") or raw.get("token") or "").strip()
            totp_secret = (raw.get("totp_secret") or raw.get("totp") or "").strip() or None
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_id": raw.get("user_id"),
                "user_name": raw.get("user_name") or "Imported Account",
                "plan_type": raw.get("plan_type"),
                "expires_at": raw.get("expires_at"),
                "device_id": raw.get("device_id"),
                "registration_password": (raw.get("registration_password") or raw.get("account_password") or "").strip(),
                "proxy_used": raw.get("proxy_used"),
                "email_source": source,
                "extra_json": json.dumps({"imported_registered": True}, ensure_ascii=False),
                "codex_status": raw.get("codex_status") or "",
                "codex_error": raw.get("codex_error"),
                "updated_at": now,
                "original_email_line": original_line,
            }
            if source == "outlook":
                account["password"] = pool_row.get("password")
                account["client_id"] = pool_row.get("client_id")
                account["refresh_token"] = pool_row.get("refresh_token")
            account["copy_line"] = _account_line(account)
            accounts.append(account)

            pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = access_token
            if totp_secret:
                pool_row["totp_secret"] = totp_secret
            inserted += 1

        _save_outlook(outlook_rows)
        _save_generic_api_emails(generic_rows)
        _save_accounts(accounts)
        return inserted, skipped


def import_codex_credential_accounts(records: list[dict]) -> tuple[int, int, int]:
    """Import OpenAI login credentials without requiring mailbox material."""
    with _LOCK:
        accounts = _load_accounts()
        inserted = updated = skipped = 0
        for raw in records:
            email = str(raw.get("email") or "").strip()
            password = str(raw.get("registration_password") or "")
            totp_secret = str(raw.get("totp_secret") or "").strip()
            if not email or not password or not totp_secret:
                skipped += 1
                continue

            row = _find_by_email(accounts, email)
            now = _now()
            if row is not None:
                if str(row.get("access_token") or "").strip():
                    skipped += 1
                    continue
                row["registration_password"] = password
                row["totp_secret"] = totp_secret
                row["codex_login_mode"] = "credentials"
                row["twofa_status"] = "active"
                row["twofa_error"] = None
                row["updated_at"] = now
                updated += 1
                continue

            accounts.append({
                "id": _next_id(accounts),
                "email": email,
                "created_at": now,
                "updated_at": now,
                "access_token": "",
                "registration_password": password,
                "totp_secret": totp_secret,
                "twofa_status": "active",
                "twofa_error": None,
                "codex_login_mode": "credentials",
                "codex_status": "",
                "codex_error": None,
                "user_name": "Imported Credential Account",
                "email_source": "credentials",
                "original_email_line": email,
                "extra_json": json.dumps({"imported_credentials": True}, ensure_ascii=False),
            })
            inserted += 1

        _save_accounts(accounts)
        return inserted, updated, skipped


def claim_next_outlook() -> dict | None:
    """原子领取一个可用 Outlook 账号并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_outlook(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_outlook(rows)
        return _decorate_outlook(row)


def release_outlook(email: str, status: str = "available", note: str | None = None) -> None:
    """把账号状态改回 available，或标记为 used/failed/disabled。"""
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_outlook(rows)


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 Outlook 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_outlook(rows)
        return True


def delete_outlook(email: str) -> bool:
    """从邮箱池彻底删除一个邮箱（按 email 匹配）。返回是否删到。"""
    with _LOCK:
        rows = _load_outlook()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_outlook(new_rows)
        return True


def list_outlook_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="outlook", status=status, limit=limit, offset=0
    )["items"]


def outlook_pool_summary() -> dict:
    with _LOCK:
        out = _pool_summary_sql("outlook")
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_outlook_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_outlook(), email)
        return _decorate_outlook(row) if row else None


# ============================================================
# generic_api email pool
# ============================================================

def import_generic_api_emails(records: list[dict]) -> tuple[int, int]:
    """
    批量导入通用 API 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = (raw.get("code_url") or raw.get("url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _generic_api_email_line(row)
            rows.append(row)
            inserted += 1
        _save_generic_api_emails(rows)
        return inserted, skipped


def claim_next_generic_api_email() -> dict | None:
    """原子领取一个可用通用 API 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_generic_api_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_generic_api_emails(rows)
        return _decorate_generic_api_email(row)


# ============================================================
# gmail_api_url email pool
# ============================================================

def import_gmail_api_url_emails(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Gmail API URL 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_gmail_api_url_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = (raw.get("code_url") or raw.get("url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _gmail_api_url_email_line(row)
            rows.append(row)
            inserted += 1
        _save_gmail_api_url_emails(rows)
        return inserted, skipped


def record_gmail_api_url_email(
    email: str,
    code_url: str,
    *,
    status: str = "used",
    note: str | None = None,
) -> bool:
    """Persist one Gmail API URL source, preserving its current ownership state."""
    normalized_email = str(email or "").strip().lower()
    normalized_url = str(code_url or "").strip()
    if not normalized_email or not normalized_url:
        raise ValueError("Gmail API URL email and code_url are required")

    state = str(status or "used").strip().lower()
    with _LOCK:
        rows = _load_gmail_api_url_emails()
        existing = _find_by_email(rows, normalized_email)
        if existing is not None:
            if str(existing.get("code_url") or "").strip() != normalized_url:
                raise ValueError("Gmail API URL email already has a different code_url")
            existing["status"] = state
            existing["used_at"] = None if state == "available" else existing.get("used_at") or _now()
            if note is not None:
                existing["note"] = str(note)
            existing["copy_line"] = _gmail_api_url_email_line(existing)
            _save_gmail_api_url_emails(rows)
            return False

        row = {
            "id": _next_id(rows),
            "email": normalized_email,
            "code_url": normalized_url,
            "status": state,
            "used_at": None if state == "available" else _now(),
            "note": note,
            "imported_at": _now(),
        }
        row["copy_line"] = _gmail_api_url_email_line(row)
        rows.append(row)
        _save_gmail_api_url_emails(rows)
        return True


def claim_next_gmail_api_url_email() -> dict | None:
    """原子领取一个可用 Gmail API URL 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_gmail_api_url_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_gmail_api_url_emails(rows)
        return _decorate_gmail_api_url_email(row)


def get_gmail_api_url_last_otp(code_url: str) -> str | None:
    """Return the last accepted OTP for a Gmail API URL mailbox."""
    target = str(code_url or "").strip()
    if not target:
        return None
    with _LOCK:
        row = next(
            (item for item in _load_gmail_api_url_emails() if str(item.get("code_url") or "").strip() == target),
            None,
        )
        value = str((row or {}).get("last_otp") or "").strip()
        return value or None


def record_gmail_api_url_otp(code_url: str, otp: str) -> bool:
    """Persist the last accepted OTP for every pool row sharing a code URL."""
    target = str(code_url or "").strip()
    value = str(otp or "").strip()
    if not target or not value:
        return False
    with _LOCK:
        rows = _load_gmail_api_url_emails()
        matched = False
        for row in rows:
            if str(row.get("code_url") or "").strip() != target:
                continue
            row["last_otp"] = value
            row["last_otp_at"] = _now()
            matched = True
        if matched:
            _save_gmail_api_url_emails(rows)
        return matched


def release_generic_api_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把通用 API 邮箱状态改回 available，或标记为 failed/used。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)


def release_gmail_api_url_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把 Gmail API URL 邮箱状态改回 available，或标记为 failed/used。"""
    with _LOCK:
        rows = _load_gmail_api_url_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_gmail_api_url_emails(rows)


def release_unconsumed_generic_api_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的通用 API 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)
        return True


def release_unconsumed_gmail_api_url_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 Gmail API URL 邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_gmail_api_url_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_gmail_api_url_emails(rows)
        return True


def delete_generic_api_email(email: str) -> bool:
    """从通用 API 邮箱池彻底删除一个邮箱。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_generic_api_emails(new_rows)
        return True


def delete_gmail_api_url_email(email: str) -> bool:
    """从 Gmail API URL 邮箱池彻底删除一个邮箱。"""
    with _LOCK:
        rows = _load_gmail_api_url_emails()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_gmail_api_url_emails(new_rows)
        return True


def list_generic_api_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="generic_api", status=status, limit=limit, offset=0
    )["items"]


def list_gmail_api_url_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_gmail_api_url_emails()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        decorated = [_decorate_gmail_api_url_email(r, account_by_email) for r in rows[:limit]]
        return _attach_gmail_api_url_alias_stats(decorated)


def generic_api_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("generic_api")


def gmail_api_url_email_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        rows = _load_gmail_api_url_emails()
        for row in rows:
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        alias_rows = _attach_gmail_api_url_alias_stats(rows)
        for key in ("alias_total", "alias_available", "alias_used", "alias_failed", "alias_reserved"):
            out[key] = sum(int(row.get(key) or 0) for row in alias_rows)
        return out


def get_generic_api_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_generic_api_emails(), email)
        return _decorate_generic_api_email(row) if row else None


def get_gmail_api_url_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_gmail_api_url_emails(), email)
        return _decorate_gmail_api_url_email(row) if row else None


def reset_gmail_api_url_aliases(email: str) -> dict:
    """Reset unused aliases for one Gmail source while preserving consumed aliases."""
    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        raise ValueError("Gmail API URL email is required")

    with _LOCK:
        record = _find_by_email(_load_gmail_api_url_emails(), normalized_email)
        if record is None:
            raise ValueError("Gmail API URL email not found")
        if _find_by_email(_load_accounts(), normalized_email) is not None:
            raise GmailApiUrlBatchConflict("该 Gmail 源邮箱已存在注册账号，不能重置")
        code_url = str(record.get("code_url") or "").strip()
        if not code_url:
            raise ValueError("Gmail API URL code_url is missing")

        reset_count = GmailApiUrlBatchStore(_SQLITE_PATH).reset_unused_aliases_for_code_url(code_url)
        if reset_count:
            release_gmail_api_url_email(
                normalized_email,
                status="available",
                note=f"手动重置未消费 alias：{reset_count}",
            )
        current = _find_by_email(_load_gmail_api_url_emails(), normalized_email) or record
        stats = _attach_gmail_api_url_alias_stats([current])[0]
        return {
            "reset_aliases": reset_count,
            "source_status": current.get("status"),
            "alias_total": int(stats.get("alias_total") or 0),
            "alias_allocated": int(stats.get("alias_allocated") or 0),
            "alias_available": int(stats.get("alias_available") or 0),
            "alias_used": int(stats.get("alias_used") or 0),
            "alias_failed": int(stats.get("alias_failed") or 0),
            "alias_reserved": int(stats.get("alias_reserved") or 0),
        }


# ============================================================
# Codex 授权账号（SQLite codex_accounts 表）
# ============================================================

def _codex_filter_sql(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if archived in (True, "1", "true", "yes", "only"):
        where.append("archived=1")
    elif archived not in ("all", "include"):
        where.append("archived=0")
    if date_from:
        value = str(date_from)
        where.append("created_at >= ?")
        params.append(value + ("T00:00:00" if len(value) == 10 else ""))
    if date_to:
        value = str(date_to)
        where.append("created_at <= ?")
        params.append(value + ("T23:59:59.999999" if len(value) == 10 else ""))
    if q and str(q).strip():
        where.append("lower(payload) LIKE ?")
        params.append("%" + str(q).strip().lower() + "%")
    return where, params


def _codex_content_to_record(content: dict) -> dict:
    """把 SQLite 中的 Codex payload 转成列表展示对象。"""
    fname = content.get("_filename", "")
    without_prefix = fname[5:-5] if fname.startswith("codex-") and fname.endswith(".json") else fname
    email = content.get("email") or without_prefix
    plan = ""
    if "-" in without_prefix and without_prefix.rsplit("-", 1)[-1].lower() in ("free", "plus", "team", "pro", "enterprise"):
        plan = without_prefix.rsplit("-", 1)[-1].lower()
        if not content.get("email"):
            email = without_prefix.rsplit("-", 1)[0]
    return {
        "filename": fname, "path": f"sqlite://codex_accounts/{fname}", "email": email, "plan": plan,
        "account_id": content.get("account_id", ""), "type": content.get("type", "codex"),
        "last_refresh": content.get("last_refresh", ""), "expired": content.get("expired", ""),
        "access_token_preview": (content.get("access_token", "") or "")[:32],
        "size": content.get("_size", 0), "mtime": content.get("_mtime", ""),
        "exported_at": content.get("_exported_at"), "exported_count": content.get("_exported_count", 0),
        "archived": bool(content.get("_archived")), "archived_at": content.get("_archived_at"),
    }


def list_codex_accounts_page(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """直接在 codex_accounts 表执行分页查询，不读取 codex_accounts/ 文件。"""
    _ensure_sqlite()
    limit = max(1, int(limit))
    offset = max(0, int(offset or 0))
    where, params = _codex_filter_sql(archived, date_from, date_to, q)
    clause = " AND ".join(where) if where else "1=1"
    with closing(_sqlite_conn()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM codex_accounts WHERE {clause}", params).fetchone()[0])
        latest = str(conn.execute(
            f"SELECT COALESCE(MAX(updated_at), '') FROM codex_accounts WHERE {clause}", params
        ).fetchone()[0] or "")
        rows = conn.execute(
            f"SELECT payload FROM codex_accounts WHERE {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = [_codex_content_to_record(json.loads(row["payload"])) for row in rows]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "revision": f"{total}:{latest}",
    }


def list_codex_accounts(
    archived: str | bool | None = "0",
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
) -> list[dict]:
    """从 SQLite 读取 Codex 凭证元数据，不扫描 codex_accounts/ 文件。"""
    _ensure_sqlite()
    where, params = _codex_filter_sql(archived, date_from, date_to, q)
    clause = " AND ".join(where) if where else "1=1"
    with closing(_sqlite_conn()) as conn:
        rows = [json.loads(row["payload"]) for row in conn.execute(
            f"SELECT payload FROM codex_accounts WHERE {clause} ORDER BY created_at DESC, id DESC", params
        )]
    return [_codex_content_to_record(content) for content in rows]


def upsert_codex_credential(content: dict, filename: str) -> str:
    """把 Codex 凭证写入 SQLite，返回逻辑文件名（不创建本地文件）。"""
    if not isinstance(content, dict) or not filename:
        raise ValueError("Codex 凭证或文件名无效")
    _ensure_sqlite()
    now = _now()
    with _LOCK, closing(_sqlite_conn()) as conn:
        old = conn.execute("SELECT payload, created_at FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
        meta = dict(content)
        if old:
            previous = json.loads(old["payload"])
            for key in ("_exported_at", "_exported_count", "_archived", "_archived_at"):
                if key not in meta:
                    meta[key] = previous.get(key)
            created_at = old["created_at"] or now
            account_id = conn.execute("SELECT id FROM codex_accounts WHERE filename=?", (filename,)).fetchone()[0]
        else:
            account_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM codex_accounts").fetchone()[0])
            created_at = now
        meta.update({"_filename": filename, "_size": len(json.dumps(content, ensure_ascii=False).encode("utf-8")), "_mtime": now})
        conn.execute(
            "INSERT INTO codex_accounts(id,filename,email,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(filename) DO UPDATE SET email=excluded.email, archived=excluded.archived, updated_at=excluded.updated_at, payload=excluded.payload",
            (account_id, filename, str(content.get("email") or ""), int(bool(meta.get("_archived"))), created_at, now, json.dumps(meta, ensure_ascii=False)),
        )
        conn.commit()
    return filename

def archive_codex(filename: str, archived: bool = True) -> dict | None:
    """归档/取消归档一条 Codex 授权凭证。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return None
            content = json.loads(row["payload"])
            rec = {"exported_at": content.get("_exported_at"), "exported_count": content.get("_exported_count", 0)}
        rec["archived"] = bool(archived)
        rec["archived_at"] = _now() if archived else None
        content.update({"_archived": rec["archived"], "_archived_at": rec["archived_at"]})
        with closing(_sqlite_conn()) as conn:
            conn.execute("UPDATE codex_accounts SET archived=?, updated_at=?, payload=? WHERE filename=?", (int(archived), _now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()
        return rec


def read_codex_credential(filename: str) -> tuple[str, str]:
    """
    读取一个 codex-*.json 文件原始内容。
    Returns: (content_string, filename)
    抛 ValueError：文件名不合法（防目录穿越）/ 不存在。
    """
    with _LOCK:
        # 防注入：只允许 codex-*.json 模式，不允许路径分隔符
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
        if not row:
            raise ValueError(f"文件不存在: {filename}")
        content = json.loads(row["payload"])
        content = {k: v for k, v in content.items() if not k.startswith("_")}
        return json.dumps(content, ensure_ascii=False, indent=2), filename


def mark_codex_exported(filename: str) -> dict:
    """
    标记某个 codex 凭证已导出（导出计数 +1，记录最近导出时间）。
    Returns: 该 filename 当前的导出状态记录。
    """
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return {"exported_count": 0}
            content = json.loads(row["payload"])
        rec = {"exported_count": int(content.get("_exported_count", 0) or 0)}
        rec["exported_count"] = int(rec.get("exported_count", 0)) + 1
        rec["exported_at"] = _now()
        content.update({"_exported_count": rec["exported_count"], "_exported_at": rec["exported_at"]})
        with closing(_sqlite_conn()) as conn:
            conn.execute("UPDATE codex_accounts SET updated_at=?, payload=? WHERE filename=?", (_now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()
        return rec


def reset_codex_exported(filename: str) -> None:
    """清掉某个 codex 凭证的导出状态（用户想重置时用）。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute("SELECT payload FROM codex_accounts WHERE filename=?", (filename,)).fetchone()
            if not row:
                return
            content = json.loads(row["payload"])
            content.update({"_exported_count": 0, "_exported_at": None})
            conn.execute("UPDATE codex_accounts SET updated_at=?, payload=? WHERE filename=?", (_now(), json.dumps(content, ensure_ascii=False), filename))
            conn.commit()


def delete_codex_credential(filename: str) -> bool:
    """从 SQLite 删除一个 Codex 凭证。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            cur = conn.execute("DELETE FROM codex_accounts WHERE filename=?", (filename,))
            conn.commit()
            return cur.rowcount > 0


def codex_accounts_summary() -> dict:
    """codex 账号汇总：总数 / 已导出 / 未导出。"""
    with _LOCK:
        _ensure_sqlite()
        with closing(_sqlite_conn()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN COALESCE(CAST(json_extract(payload, '$._exported_count') AS INTEGER), 0) > 0 "
                "THEN 1 ELSE 0 END) AS exported "
                "FROM codex_accounts WHERE archived=0"
            ).fetchone()
        total = int(row["total"] or 0)
        exported = int(row["exported"] or 0)
        return {
            "total": total,
            "exported": exported,
            "pending": total - exported,
        }


# ============================================================
# registration_jobs
# ============================================================

def _new_job_row(
    rows: list[dict],
    *,
    email_source: str,
    job_type: str = "registration",
    parent_job_id: int | None = None,
    root_job_id: int | None = None,
    retry_attempt: int = 0,
    retry_action: str | None = None,
    email: str | None = None,
    account_id: int | None = None,
    provider_context: dict | None = None,
) -> dict:
    job_uuid = str(uuid.uuid4())
    log_file = str(_LOG_DIR / f"{job_uuid}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    return {
        "id": _next_registration_job_id(rows),
        "job_uuid": job_uuid,
        "job_type": job_type,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "retry_attempt": int(retry_attempt or 0),
        "retry_action": retry_action,
        "email_source": email_source,
        "email": email,
        "status": "pending",
        "error_message": None,
        "log_file": log_file,
        "started_at": None,
        "completed_at": None,
        "account_id": account_id,
        "provider_context": dict(provider_context or {}),
        "created_at": _now(),
    }


def create_job(
    email_source: str,
    *,
    job_type: str = "registration",
    email: str | None = None,
    account_id: int | None = None,
    provider_context: dict | None = None,
) -> dict:
    """创建一个首次执行的 pending 任务。"""
    with _LOCK:
        rows = _load_jobs()
        row = _new_job_row(
            rows,
            email_source=email_source,
            job_type=job_type,
            email=email,
            account_id=account_id,
            provider_context=provider_context,
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row)


def create_retry_job(
    source_job_id: int,
    *,
    job_type: str,
    email_source: str,
    email: str | None = None,
    account_id: int | None = None,
    allow_success_for_twofa: bool = False,
) -> tuple[dict, bool]:
    """原子创建重试子任务；同一任务链已有活跃任务时直接复用。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(source_job_id)), None)
        if source is None:
            raise LookupError("任务不存在")
        allowed_statuses = {"failed", "stopped", "cancelled"}
        if allow_success_for_twofa and job_type == "twofa_retry":
            allowed_statuses.add("success")
        if source.get("status") not in allowed_statuses:
            raise ValueError(f"当前状态不支持重试：{source.get('status')}")

        root_id = int(source.get("root_job_id") or source.get("id"))
        active_states = {"pending", "running", "stopping"}
        active = next((
            r for r in rows
            if int(r.get("id") or 0) != int(source_job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") in active_states
        ), None)
        if active is not None:
            if active.get("job_type", "registration") != job_type:
                raise ValueError(f"已有其他类型重试任务 #{active.get('id')} 在排队或运行中")
            return dict(active), False

        attempts = [
            int(r.get("retry_attempt") or 0)
            for r in rows
            if int(r.get("id") or 0) == root_id or int(r.get("root_job_id") or 0) == root_id
        ]
        row = _new_job_row(
            rows,
            email_source=email_source,
            job_type=job_type,
            parent_job_id=int(source_job_id),
            root_job_id=root_id,
            retry_attempt=(max(attempts) if attempts else 0) + 1,
            retry_action=(
                "codex" if job_type == "codex_retry"
                else "2fa" if job_type == "twofa_retry"
                else "registration"
            ),
            email=email,
            account_id=account_id,
            provider_context=dict(source.get("provider_context") or {}),
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row), True


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    email: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    account_id: int | None = None,
    network_identity: dict | None = None,
) -> None:
    with _LOCK:
        rows = _load_jobs()
        row = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if row is None:
            return
        if status is not None:
            row["status"] = status
        if email is not None:
            row["email"] = email
        if error is not None:
            row["error_message"] = error
        if started_at is not None:
            row["started_at"] = started_at
        if completed_at is not None:
            row["completed_at"] = completed_at
        if account_id is not None:
            row["account_id"] = account_id
        if network_identity is not None:
            row["network_identity"] = dict(network_identity)
        _save_jobs(rows)


def list_jobs(limit: int = 100) -> list[dict]:
    with _LOCK:
        return [dict(r) for r in _query_collection("jobs", limit=limit)]


def list_jobs_page(limit: int = 50, offset: int = 0) -> dict:
    """直接使用 registration_jobs 的 SQL LIMIT/OFFSET 返回任务页。"""
    with _LOCK:
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        rows, total, latest = _query_collection_page(
            "jobs", limit=limit, offset=offset
        )
        return {
            "items": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
            "revision": f"{total}:{latest}",
        }


def job_status_counts() -> dict:
    """在 SQLite 中聚合任务状态，避免为统计目的加载全部任务 payload。"""
    _ensure_sqlite()
    with closing(_sqlite_conn()) as conn:
        counts = {
            str(row["status"] or "unknown"): int(row["n"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM registration_jobs GROUP BY status"
            )
        }
    counts["active"] = sum(int(counts.get(status, 0) or 0) for status in ("pending", "running", "stopping"))
    return counts


def list_jobs_for_automation_request(request_id: str) -> list[dict]:
    """Return all jobs owned by one sub2api automation request."""
    request_id = str(request_id or "").strip()
    if not request_id:
        return []
    with _LOCK:
        rows = []
        for row in _load_jobs():
            context = row.get("provider_context") if isinstance(row, dict) else None
            if isinstance(context, dict) and context.get("sub2api_automation_request_id") == request_id:
                rows.append(dict(row))
        return sorted(rows, key=lambda item: int(item.get("id") or 0))


def get_job(job_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_jobs() if int(r.get("id") or 0) == int(job_id)), None)
        return dict(row) if row else None


def get_latest_job_for_account(account_id: int) -> dict | None:
    """返回账号最近关联的注册/补跑任务。"""
    with _LOCK:
        matches = [
            r for r in _load_jobs()
            if int(r.get("account_id") or 0) == int(account_id)
        ]
        row = max(matches, key=lambda r: int(r.get("id") or 0), default=None)
        return dict(row) if row else None


def get_latest_job_for_email(email: str) -> dict | None:
    """账号 ID 缺失时按邮箱返回最近关联任务。"""
    target = str(email or "").strip().casefold()
    if not target:
        return None
    with _LOCK:
        matches = [
            r for r in _load_jobs()
            if str(r.get("email") or "").strip().casefold() == target
        ]
        row = max(matches, key=lambda r: int(r.get("id") or 0), default=None)
        return dict(row) if row else None


def get_successful_retry_for_job(job_id: int) -> dict | None:
    """返回同一任务链中已成功的其他重试任务，用于保留原任务历史状态并阻止重复重试。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if source is None:
            return None
        root_id = int(source.get("root_job_id") or source.get("id") or 0)
        matches = [
            r for r in rows
            if int(r.get("id") or 0) != int(job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") == "success"
        ]
        if not matches:
            return None
        return dict(max(matches, key=lambda r: int(r.get("id") or 0)))


def delete_job(job_id: int, *, delete_log: bool = True, allow_running: bool = False) -> bool:
    """
    删除一个注册任务记录；默认同时删除该任务日志文件。返回是否删除到记录。
    默认不删除 running 任务，避免后台线程仍在执行但前端记录消失。
    """
    with _LOCK:
        rows = _load_jobs()
        idx = next((i for i, r in enumerate(rows) if int(r.get("id") or 0) == int(job_id)), None)
        if idx is None:
            return False
        if not allow_running and rows[idx].get("status") in ("running", "stopping"):
            return False
        row = rows.pop(idx)
        _save_jobs(rows)

    if delete_log:
        log_file = row.get("log_file")
        if log_file:
            try:
                Path(log_file).unlink(missing_ok=True)
            except OSError:
                logger.debug("删除注册任务日志失败: %s", log_file, exc_info=True)
    return True


# ============================================================
# 迁移与路径
# ============================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _migrate_legacy_sqlite() -> dict:
    summary = {"sqlite_accounts_imported": 0, "sqlite_outlook_imported": 0, "sqlite_outlook_skipped": 0}
    if not _LEGACY_SQLITE.exists():
        return summary
    try:
        with closing(sqlite3.connect(str(_LEGACY_SQLITE))) as conn:
            conn.row_factory = sqlite3.Row
            if _table_exists(conn, "outlook_pool"):
                records = []
                statuses = []
                for row in conn.execute("SELECT * FROM outlook_pool").fetchall():
                    records.append({
                        "email": row["email"],
                        "password": row["password"],
                        "client_id": row["client_id"],
                        "refresh_token": row["refresh_token"],
                    })
                    statuses.append({
                        "email": row["email"],
                        "status": row["status"],
                        "note": row["note"],
                    })
                ins, skip = import_outlook_accounts(records)
                for item in statuses:
                    if item["status"] != "available":
                        release_outlook(item["email"], status=item["status"], note=item["note"])
                summary["sqlite_outlook_imported"] += ins
                summary["sqlite_outlook_skipped"] += skip
            if _table_exists(conn, "registered_accounts"):
                for row in conn.execute("SELECT * FROM registered_accounts").fetchall():
                    insert_account(
                        email=row["email"],
                        access_token=row["access_token"],
                        totp_secret=row["totp_secret"],
                        user_id=row["user_id"],
                        user_name=row["user_name"],
                        plan_type=row["plan_type"],
                        expires_at=row["expires_at"],
                        proxy_used=row["proxy_used"],
                        email_source=row["email_source"],
                        extra=json.loads(row["extra_json"]) if row["extra_json"] else None,
                    )
                    summary["sqlite_accounts_imported"] += 1
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, AttributeError) as exc:
        summary["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def migrate_legacy_files() -> dict:
    """
    把历史 SQLite、accounts/*.json、旧邮箱 TXT/JSON 迁移到当前 SQLite 存储。
    多次调用是幂等的，不会生成或更新旧 JSON/TXT 文件。
    """
    summary = {
        "accounts_imported": 0,
        "outlook_imported": 0,
        "outlook_skipped": 0,
    }
    summary.update(_migrate_legacy_sqlite())

    accounts_dir = _PROJECT_ROOT / "accounts"
    if accounts_dir.exists():
        for jf in accounts_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not data.get("email") or not data.get("access_token"):
                    continue
                extra = data.get("extra") or {}
                user = extra.get("user") or {}
                account = extra.get("account") or {}
                insert_account(
                    email=data["email"],
                    access_token=data["access_token"],
                    totp_secret=data.get("totp_secret"),
                    user_id=user.get("id"),
                    user_name=user.get("name"),
                    plan_type=account.get("planType"),
                    expires_at=extra.get("expires"),
                    extra=extra,
                )
                summary["accounts_imported"] += 1
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.debug("无法迁移账号文件 %s：%s: %s", jf, type(exc).__name__, exc)
                continue

    for txt in (_PROJECT_ROOT / "outlook_accounts.txt", _OUTLOOK_TXT):
        if txt.exists():
            records = []
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                # 支持 4 段或 6 段格式
                if len(parts) == 4:
                    email, password, client_id, refresh_token = (p.strip() for p in parts)
                elif len(parts) == 6:
                    email, password, client_id, refresh_token, _, _ = (p.strip() for p in parts)
                else:
                    continue
                records.append({
                    "email": email,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                })
            ins, skip = import_outlook_accounts(records)
            summary["outlook_imported"] += ins
            summary["outlook_skipped"] += skip

    used = _PROJECT_ROOT / "outlook_accounts_used.json"
    if used.exists():
        try:
            emails = json.loads(used.read_text(encoding="utf-8"))
            for email in emails:
                release_outlook(email, status="used")
        except (OSError, UnicodeError, TypeError, ValueError, sqlite3.Error):
            logger.debug("迁移已使用 Outlook 邮箱状态失败", exc_info=True)

    return summary


def db_path() -> Path:
    """返回 SQLite 主数据库路径（保留函数名兼容旧调用方）。"""
    _ensure_sqlite()
    return _active_sqlite_path()


def storage_paths() -> dict:
    return {
        "sqlite": str(_active_sqlite_path()),
        "app_state_db": str(app_state_db.APP_STATE_DB_PATH),
        "outlook_json": str(_OUTLOOK_JSON),
        "outlook_txt": str(_OUTLOOK_TXT),
        "accounts_json": str(_ACCOUNTS_JSON),
        "accounts_txt": str(_ACCOUNTS_TXT),
        "tokens_txt": str(_TOKENS_TXT),
        "viewer_html": str(_VIEWER_HTML),
        "jobs_json": str(_JOBS_JSON),
        "logs_dir": str(_LOG_DIR),
    }


# ============================================================
# Domain email pool（Cloudflare 域名邮箱跟踪）
# ============================================================

_DOMAIN_EMAIL_JSON = _PROJECT_ROOT / "用于注册的域名邮箱.json"


def _load_domain_pool() -> list[dict]:
    return _load_collection("domain")


def _save_domain_pool(rows: list[dict]) -> None:
    _save_collection("domain", rows)


def _find_domain_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def claim_next_domain_email(email: str) -> dict:
    """记录一个新的域名邮箱地址到池中（标记为 available）。"""
    with _LOCK:
        rows = _load_domain_pool()
        if _find_domain_email(rows, email):
            # 已存在，直接返回
            row = _find_domain_email(rows, email)
            return row
        row = {
            "id": _next_id(rows),
            "email": email,
            "status": "available",
            "used_at": None,
            "note": None,
            "created_at": _now(),
        }
        rows.append(row)
        _save_domain_pool(rows)
        return dict(row)


def release_domain_email(email: str, status: str = "available", note: str | None = None) -> None:
    """更新域名邮箱状态。"""
    with _LOCK:
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)


def release_unconsumed_domain_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的域名邮箱。"""
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)
        return True


def get_domain_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_domain_email(_load_domain_pool(), email)
        return dict(row) if row else None


def list_domain_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    return list_email_pool_page(
        source="cloudflare_domain", status=status, limit=limit, offset=0
    )["items"]


def domain_email_pool_summary() -> dict:
    with _LOCK:
        return _pool_summary_sql("domain")


def delete_domain_email(email: str) -> bool:
    """从域名邮箱池删除一个邮箱。"""
    with _LOCK:
        rows = _load_domain_pool()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_domain_pool(new_rows)
        return True
