from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import settings

BILL_AMOUNTS = {
    "CUS1001": 23410.00,
    "CUS1002": 18450.00,
    "CUS1003": 7820.50,
    "CUS1004": 31200.00,
    "CUS1005": 4650.00,
    "CUS1006": 12990.00,
    "CUS1007": 9875.25,
    "CUS1008": 2340.00,
    "CUS1009": 45600.00,
    "CUS1010": 15780.00,
}
BILL_STATEMENT_MONTH = "Oct 2026"
BILL_DUE_DATE = "2026-11-25"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _seed_bills(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, credit_card_last4 FROM customers").fetchall()
    for r in rows:
        amount = BILL_AMOUNTS.get(r["id"], 5000.00)
        conn.execute(
            """
            INSERT OR IGNORE INTO credit_card_bills
            (customer_id, card_last4, statement_month, amount_due, min_due, due_date, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'due', ?)
            """,
            (r["id"], r["credit_card_last4"] or "0000", BILL_STATEMENT_MONTH, amount, round(amount * 0.05, 2), BILL_DUE_DATE, utcnow()),
        )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mobile TEXT,
                email TEXT,
                account_number TEXT,
                ifsc TEXT,
                balance REAL DEFAULT 0,
                debit_card_last4 TEXT,
                credit_card_last4 TEXT,
                loan_type TEXT,
                loan_outstanding REAL DEFAULT 0,
                loan_emi REAL DEFAULT 0,
                dummy_aadhaar TEXT,
                dummy_pan TEXT,
                dob TEXT,
                city TEXT,
                voice_embedding TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS vcip_applications (
                id TEXT PRIMARY KEY,
                name TEXT,
                dob TEXT,
                id_type TEXT,
                id_number TEXT,
                liveness_passed INTEGER,
                motion_score REAL,
                face_score REAL,
                summary TEXT,
                status TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS credit_card_bills (
                customer_id TEXT PRIMARY KEY,
                card_last4 TEXT,
                statement_month TEXT,
                amount_due REAL,
                min_due REAL,
                due_date TEXT,
                status TEXT DEFAULT 'due',
                updated_at TEXT
            );
            """
        )
        _seed_bills(conn)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def list_customers() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, mobile, city, balance, voice_embedding IS NOT NULL AS enrolled FROM customers ORDER BY name"
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["enrolled"] = bool(item["enrolled"])
        out.append(item)
    return out


def get_customer(customer_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return row_to_dict(row)


def find_customer_by_name(name: str) -> dict[str, Any] | None:
    q = " ".join(str(name or "").lower().split())
    if not q:
        return None
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM customers").fetchall()]
    for r in rows:
        if r["name"].lower() == q:
            return r
    for r in rows:
        n = r["name"].lower()
        if q in n or n in q:
            return r
    tokens = [t for t in q.split() if len(t) > 2]
    for r in rows:
        parts = r["name"].lower().split()
        if any(t in parts for t in tokens):
            return r
    return None


def save_voice_embedding(customer_id: str, embedding: list[float]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE customers SET voice_embedding = ? WHERE id = ?",
            (json.dumps(embedding), customer_id),
        )


def get_voice_embedding(customer_id: str) -> list[float] | None:
    cust = get_customer(customer_id)
    if not cust or not cust.get("voice_embedding"):
        return None
    return json.loads(cust["voice_embedding"])


def insert_customer(data: dict[str, Any]) -> None:
    cols = [
        "id",
        "name",
        "mobile",
        "email",
        "account_number",
        "ifsc",
        "balance",
        "debit_card_last4",
        "credit_card_last4",
        "loan_type",
        "loan_outstanding",
        "loan_emi",
        "dummy_aadhaar",
        "dummy_pan",
        "dob",
        "city",
        "voice_embedding",
        "created_at",
    ]
    payload = {c: data.get(c) for c in cols}
    payload["created_at"] = payload.get("created_at") or utcnow()
    placeholders = ", ".join(["?"] * len(cols))
    with connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO customers ({', '.join(cols)}) VALUES ({placeholders})",
            [payload[c] for c in cols],
        )


def insert_vcip(data: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO vcip_applications
            (id, name, dob, id_type, id_number, liveness_passed, motion_score, face_score, summary, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["id"],
                data.get("name"),
                data.get("dob"),
                data.get("id_type"),
                data.get("id_number"),
                1 if data.get("liveness_passed") else 0,
                data.get("motion_score"),
                data.get("face_score"),
                data.get("summary"),
                data.get("status", "pending"),
                utcnow(),
            ),
        )


def update_vcip_status(vcip_id: str, status: str) -> dict[str, Any] | None:
    with connect() as conn:
        conn.execute("UPDATE vcip_applications SET status = ? WHERE id = ?", (status, vcip_id))
        row = conn.execute("SELECT * FROM vcip_applications WHERE id = ?", (vcip_id,)).fetchone()
    return row_to_dict(row)


def get_card_bill(customer_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM credit_card_bills WHERE customer_id = ?", (customer_id,)).fetchone()
    return row_to_dict(row)


def set_bill_status(customer_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE credit_card_bills SET status = ?, updated_at = ? WHERE customer_id = ?",
            (status, utcnow(), customer_id),
        )


def list_bills() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT b.customer_id, c.name, b.card_last4, b.statement_month, b.amount_due, b.due_date, b.status, b.updated_at
            FROM credit_card_bills b JOIN customers c ON c.id = b.customer_id
            ORDER BY c.name
            """
        ).fetchall()
    return [dict(r) for r in rows]


def reset_bills() -> None:
    with connect() as conn:
        conn.execute("UPDATE credit_card_bills SET status = 'due', updated_at = ?", (utcnow(),))
        _seed_bills(conn)