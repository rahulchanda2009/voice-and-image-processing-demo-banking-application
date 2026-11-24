from __future__ import annotations

import uuid
from typing import Any

from app import llm_client
from app.config import settings
from app.db import insert_customer, insert_vcip, update_vcip_status
from app.face_match import match_faces


def _last4(value: Any) -> str:
    s = str(value or "")
    return f"****{s[-4:]}" if len(s) > 4 else s


def analyze(
    card_bytes: bytes,
    card_mime: str,
    live_bytes: bytes,
    liveness: dict[str, Any],
    specimen: bool,
    trace,
) -> dict[str, Any]:
    fields, dt_card, raw, card_prompt = llm_client.extract_card_fields(card_bytes, card_mime)
    trace.llm(
        name="gemini_card_read",
        model=settings.gemini_model,
        messages=[
            {
                "role": "user",
                "content": card_prompt
                + "\n\n[Specimen card image attached in the original request. The image stays on the laptop and is not exported.]",
            }
        ],
        output={"role": "assistant", "content": raw},
        duration_s=dt_card,
        metadata={"step": "card_read", "specimen": specimen},
    )

    motion = float(liveness.get("motion_score") or 0)
    blinks = int(liveness.get("blinks") or 0)
    turned = bool(liveness.get("head_turn"))
    liveness_passed = bool(liveness.get("passed")) or motion >= settings.liveness_min_motion or blinks >= 2 or turned
    trace.tool(
        name="liveness_check",
        input_data={
            "motion_score": motion,
            "blinks": blinks,
            "head_turn": turned,
            "min_motion": settings.liveness_min_motion,
        },
        output={"passed": liveness_passed},
        duration_s=float(liveness.get("duration_s") or 0),
        metadata={"runs_on": "local_browser"},
    )

    face = match_faces(card_bytes, live_bytes, specimen=specimen)
    face_score = float(face.get("score") or 0)
    face_passed = bool(
        face.get("card_face_found") and face.get("live_face_found") and face_score >= settings.face_match_threshold
    )
    trace.tool(
        name="face_match",
        input_data={
            "backend": face.get("backend"),
            "threshold": settings.face_match_threshold,
            "images_exported": False,
        },
        output={"score": round(face_score, 4), "passed": face_passed},
        duration_s=face.get("duration_s") or 0,
        metadata={"runs_on": "local"},
    )

    recommendation = "approve" if (liveness_passed and face_passed and fields.get("name")) else "review"
    summary_input = {
        "name": fields.get("name"),
        "dob": fields.get("dob"),
        "id_type": fields.get("id_type"),
        "id_number_last4": _last4(fields.get("id_number")),
        "liveness_passed": liveness_passed,
        "face_score": round(face_score, 3),
        "face_threshold": settings.face_match_threshold,
        "system_recommendation": recommendation,
    }
    summary, dt_sum, summary_prompt = llm_client.vcip_summary(summary_input)
    trace.llm(
        name="gemini_vcip_summary",
        model=settings.gemini_model,
        messages=[{"role": "user", "content": summary_prompt}],
        output={"role": "assistant", "content": summary},
        duration_s=dt_sum,
        metadata={"step": "summary", "ai_recommendation": recommendation},
    )

    vcip_id = "VCIP-" + uuid.uuid4().hex[:8].upper()
    insert_vcip(
        {
            "id": vcip_id,
            "name": fields.get("name"),
            "dob": fields.get("dob"),
            "id_type": fields.get("id_type"),
            "id_number": fields.get("id_number"),
            "liveness_passed": liveness_passed,
            "motion_score": motion,
            "face_score": face_score,
            "summary": summary,
            "status": "pending",
        }
    )
    return {
        "vcip_id": vcip_id,
        "fields": fields,
        "liveness_passed": liveness_passed,
        "face": face,
        "face_passed": face_passed,
        "ai_recommendation": recommendation,
        "summary": summary,
    }


def decide(vcip_id: str, approved: bool, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    status = "approved" if approved else "rejected"
    row = update_vcip_status(vcip_id, status)
    created_customer_id = None
    if approved and fields and fields.get("name"):
        created_customer_id = "CUS" + uuid.uuid4().hex[:6].upper()
        id_type = str(fields.get("id_type") or "").upper()
        insert_customer(
            {
                "id": created_customer_id,
                "name": fields.get("name"),
                "mobile": "9000000000",
                "email": "new.customer@example.com",
                "account_number": "50100" + uuid.uuid4().hex[:7],
                "ifsc": "SAHY0001999",
                "balance": 1000.0,
                "debit_card_last4": "0000",
                "credit_card_last4": "0000",
                "loan_type": None,
                "loan_outstanding": 0,
                "loan_emi": 0,
                "dummy_aadhaar": fields.get("id_number") if id_type == "AADHAAR" else "9999 0000 1111",
                "dummy_pan": fields.get("id_number") if id_type == "PAN" else "DEMOD1234X",
                "dob": fields.get("dob"),
                "city": "Mumbai",
                "voice_embedding": None,
            }
        )
    return {"vcip": row, "status": status, "created_customer_id": created_customer_id}