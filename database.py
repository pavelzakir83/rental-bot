import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "rental.db")

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
    with conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                name         TEXT NOT NULL,
                plan_rent    REAL NOT NULL DEFAULT 0,
                plan_utility REAL NOT NULL DEFAULT 0,
                due_day      INTEGER NOT NULL DEFAULT 10,
                active       INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS payments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id    INTEGER NOT NULL REFERENCES objects(id),
                amount       REAL NOT NULL,
                payment_type TEXT NOT NULL DEFAULT 'unknown',
                date         TEXT NOT NULL,
                period       TEXT NOT NULL,
                payer        TEXT,
                notes        TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_obj_chat ON objects(chat_id);
            CREATE INDEX IF NOT EXISTS idx_pay_obj_period ON payments(object_id, period);
        """)

# ── Objects ──────────────────────────────────────────────────

def add_object(chat_id, name, plan_rent, plan_utility, due_day):
    with conn() as c:
        r = c.execute(
            "INSERT INTO objects (chat_id,name,plan_rent,plan_utility,due_day) VALUES (?,?,?,?,?)",
            (chat_id, name, plan_rent, plan_utility, due_day)
        )
        return r.lastrowid

def get_objects(chat_id):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM objects WHERE chat_id=? AND active=1 ORDER BY name", (chat_id,)
        ).fetchall()]

def get_object(obj_id):
    with conn() as c:
        r = c.execute("SELECT * FROM objects WHERE id=?", (obj_id,)).fetchone()
        return dict(r) if r else None

def update_object(obj_id, name, plan_rent, plan_utility, due_day):
    with conn() as c:
        c.execute(
            "UPDATE objects SET name=?,plan_rent=?,plan_utility=?,due_day=? WHERE id=?",
            (name, plan_rent, plan_utility, due_day, obj_id)
        )

def delete_object(obj_id):
    with conn() as c:
        c.execute("UPDATE objects SET active=0 WHERE id=?", (obj_id,))

def get_all_active_objects():
    """Для планировщика напоминаний — все объекты всех пользователей."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM objects WHERE active=1"
        ).fetchall()]

# ── Payments ─────────────────────────────────────────────────

def add_payment(object_id, amount, payment_type, date, period, payer=None, notes=None):
    with conn() as c:
        r = c.execute(
            "INSERT INTO payments (object_id,amount,payment_type,date,period,payer,notes) VALUES (?,?,?,?,?,?,?)",
            (object_id, amount, payment_type, date, period, payer, notes)
        )
        return r.lastrowid

def get_monthly_summary(object_id, period):
    with conn() as c:
        rows = c.execute(
            "SELECT payment_type, amount FROM payments WHERE object_id=? AND period=?",
            (object_id, period)
        ).fetchall()
    paid_rent = paid_util = paid_both = 0.0
    for r in rows:
        t, a = r["payment_type"], r["amount"]
        if t == "rent":      paid_rent += a
        elif t == "utility": paid_util += a
        else:                paid_both += a
    paid_rent += paid_both / 2
    paid_util += paid_both / 2
    return {"paid_rent": round(paid_rent, 2), "paid_utility": round(paid_util, 2)}

def get_payments(object_id, period=None):
    with conn() as c:
        if period:
            rows = c.execute(
                "SELECT * FROM payments WHERE object_id=? AND period=? ORDER BY date",
                (object_id, period)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM payments WHERE object_id=? ORDER BY date DESC LIMIT 30",
                (object_id,)
            ).fetchall()
    return [dict(r) for r in rows]

def has_payment_this_period(object_id, period):
    with conn() as c:
        r = c.execute(
            "SELECT COUNT(*) as n FROM payments WHERE object_id=? AND period=?",
            (object_id, period)
        ).fetchone()
    return r["n"] > 0

def match_object(chat_id, hint: str | None):
    if not hint:
        return None
    objects = get_objects(chat_id)
    h = hint.lower()
    for o in objects:
        n = o["name"].lower()
        if h in n or n in h:
            return o
    return None
