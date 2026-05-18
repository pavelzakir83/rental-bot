import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "/data/rental.db")

@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def init():
    dirname = os.path.dirname(DB_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                name         TEXT NOT NULL,
                tenant_name  TEXT NOT NULL,
                plan_rent    REAL NOT NULL DEFAULT 0,
                due_day      INTEGER NOT NULL DEFAULT 10,
                active       INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS rent_payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id  INTEGER NOT NULL REFERENCES objects(id),
                amount     REAL NOT NULL,
                date       TEXT NOT NULL,
                period     TEXT NOT NULL,
                sender     TEXT,
                notes      TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS utility_bills (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id     INTEGER NOT NULL REFERENCES objects(id),
                amount        REAL NOT NULL,
                period        TEXT NOT NULL,
                received_date TEXT NOT NULL,
                notes         TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS utility_payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id  INTEGER NOT NULL REFERENCES objects(id),
                bill_id    INTEGER REFERENCES utility_bills(id),
                amount     REAL NOT NULL,
                date       TEXT NOT NULL,
                period     TEXT NOT NULL,
                sender     TEXT,
                notes      TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_obj_chat    ON objects(chat_id);
            CREATE INDEX IF NOT EXISTS idx_rp_obj_per  ON rent_payments(object_id, period);
            CREATE INDEX IF NOT EXISTS idx_ub_obj_per  ON utility_bills(object_id, period);
            CREATE INDEX IF NOT EXISTS idx_up_obj_per  ON utility_payments(object_id, period);
        """)
    print(f"✅ БД инициализирована: {DB_PATH}")

# ── Objects ──────────────────────────────────────────────────────────────────

def upsert_object(chat_id: int, name: str, tenant_name: str,
                  plan_rent: float, due_day: int) -> tuple[int, str]:
    """Вставляет или обновляет объект. Возвращает (id, 'created'|'updated'|'unchanged')."""
    with conn() as c:
        row = c.execute(
            "SELECT * FROM objects WHERE chat_id=? AND tenant_name=?",
            (chat_id, tenant_name)
        ).fetchone()
        if not row:
            r = c.execute(
                "INSERT INTO objects (chat_id,name,tenant_name,plan_rent,due_day) VALUES (?,?,?,?,?)",
                (chat_id, name, tenant_name, plan_rent, due_day)
            )
            return r.lastrowid, "created"
        changed = (row["name"] != name or row["plan_rent"] != plan_rent
                   or row["due_day"] != due_day or not row["active"])
        if changed:
            c.execute(
                "UPDATE objects SET name=?,plan_rent=?,due_day=?,active=1 WHERE id=?",
                (name, plan_rent, due_day, row["id"])
            )
            return row["id"], "updated"
        return row["id"], "unchanged"

def get_objects(chat_id: int) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM objects WHERE chat_id=? AND active=1 ORDER BY name",
            (chat_id,)
        ).fetchall()]

def get_object(obj_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM objects WHERE id=?", (obj_id,)).fetchone()
        return dict(r) if r else None

def get_all_active_objects() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM objects WHERE active=1"
        ).fetchall()]

def find_object_by_tenant(chat_id: int, sender_name: str) -> dict | None:
    """Ищет объект по имени арендатора (частичное совпадение)."""
    if not sender_name:
        return None
    objects = get_objects(chat_id)
    s = sender_name.lower().strip()
    for o in objects:
        t = o["tenant_name"].lower().strip()
        if s == t or s in t or t in s:
            return o
    return None

# ── Rent Payments ────────────────────────────────────────────────────────────

def add_rent_payment(object_id: int, amount: float, date: str,
                     period: str, sender: str = None, notes: str = None) -> int:
    with conn() as c:
        r = c.execute(
            "INSERT INTO rent_payments (object_id,amount,date,period,sender,notes) VALUES (?,?,?,?,?,?)",
            (object_id, amount, date, period, sender, notes)
        )
        return r.lastrowid

def get_rent_payments(object_id: int, period: str = None) -> list[dict]:
    with conn() as c:
        if period:
            rows = c.execute(
                "SELECT * FROM rent_payments WHERE object_id=? AND period=? ORDER BY date",
                (object_id, period)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM rent_payments WHERE object_id=? ORDER BY date DESC",
                (object_id,)
            ).fetchall()
        return [dict(r) for r in rows]

def get_rent_paid(object_id: int, period: str) -> float:
    with conn() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(amount),0) as total FROM rent_payments WHERE object_id=? AND period=?",
            (object_id, period)
        ).fetchone()
        return r["total"]

def has_rent_payment(object_id: int, period: str) -> bool:
    return get_rent_paid(object_id, period) > 0

# ── Utility Bills ────────────────────────────────────────────────────────────

def add_utility_bill(object_id: int, amount: float, period: str,
                     received_date: str, notes: str = None) -> int:
    with conn() as c:
        r = c.execute(
            "INSERT INTO utility_bills (object_id,amount,period,received_date,notes) VALUES (?,?,?,?,?)",
            (object_id, amount, period, received_date, notes)
        )
        return r.lastrowid

def get_utility_bills(object_id: int, period: str = None) -> list[dict]:
    with conn() as c:
        if period:
            rows = c.execute(
                "SELECT * FROM utility_bills WHERE object_id=? AND period=? ORDER BY received_date",
                (object_id, period)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM utility_bills WHERE object_id=? ORDER BY period DESC",
                (object_id,)
            ).fetchall()
        return [dict(r) for r in rows]

def has_utility_bill(object_id: int, period: str) -> bool:
    with conn() as c:
        r = c.execute(
            "SELECT COUNT(*) as n FROM utility_bills WHERE object_id=? AND period=?",
            (object_id, period)
        ).fetchone()
        return r["n"] > 0

def find_matching_bill(object_id: int, period: str, amount: float) -> dict | None:
    """Ищет квитанцию с точным совпадением суммы."""
    with conn() as c:
        r = c.execute(
            """SELECT ub.* FROM utility_bills ub
               LEFT JOIN utility_payments up ON up.bill_id = ub.id
               WHERE ub.object_id=? AND ub.period=? AND ub.amount=? AND up.id IS NULL
               LIMIT 1""",
            (object_id, period, amount)
        ).fetchone()
        return dict(r) if r else None

# ── Utility Payments ─────────────────────────────────────────────────────────

def add_utility_payment(object_id: int, bill_id: int | None, amount: float,
                        date: str, period: str, sender: str = None, notes: str = None) -> int:
    with conn() as c:
        r = c.execute(
            "INSERT INTO utility_payments (object_id,bill_id,amount,date,period,sender,notes) VALUES (?,?,?,?,?,?,?)",
            (object_id, bill_id, amount, date, period, sender, notes)
        )
        return r.lastrowid

def get_utility_payments(object_id: int, period: str = None) -> list[dict]:
    with conn() as c:
        if period:
            rows = c.execute(
                "SELECT * FROM utility_payments WHERE object_id=? AND period=? ORDER BY date",
                (object_id, period)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM utility_payments WHERE object_id=? ORDER BY period DESC",
                (object_id,)
            ).fetchall()
        return [dict(r) for r in rows]
