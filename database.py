import os
import random
import sqlite3
import string
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

CAIRO_TZ = ZoneInfo("Africa/Cairo")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant_data.db")
_db_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _table_has_column(conn, table_name, col_name):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
    return col_name in cols


def init_db():
    with _db_lock, get_conn() as conn:
        # ترقية جدول settings القديم إن وجد ليدعم تعدد المساحات
        existing_tables = [
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        if "settings" in existing_tables and not _table_has_column(conn, "settings", "ws_id"):
            conn.execute("ALTER TABLE settings RENAME TO settings_legacy")

        if "seen_links" in existing_tables and not _table_has_column(conn, "seen_links", "ws_id"):
            conn.execute("ALTER TABLE seen_links RENAME TO seen_links_legacy")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                active_ws TEXT NOT NULL,
                shared_ws TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                ws_id TEXT PRIMARY KEY,
                invite_code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                ws_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (ws_id, key)
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ws_id TEXT NOT NULL DEFAULT 'default',
                added_by TEXT DEFAULT '',
                item TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ws_id TEXT NOT NULL DEFAULT 'default',
                text TEXT NOT NULL,
                remind_at_ts INTEGER NOT NULL,
                remind_at_str TEXT NOT NULL,
                is_sent INTEGER DEFAULT 0,
                repeat_seconds INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS vault (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ws_id TEXT NOT NULL DEFAULT 'default',
                added_by TEXT DEFAULT '',
                tag TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watchers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ws_id TEXT NOT NULL DEFAULT 'default',
                w_type TEXT NOT NULL,
                target_url TEXT,
                criteria TEXT NOT NULL,
                last_value TEXT,
                title TEXT
            );
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ws_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL,
                amount REAL NOT NULL,
                due_day INTEGER NOT NULL,
                last_paid_month TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS seen_links (
                ws_id TEXT NOT NULL,
                url TEXT NOT NULL,
                PRIMARY KEY (ws_id, url)
            );
        """)

        # ترقية الجداول القديمة إن وجدت
        for tbl in ("expenses", "reminders", "vault", "watchers", "bills"):
            if not _table_has_column(conn, tbl, "ws_id"):
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN ws_id TEXT NOT NULL DEFAULT 'default'")

        for tbl in ("expenses", "vault"):
            if not _table_has_column(conn, tbl, "added_by"):
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN added_by TEXT DEFAULT ''")

        if not _table_has_column(conn, "reminders", "repeat_seconds"):
            conn.execute("ALTER TABLE reminders ADD COLUMN repeat_seconds INTEGER DEFAULT 0")

        # نقل البيانات القديمة تلقائياً لحساب المالك الأول
        if "settings_legacy" in existing_tables:
            old_rows = conn.execute("SELECT key, value FROM settings_legacy").fetchall()
            old_owner = next((r["value"] for r in old_rows if r["key"] == "owner_chat_id"), None)
            target_ws = f"user_{old_owner}" if old_owner else "default"
            for r in old_rows:
                if r["key"] == "owner_chat_id":
                    conn.execute("INSERT OR IGNORE INTO settings (ws_id, key, value) VALUES ('global', 'bot_admin_id', ?)", (r["value"],))
                elif r["key"] == "gemini_api_key":
                    conn.execute("INSERT OR IGNORE INTO settings (ws_id, key, value) VALUES ('global', 'gemini_api_key', ?)", (r["value"],))
                else:
                    conn.execute("INSERT OR IGNORE INTO settings (ws_id, key, value) VALUES (?, ?, ?)", (target_ws, r["key"], r["value"]))
            if old_owner:
                for tbl in ("expenses", "reminders", "vault", "watchers", "bills"):
                    conn.execute(f"UPDATE {tbl} SET ws_id = ? WHERE ws_id = 'default'", (target_ws,))
            conn.execute("DROP TABLE settings_legacy")

        if "seen_links_legacy" in existing_tables:
            conn.execute("DROP TABLE seen_links_legacy")


def get_setting(ws_id, key, default=None):
    with _db_lock, get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE ws_id = ? AND key = ?", (ws_id, key)).fetchone()
        return row["value"] if row else default


def set_setting(ws_id, key, value):
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (ws_id, key, value) VALUES (?, ?, ?) ON CONFLICT(ws_id, key) DO UPDATE SET value = excluded.value",
            (ws_id, key, str(value)),
        )


# --- إدارة المستخدمين والمساحات المشتركة ---
def register_or_get_user(chat_id, first_name="مستخدم"):
    cid = str(chat_id)
    personal_ws = f"user_{cid}"
    clean_name = (first_name or "مستخدم").strip()[:25]

    with _db_lock, get_conn() as conn:
        admin = conn.execute("SELECT value FROM settings WHERE ws_id = 'global' AND key = 'bot_admin_id'").fetchone()
        if not admin:
            conn.execute("INSERT INTO settings (ws_id, key, value) VALUES ('global', 'bot_admin_id', ?)", (cid,))

        row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (cid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (chat_id, display_name, active_ws, shared_ws) VALUES (?, ?, ?, '')",
                (cid, clean_name, personal_ws),
            )
            row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (cid,)).fetchone()

    user = dict(row)
    user["is_shared_mode"] = user["active_ws"].startswith("shared_")
    return user


def create_shared_workspace(chat_id, space_name="مساحة العائلة"):
    cid = str(chat_id)
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    ws_id = f"shared_{code}"
    clean_name = (space_name or "مساحة العائلة").strip()[:30]

    with _db_lock, get_conn() as conn:
        conn.execute("INSERT INTO workspaces (ws_id, invite_code, name) VALUES (?, ?, ?)", (ws_id, code, clean_name))
        conn.execute("UPDATE users SET shared_ws = ?, active_ws = ? WHERE chat_id = ?", (ws_id, ws_id, cid))
    return {"ws_id": ws_id, "invite_code": code, "name": clean_name}


def join_shared_workspace(chat_id, invite_code):
    cid = str(chat_id)
    code = invite_code.strip().upper()
    with _db_lock, get_conn() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE invite_code = ?", (code,)).fetchone()
        if not ws:
            return None
        conn.execute("UPDATE users SET shared_ws = ?, active_ws = ? WHERE chat_id = ?", (ws["ws_id"], ws["ws_id"], cid))
        return dict(ws)


def switch_workspace_mode(chat_id, mode):
    cid = str(chat_id)
    with _db_lock, get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE chat_id = ?", (cid,)).fetchone()
        if not user:
            return False
        if mode == "shared" and user["shared_ws"]:
            conn.execute("UPDATE users SET active_ws = ? WHERE chat_id = ?", (user["shared_ws"], cid))
            return True
        elif mode == "personal":
            conn.execute("UPDATE users SET active_ws = ? WHERE chat_id = ?", (f"user_{cid}", cid))
            return True
    return False


def leave_shared_workspace(chat_id):
    cid = str(chat_id)
    with _db_lock, get_conn() as conn:
        conn.execute("UPDATE users SET shared_ws = '', active_ws = ? WHERE chat_id = ?", (f"user_{cid}", cid))


def get_workspace_details(ws_id):
    with _db_lock, get_conn() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE ws_id = ?", (ws_id,)).fetchone()
        if not ws:
            return None
        members = conn.execute("SELECT chat_id, display_name FROM users WHERE shared_ws = ?", (ws_id,)).fetchall()
        return {
            "ws_id": ws["ws_id"],
            "invite_code": ws["invite_code"],
            "name": ws["name"],
            "members": [dict(m) for m in members],
        }


def get_workspace_recipients(ws_id):
    if ws_id.startswith("user_"):
        return [ws_id[5:]]
    if ws_id.startswith("shared_"):
        with _db_lock, get_conn() as conn:
            rows = conn.execute("SELECT chat_id FROM users WHERE shared_ws = ?", (ws_id,)).fetchall()
            return [r["chat_id"] for r in rows]
    return []


def get_all_active_workspaces():
    with _db_lock, get_conn() as conn:
        u_rows = conn.execute("SELECT chat_id FROM users").fetchall()
        s_rows = conn.execute("SELECT DISTINCT shared_ws FROM users WHERE shared_ws != ''").fetchall()
        ws_list = [f"user_{r['chat_id']}" for r in u_rows] + [r["shared_ws"] for r in s_rows]
        return list(dict.fromkeys(ws_list))


# --- المصاريف ---
def add_expense(ws_id, item, amount, category, added_by=""):
    now_str = datetime.now(CAIRO_TZ).strftime("%Y-%m-%d %H:%M")
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (ws_id, added_by, item, amount, category, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ws_id, added_by, item, amount, category, now_str),
        )


def undo_last_expense(ws_id):
    with _db_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT id, item, amount, category FROM expenses WHERE ws_id = ? ORDER BY id DESC LIMIT 1",
            (ws_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM expenses WHERE id = ?", (row["id"],))
        return dict(row)


def get_expenses_summary(ws_id):
    now = datetime.now(CAIRO_TZ)
    today_prefix = now.strftime("%Y-%m-%d")
    month_prefix = now.strftime("%Y-%m")

    with _db_lock, get_conn() as conn:
        today_total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM expenses WHERE ws_id = ? AND created_at LIKE ?",
            (ws_id, f"{today_prefix}%"),
        ).fetchone()["s"]

        month_total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM expenses WHERE ws_id = ? AND created_at LIKE ?",
            (ws_id, f"{month_prefix}%"),
        ).fetchone()["s"]

        by_cat = conn.execute(
            "SELECT category, SUM(amount) AS total FROM expenses WHERE ws_id = ? AND created_at LIKE ? GROUP BY category ORDER BY total DESC",
            (ws_id, f"{month_prefix}%"),
        ).fetchall()

        recent = conn.execute(
            "SELECT item, amount, category, added_by, created_at FROM expenses WHERE ws_id = ? ORDER BY id DESC LIMIT 6",
            (ws_id,),
        ).fetchall()

    return {
        "today": round(today_total, 2),
        "month": round(month_total, 2),
        "categories": [(r["category"], round(r["total"], 2)) for r in by_cat],
        "recent": [dict(r) for r in recent],
    }


def get_all_expenses(ws_id):
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT id, item, amount, category, added_by, created_at FROM expenses WHERE ws_id = ? ORDER BY id DESC",
            (ws_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- الفواتير والأقساط الشهرية ---
def add_bill(ws_id, title, amount, due_day):
    day = min(max(int(due_day), 1), 28)
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO bills (ws_id, title, amount, due_day, last_paid_month) VALUES (?, ?, ?, ?, '')",
            (ws_id, title.strip(), float(amount), day),
        )


def get_bills(ws_id):
    with _db_lock, get_conn() as conn:
        rows = conn.execute("SELECT * FROM bills WHERE ws_id = ? ORDER BY due_day ASC", (ws_id,)).fetchall()
        return [dict(r) for r in rows]


def delete_bill(ws_id, bill_id):
    with _db_lock, get_conn() as conn:
        conn.execute("DELETE FROM bills WHERE ws_id = ? AND id = ?", (ws_id, int(bill_id)))


def mark_bill_paid(ws_id, bill_id, added_by=""):
    curr_month = datetime.now(CAIRO_TZ).strftime("%Y-%m")
    with _db_lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM bills WHERE ws_id = ? AND id = ?", (ws_id, int(bill_id))).fetchone()
        if not row:
            return None
        conn.execute("UPDATE bills SET last_paid_month = ? WHERE id = ?", (curr_month, int(bill_id)))
    add_expense(ws_id, f"فاتورة/قسط: {row['title']}", row["amount"], "فواتير واشتراكات", added_by=added_by)
    return dict(row)


def get_due_bills(ws_id, days_ahead=2):
    now = datetime.now(CAIRO_TZ)
    curr_month = now.strftime("%Y-%m")
    due_list = []
    for b in get_bills(ws_id):
        if b["last_paid_month"] == curr_month:
            continue
        diff = b["due_day"] - now.day
        if -5 <= diff <= days_ahead:
            due_list.append(b)
    return due_list


# --- التذكيرات ---
def add_reminder(ws_id, text, remind_dt, repeat_seconds=0):
    ts = int(remind_dt.timestamp())
    dt_str = remind_dt.strftime("%Y-%m-%d %I:%M:%S %p")
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO reminders (ws_id, text, remind_at_ts, remind_at_str, is_sent, repeat_seconds) VALUES (?, ?, ?, ?, 0, ?)",
            (ws_id, text, ts, dt_str, int(repeat_seconds)),
        )
    return dt_str


def get_due_reminders():
    now_dt = datetime.now(CAIRO_TZ)
    now_ts = int(now_dt.timestamp())
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ws_id, text, remind_at_str, COALESCE(repeat_seconds, 0) AS repeat_seconds FROM reminders WHERE is_sent = 0 AND remind_at_ts <= ?",
            (now_ts,),
        ).fetchall()

        for r in rows:
            rep = int(r["repeat_seconds"])
            if rep > 0:
                next_ts = now_ts + rep
                next_str = datetime.fromtimestamp(next_ts, CAIRO_TZ).strftime("%Y-%m-%d %I:%M:%S %p")
                conn.execute(
                    "UPDATE reminders SET remind_at_ts = ?, remind_at_str = ? WHERE id = ?",
                    (next_ts, next_str, r["id"]),
                )
            else:
                conn.execute("UPDATE reminders SET is_sent = 1 WHERE id = ?", (r["id"],))

        return [dict(r) for r in rows]


def get_upcoming_reminders(ws_id):
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text, remind_at_str, COALESCE(repeat_seconds, 0) AS repeat_seconds FROM reminders WHERE ws_id = ? AND is_sent = 0 ORDER BY remind_at_ts ASC LIMIT 12",
            (ws_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_today_reminders(ws_id):
    today_prefix = datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT text, remind_at_str FROM reminders WHERE ws_id = ? AND is_sent = 0 AND remind_at_str LIKE ? ORDER BY remind_at_ts ASC",
            (ws_id, f"{today_prefix}%"),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_reminder(ws_id, rem_id):
    with _db_lock, get_conn() as conn:
        conn.execute("DELETE FROM reminders WHERE ws_id = ? AND id = ?", (ws_id, int(rem_id)))


# --- الذاكرة التانية (Smart Vault) ---
def add_vault_item(ws_id, tag, content, added_by=""):
    now_str = datetime.now(CAIRO_TZ).strftime("%Y-%m-%d %H:%M")
    clean_tag = tag.strip().lstrip("#").lower()
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO vault (ws_id, added_by, tag, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (ws_id, added_by, clean_tag, content.strip(), now_str),
        )


def get_vault_tags(ws_id):
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT tag, COUNT(*) AS cnt FROM vault WHERE ws_id = ? GROUP BY tag ORDER BY cnt DESC",
            (ws_id,),
        ).fetchall()
        return [(r["tag"], r["cnt"]) for r in rows]


def get_vault_items_by_tag(ws_id, tag):
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT content, added_by, created_at FROM vault WHERE ws_id = ? AND tag = ? ORDER BY id DESC LIMIT 15",
            (ws_id, tag),
        ).fetchall()
        return [dict(r) for r in rows]


def search_vault(ws_id, keyword):
    q = f"%{keyword.strip()}%"
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT tag, content, added_by, created_at FROM vault WHERE ws_id = ? AND (content LIKE ? OR tag LIKE ?) ORDER BY id DESC LIMIT 15",
            (ws_id, q, q),
        ).fetchall()
        return [dict(r) for r in rows]


# --- المراقبات (ذهب / منتجات / وظائف) ---
def add_watcher(ws_id, w_type, target_url, criteria, last_value="", title=""):
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO watchers (ws_id, w_type, target_url, criteria, last_value, title) VALUES (?, ?, ?, ?, ?, ?)",
            (ws_id, w_type, target_url, str(criteria), str(last_value), title),
        )


def get_watchers(ws_id=None, w_type=None):
    with _db_lock, get_conn() as conn:
        if ws_id and w_type:
            rows = conn.execute("SELECT * FROM watchers WHERE ws_id = ? AND w_type = ? ORDER BY id DESC", (ws_id, w_type)).fetchall()
        elif ws_id:
            rows = conn.execute("SELECT * FROM watchers WHERE ws_id = ? ORDER BY id DESC", (ws_id,)).fetchall()
        elif w_type:
            rows = conn.execute("SELECT * FROM watchers WHERE w_type = ? ORDER BY id DESC", (w_type,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM watchers ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def delete_watcher(w_id):
    with _db_lock, get_conn() as conn:
        conn.execute("DELETE FROM watchers WHERE id = ?", (int(w_id),))


def is_link_seen(ws_id, url):
    with _db_lock, get_conn() as conn:
        row = conn.execute("SELECT 1 FROM seen_links WHERE ws_id = ? AND url = ?", (ws_id, url)).fetchone()
        if row:
            return True
        conn.execute("INSERT INTO seen_links (ws_id, url) VALUES (?, ?)", (ws_id, url))
        return False