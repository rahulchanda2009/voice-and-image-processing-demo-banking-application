from __future__ import annotations

import json
import re
from typing import Any

AADHAAR_RE = re.compile(r"\b(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})\b")
PAN_RE = re.compile(r"\b([A-Z]{5}\d{4}[A-Z])\b")
PHONE_RE = re.compile(r"\b(?:\+91[\s-]?)?[6-9]\d{9}\b")
ACCOUNT_RE = re.compile(r"\b\d{11,18}\b")
CARD_RE = re.compile(r"\b(?:\d{4}[\s-]?){3}\d{4}\b")
AMOUNT_RE = re.compile(r"(?:₹|INR|Rs\.?)\s*[\d,]+(?:\.\d{1,2})?", re.I)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)

IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")


def _mask_aadhaar(m: re.Match) -> str:
    return f"XXXX XXXX {m.group(3)}"


def mask_text(text: str | None) -> str:
    if not text:
        return ""
    out = str(text)
    out = AADHAAR_RE.sub(_mask_aadhaar, out)
    out = PAN_RE.sub(lambda m: m.group(1)[:5] + "****" + m.group(1)[-1], out)
    out = CARD_RE.sub("XXXX-XXXX-XXXX-XXXX", out)
    out = PHONE_RE.sub("XXXXXX1234", out)
    out = ACCOUNT_RE.sub("XXXXXXXX1234", out)
    out = IFSC_RE.sub("XXXX0XXXXXX", out)
    out = AMOUNT_RE.sub("₹XX,XXX", out)
    out = EMAIL_RE.sub("***@***.com", out)
    return out


def mask_any(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [mask_any(v) for v in value]
    if isinstance(value, dict):
        sensitive = {
            "aadhaar",
            "dummy_aadhaar",
            "pan",
            "dummy_pan",
            "id_number",
            "mobile",
            "phone",
            "email",
            "account_number",
            "balance",
            "loan_outstanding",
            "loan_emi",
            "debit_card_last4",
            "credit_card_last4",
            "embedding",
            "voice_embedding",
            "face_encoding",
        }
        out = {}
        for k, v in value.items():
            if k.lower() in sensitive:
                out[k] = "***"
            else:
                out[k] = mask_any(v)
        return out
    try:
        return mask_text(json.dumps(value, ensure_ascii=False))
    except Exception:
        return "***"