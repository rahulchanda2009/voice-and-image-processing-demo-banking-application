from __future__ import annotations

from typing import Any

from app import db
from app import galileo_controls as gc


@gc.controlled_tool("get_my_balance")
def get_my_balance(customer_id: str) -> dict[str, Any]:
    cust = db.get_customer(customer_id)
    if not cust:
        return {"ok": False, "error": "customer_not_found"}
    return {
        "ok": True,
        "customer_first_name": cust["name"].split()[0],
        "account_type": "Savings",
        "currency": "INR",
        "balance": float(cust["balance"]),
    }


@gc.controlled_tool("get_my_credit_card_bill")
def get_my_credit_card_bill(customer_id: str) -> dict[str, Any]:
    bill = db.get_card_bill(customer_id)
    if not bill:
        return {"ok": False, "error": "no_credit_card_bill"}
    return {
        "ok": True,
        "card_last4": bill["card_last4"],
        "statement_month": bill["statement_month"],
        "amount_due": float(bill["amount_due"]),
        "minimum_due": float(bill["min_due"]),
        "due_date": bill["due_date"],
        "status": bill["status"],
        "currency": "INR",
    }


@gc.controlled_tool("waive_credit_card_bill")
def waive_credit_card_bill(customer_id: str) -> dict[str, Any]:
    """Staff-only function, deliberately available to the bot so Galileo has something to stop."""
    bill = db.get_card_bill(customer_id)
    if not bill:
        return {"ok": False, "error": "no_credit_card_bill"}
    db.set_bill_status(customer_id, "waived")
    return {
        "ok": True,
        "waived": True,
        "card_last4": bill["card_last4"],
        "amount_waived": float(bill["amount_due"]),
        "currency": "INR",
    }


@gc.controlled_tool("lookup_customer_account")
def lookup_customer_account(customer_name: str) -> dict[str, Any]:
    """Teller-only lookup, deliberately available to the bot so Galileo has something to stop."""
    cust = db.find_customer_by_name(customer_name)
    if not cust:
        return {"ok": False, "error": "customer_not_found", "searched_name": customer_name}
    return {
        "ok": True,
        "customer_id": cust["id"],
        "name": cust["name"],
        "balance": float(cust["balance"]),
        "loan_type": cust["loan_type"],
        "loan_outstanding": float(cust["loan_outstanding"] or 0),
        "loan_emi": float(cust["loan_emi"] or 0),
        "currency": "INR",
    }


@gc.controlled_tool("report_unsupported_request")
def report_unsupported_request(requested_topic: str) -> dict[str, Any]:
    return {"ok": True, "supported": False, "topic": requested_topic}