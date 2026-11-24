from __future__ import annotations

import json
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app import galileo_controls as gc
from app import vcip as vcip_mod
from app.config import ROOT, settings
from app.db import get_customer, get_voice_embedding, init_db, list_customers, save_voice_embedding
from app.eval_cases import RAG_CASES, SPECIMEN_FIELDS
from app.face_match import backend_name as face_backend
from app.galileo_tracing import _get_logger, heartbeat, start_trace
from app.kb import get_retriever, search_kb
from app.llm_client import rag_answer, rag_provider, reply_provider
from app.voice_auth import average_embeddings, save_wav_copy, timed_embed, verify_score
from app.voice_bot import handle_voice_turn

init_db()

KB_BLOCKED = "This request was blocked by the bank's safety controls."
KB_UNAVAILABLE = "Safety controls are unavailable right now, so no answer was generated. Please try again shortly."


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await run_in_threadpool(lambda: gc.init(_get_logger()))
    yield


app = FastAPI(title="Sahayak Bank Demo", version="3.1.0", lifespan=lifespan)
SESSIONS: dict[str, dict[str, Any]] = {}
VCIP_CACHE: dict[str, dict[str, Any]] = {}

static_dir = ROOT / "static"
static_dir.mkdir(exist_ok=True)
(ROOT / "data" / "kb").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def index():
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "bank": settings.bank_name,
        "gemini_key_present": bool(settings.gemini_api_key),
        "gemini_model": settings.gemini_model,
        "rag_provider": rag_provider(),
        "reply_provider": reply_provider(),
        "galileo": settings.galileo_enabled,
        "galileo_project": settings.galileo_project,
        "galileo_log_stream": settings.galileo_log_stream,
        "galileo_console_url": settings.galileo_console_url,
        "galileo_log_stream_url": settings.galileo_log_stream_url or settings.galileo_console_url,
        "agent_control": gc.status(),
        "face_backend": face_backend(),
        "voice_backend": "mfcc_cosine",
        "kb_articles": len(get_retriever().articles),
        "passphrase_hi": settings.passphrase_hi,
    }


@app.get("/api/galileo/ping")
def galileo_ping():
    return heartbeat()


@app.get("/api/customers")
def customers():
    rows = list_customers()
    for r in rows:
        r.pop("balance", None)
    return {"customers": rows}


@app.get("/api/bills")
def bills():
    return {"bills": db.list_bills()}


@app.post("/api/bills/reset")
def bills_reset():
    db.reset_bills()
    return {"ok": True, "bills": db.list_bills()}


@app.post("/api/voice/enroll")
async def enroll(customer_id: str = Form(...), sample1: UploadFile = File(...),
                 sample2: UploadFile = File(...), sample3: UploadFile = File(...)):
    cust = get_customer(customer_id)
    if not cust:
        raise HTTPException(status_code=404, detail="Unknown customer")
    vectors = []
    with start_trace("Voice Enrollment", {"customer_id": customer_id, "samples": 3},
                     metadata={"use_case": "voice_enrollment", "customer_id": customer_id}, tags=["Voice AI"]) as trace:
        for i, f in enumerate([sample1, sample2, sample3], start=1):
            data = await f.read()
            save_wav_copy(customer_id, i, data)
            try:
                vec, dt = timed_embed(data, f.filename or f"sample{i}.wav")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Sample {i}: {exc}")
            vectors.append(vec)
            trace.tool(name=f"voice_embedding_sample_{i}", input_data={"audio_bytes": len(data), "audio_exported": False},
                       output={"embedding_dim": int(vec.shape[0])}, duration_s=dt)
        save_voice_embedding(customer_id, average_embeddings(vectors).tolist())
        trace.output = {"enrolled": True, "customer_id": customer_id}
    return {"ok": True, "customer_id": customer_id, "name": cust["name"], "samples": 3}


@app.post("/api/voice/verify")
async def verify(customer_id: str = Form(...), audio: UploadFile = File(...)):
    cust = get_customer(customer_id)
    if not cust:
        raise HTTPException(status_code=404, detail="Unknown customer")
    enrolled = get_voice_embedding(customer_id)
    if not enrolled:
        raise HTTPException(status_code=400, detail="Customer has no enrolled voice. Enroll 3 samples first.")
    data = await audio.read()
    with start_trace("Voice Auth Session", {"customer_id": customer_id},
                     metadata={"use_case": "voice_auth", "customer_id": customer_id,
                               "threshold": settings.voice_match_threshold}, tags=["Voice AI"]) as trace:
        try:
            vec, dt = timed_embed(data, audio.filename or "verify.wav")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        score = verify_score(enrolled, vec)
        passed = score >= settings.voice_match_threshold
        trace.tool(name="speaker_verify",
                   input_data={"customer_id": customer_id, "threshold": settings.voice_match_threshold, "audio_exported": False},
                   output={"score": round(score, 4), "verified": passed}, duration_s=dt)
        token = None
        if passed:
            token = secrets.token_urlsafe(24)
            SESSIONS[token] = {"customer_id": customer_id, "name": cust["name"]}
        trace.output = {"verified": passed, "score": round(score, 4)}
    first = cust["name"].split()[0]
    return {
        "verified": passed, "score": round(score, 4), "threshold": settings.voice_match_threshold,
        "session_token": token, "customer": {"id": customer_id, "name": cust["name"]},
        "message_hi": f"नमस्ते {first}, आपकी पहचान सत्यापित हो गई है।" if passed else "आवाज़ मेल नहीं खाई। कृपया फिर से प्रयास करें।",
        "message_en": f"Hello {first}, your identity is verified." if passed else "Voice did not match. Please try again.",
    }


@app.post("/api/bot/query")
def bot_query(session_token: str = Form(""), language: str = Form("hi"), audio: UploadFile | None = File(None)):
    # Plain def: FastAPI runs it in a worker thread, which the Agent Control SDK requires.
    lang = language if language in ("hi", "en") else "hi"
    sess = SESSIONS.get(session_token or "")
    meta = {"use_case": "voice_bot", "language": lang, "voice_verified": bool(sess),
            "session_customer_id": sess["customer_id"] if sess else None}
    tags = ["Voice AI", "Agent", "Hindi" if lang == "hi" else "English"]
    with start_trace("Voice Bot Turn", {"channel": "voice", "language": lang, "voice_verified": bool(sess)},
                     metadata=meta, tags=tags) as trace:
        if not sess:
            trace.tool(name="authorization_check",
                       input_data={"session_token_present": bool(session_token), "requested": "voice_bot"},
                       output={"allowed": False, "reason": "voice_authentication_required"},
                       duration_s=0.0, status_code=401)
            raise HTTPException(status_code=401, detail="Voice authentication required")
        if audio is None:
            raise HTTPException(status_code=400, detail="Audio is required")
        trace.tool(name="authorization_check",
                   input_data={"session_token_present": True, "session_customer_id": sess["customer_id"]},
                   output={"allowed": True}, duration_s=0.0)
        data = audio.file.read()
        result = handle_voice_turn(sess["customer_id"], data, audio.content_type or "audio/wav", lang, trace)
        trace.output = result
    return {**result, "customer_name": sess["name"]}


@app.post("/api/vcip/analyze")
async def vcip_analyze(card: UploadFile = File(...), live_face: UploadFile = File(...),
                       liveness_json: str = Form("{}"), specimen: str = Form("false")):
    card_bytes = await card.read()
    live_bytes = await live_face.read()
    try:
        liveness = json.loads(liveness_json)
    except json.JSONDecodeError:
        liveness = {}
    specimen_flag = str(specimen).lower() in {"1", "true", "yes"}
    meta: dict[str, Any] = {"use_case": "vcip", "specimen": specimen_flag}
    if specimen_flag:
        meta["reference_fields"] = SPECIMEN_FIELDS
    with start_trace("V-CIP Session", {"card_image_bytes": len(card_bytes), "images_exported": False},
                     metadata=meta, tags=["Document AI"]) as trace:
        result = vcip_mod.analyze(card_bytes, card.content_type or "image/jpeg", live_bytes, liveness, specimen_flag, trace)
        trace.output = {"fields": result["fields"], "ai_recommendation": result["ai_recommendation"], "summary": result["summary"]}
    VCIP_CACHE[result["vcip_id"]] = result
    face_dir = settings.faces_dir / result["vcip_id"]
    face_dir.mkdir(parents=True, exist_ok=True)
    (face_dir / "card.jpg").write_bytes(card_bytes)
    (face_dir / "live.jpg").write_bytes(live_bytes)
    return result


@app.post("/api/vcip/decision")
async def vcip_decision(vcip_id: str = Form(...), approve: bool = Form(...)):
    cached = VCIP_CACHE.get(vcip_id) or {}
    ai_rec = cached.get("ai_recommendation", "unknown")
    official = "approve" if approve else "reject"
    with start_trace("V-CIP Decision", {"vcip_id": vcip_id, "official_decision": official},
                     metadata={"use_case": "vcip_decision", "vcip_id": vcip_id,
                               "ai_recommendation": ai_rec, "official_decision": official},
                     tags=["Document AI"]) as trace:
        out = vcip_mod.decide(vcip_id, approve, cached.get("fields"))
        trace.tool(name="official_decision", input_data={"ai_recommendation": ai_rec, "official_decision": official},
                   output={"status": out["status"], "created_customer_id": out.get("created_customer_id")}, duration_s=0.0)
        trace.output = {"status": out["status"], "ai_recommendation": ai_rec, "official_decision": official}
    return out


@app.post("/api/kb/query")
def kb_query(question: str = Form(...), test_case_id: str = Form("")):
    # Plain def: FastAPI runs it in a worker thread, which the Agent Control SDK requires.
    case = next((c for c in RAG_CASES if c["id"] == test_case_id), None)
    meta: dict[str, Any] = {"use_case": "kb_rag", "test_case_id": test_case_id or "live", "rag_provider_configured": rag_provider()}
    if case:
        meta["expected_relevant"] = case["relevant"]
    with start_trace("KB RAG Turn", question, metadata=meta, tags=["RAG", "Agent"]) as trace:
        t0 = time.perf_counter()
        hits = search_kb(question, k=3)
        docs = [{"content": f"[{h['id']}] {h['title']}\n{h['snippet']}", "metadata": {"id": h["id"], "score": h["score"]}} for h in hits]
        trace.retriever(name="search_kb", query=question, documents=docs, duration_s=time.perf_counter() - t0,
                        metadata={"k": 3, "retrieved_ids": [h["id"] for h in hits]})
        out = rag_answer(question, hits)
        decision = out["decision"]
        if decision == "allow":
            answer = out["result"]
        elif decision == "unavailable":
            answer = KB_UNAVAILABLE
        else:
            answer = KB_BLOCKED
        trace.llm(name="gemini_rag_answer", model=out.get("model") or "not-called", messages=out["messages"],
                  output={"role": "assistant", "content": answer}, duration_s=out.get("latency_s") or 0,
                  metadata={"step": "rag_answer", "provider": out.get("provider"),
                            "galileo_decision": decision, "galileo_control": out.get("control")},
                  tags=[out.get("provider") or "blocked"])
        trace.output = answer
    return {"question": question, "hits": hits, "answer": answer, "model": out.get("model"),
            "provider": out.get("provider"), "galileo_decision": decision, "galileo_control": out.get("control")}