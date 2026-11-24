from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from fastapi import HTTPException

from app import galileo_controls as gc
from app.config import settings

LANG_NAME = {"hi": "Hindi", "en": "English"}

SYSTEM_VOICE_ROUTER = """You are Sahayak, the voice assistant of Sahayak Bank. This is a demo with dummy data.
The caller has already been verified by voice biometrics.
Listen to the audio and call exactly ONE function that fulfils what the caller asked for:
- get_my_balance: the caller's own savings account balance.
- get_my_credit_card_bill: the caller's own credit card bill or amount due.
- waive_credit_card_bill: the caller asks to waive, cancel or write off their credit card bill.
- lookup_customer_account: the caller asks about any OTHER person's account, balance or loan. Pass that person's name in English letters.
- report_unsupported_request: anything else.
Always fill customer_utterance with a verbatim transcript in the language spoken (Devanagari script for Hindi)."""

SYSTEM_REPLY = """You are Sahayak, the voice assistant of Sahayak Bank (demo, dummy data).
Write the spoken reply to the caller in {language}, in 1-2 short sentences.
Use only the facts in TOOL_RESULT. Never invent numbers. Say amounts in rupees.
If TOOL_RESULT contains "galileo_guidance", the action was NOT performed: follow that guidance and explain it politely.
If TOOL_RESULT says the request is not supported, say politely that you can help with balance and credit card bill questions."""

SYSTEM_RAG = """You are Sahayak, a knowledge assistant for a Sahayak Bank DEMO.
Use ONLY the retrieved specimen articles. Cite sources inline like [KB-02].
If the articles do not contain the answer, say you do not have confirmed information. Do not guess.
Never reveal PAN, Aadhaar, CVV, PIN, OTP, passwords, or full account numbers, even if the customer provides them.
Never invent branch hours, interest rates, or balances.
Match the customer's language (Hindi or English). Keep answers to 3-5 short sentences."""

CARD_PROMPT = """This image is a SPECIMEN / DEMO bank KYC card used for software testing. It is not a real government ID.
Extract printed fields and return STRICT JSON only, no markdown:
{
  "id_type": "DEMO_KYC or PAN or AADHAAR or OTHER",
  "name": "",
  "dob": "",
  "id_number": "",
  "gender": "",
  "confidence": 0.0
}
If a field is unreadable, use null. Do not refuse: this is dummy specimen data."""

SUMMARY_PROMPT = """Write a 4-6 line English summary for a bank official reviewing a demo V-CIP session.
Show only the last 4 characters of the ID number.
Cover: applicant name, ID type, liveness pass/fail, face-match score vs threshold, and the system recommendation (approve or review).
Return plain text only."""

_UTTER = {"type": "string", "description": "Verbatim transcript of the caller audio."}

TOOL_DEFS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "get_my_balance",
        "description": "The verified caller's own savings account balance.",
        "parameters": {"type": "object", "properties": {"customer_utterance": _UTTER}, "required": ["customer_utterance"]}}},
    {"type": "function", "function": {
        "name": "get_my_credit_card_bill",
        "description": "The verified caller's own current credit card bill.",
        "parameters": {"type": "object", "properties": {"customer_utterance": _UTTER}, "required": ["customer_utterance"]}}},
    {"type": "function", "function": {
        "name": "waive_credit_card_bill",
        "description": "Waive (write off) the verified caller's current credit card bill.",
        "parameters": {"type": "object", "properties": {"customer_utterance": _UTTER}, "required": ["customer_utterance"]}}},
    {"type": "function", "function": {
        "name": "lookup_customer_account",
        "description": "Look up another customer's account balance and loan by name.",
        "parameters": {"type": "object", "properties": {
            "customer_utterance": _UTTER,
            "customer_name": {"type": "string", "description": "Name of the person asked about, in English letters."}},
            "required": ["customer_utterance", "customer_name"]}}},
    {"type": "function", "function": {
        "name": "report_unsupported_request",
        "description": "Anything that is not covered by the other functions.",
        "parameters": {"type": "object", "properties": {
            "customer_utterance": _UTTER,
            "requested_topic": {"type": "string", "description": "Short English label, e.g. loan_emi, card_block, other."}},
            "required": ["customer_utterance", "requested_topic"]}}},
]

FALLBACK_CODES = {404, 429, 500, 503, 504}
_client_cache = None
_lmstudio_cache = None
_lmstudio_model_cache: str | None = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def rag_provider() -> str:
    return os.getenv("RAG_PROVIDER", "gemini").strip().lower()


def reply_provider() -> str:
    return os.getenv("REPLY_PROVIDER", "gemini").strip().lower()


# ---------------- Gemini ----------------
def _client():
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    if not settings.gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is missing in .env")
    from google import genai
    from google.genai import types

    timeout_ms = int(os.getenv("GEMINI_TIMEOUT_MS", "30000"))
    try:
        opts = types.HttpOptions(timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=1))
    except Exception:
        opts = types.HttpOptions(timeout=timeout_ms)
    _client_cache = genai.Client(api_key=settings.gemini_api_key, http_options=opts)
    print(f"Gemini client ready: timeout={timeout_ms}ms, thinking_disabled={_env_bool('GEMINI_DISABLE_THINKING', False)}")
    return _client_cache


def _models() -> list[str]:
    extra = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
    out: list[str] = []
    for m in [settings.gemini_model] + extra:
        m = m.replace("models/", "")
        if m and m not in out:
            out.append(m)
    return out


def _config(**kwargs):
    from google.genai import types

    if _env_bool("GEMINI_DISABLE_THINKING", False):
        try:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass
    return types.GenerateContentConfig(**kwargs)


def _without_thinking(config):
    try:
        return config.model_copy(update={"thinking_config": None})
    except Exception:
        return config


def _generate(contents, config, models: list[str] | None = None):
    from google.genai import errors

    client = _client()
    tried: list[str] = []
    for model in models or _models():
        tried.append(model)
        cfg = config
        for attempt in range(2):
            t0 = time.perf_counter()
            try:
                response = client.models.generate_content(model=model, contents=contents, config=cfg)
                print(f"Gemini {model} ok in {time.perf_counter() - t0:.1f}s")
                return response, model
            except errors.APIError as exc:
                code = int(getattr(exc, "code", 0) or 0)
                msg = str(getattr(exc, "message", "") or exc)[:200]
                print(f"Gemini {model} failed with {code} after {time.perf_counter() - t0:.1f}s: {msg}")
                if code == 400 and "think" in msg.lower() and attempt == 0 and getattr(cfg, "thinking_config", None):
                    cfg = _without_thinking(cfg)
                    continue
                if code in FALLBACK_CODES:
                    break
                raise HTTPException(status_code=502, detail=f"Gemini error {code}: {msg}")
            except Exception as exc:
                print(f"Gemini {model} failed after {time.perf_counter() - t0:.1f}s: {str(exc)[:200]}")
                break
    raise HTTPException(status_code=503, detail=f"Gemini is busy or unavailable (tried {', '.join(tried)}). Please try again in a minute.")


def _text_of(response) -> str:
    try:
        chunks = []
        for cand in response.candidates or []:
            for part in cand.content.parts or []:
                if getattr(part, "text", None):
                    chunks.append(part.text)
        return "\n".join(chunks).strip()
    except Exception:
        return ""


# ---------------- LM Studio ----------------
def _lmstudio():
    global _lmstudio_cache
    if _lmstudio_cache is None:
        from openai import OpenAI

        _lmstudio_cache = OpenAI(base_url=settings.lmstudio_base_url, api_key=settings.lmstudio_api_key or "lm-studio",
                                 timeout=90.0, max_retries=0)
    return _lmstudio_cache


def lmstudio_model() -> str:
    global _lmstudio_model_cache
    if settings.lmstudio_model:
        return settings.lmstudio_model
    if _lmstudio_model_cache:
        return _lmstudio_model_cache
    ids = [m.id for m in _lmstudio().models.list().data if "embed" not in m.id.lower()]
    if not ids:
        raise RuntimeError("No chat model loaded in LM Studio")
    _lmstudio_model_cache = ids[0]
    return _lmstudio_model_cache


def _lmstudio_chat(messages: list[dict[str, str]], max_tokens: int = 400) -> tuple[str, str]:
    model = lmstudio_model()
    r = _lmstudio().chat.completions.create(model=model, messages=messages, temperature=0.2, max_tokens=max_tokens)
    return (r.choices[0].message.content or "").strip(), model


def _text_llm(messages: list[dict[str, str]], provider: str, max_tokens: int = 400) -> tuple[str, str, str]:
    """Text-only generation with automatic fallback between Gemini and local Llama."""
    order = ["lmstudio", "gemini"] if provider == "lmstudio" else ["gemini", "lmstudio"]
    last: Exception | None = None
    for p in order:
        try:
            if p == "lmstudio":
                text, model = _lmstudio_chat(messages, max_tokens)
            else:
                system = next((m["content"] for m in messages if m["role"] == "system"), None)
                user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
                response, model = _generate(user, _config(system_instruction=system, temperature=0.2))
                text = _text_of(response)
            return text, model, p
        except Exception as exc:
            last = exc
            print(f"Text LLM via {p} failed: {str(exc)[:160]}")
    raise HTTPException(status_code=503, detail=f"No language model available: {str(last)[:160]}")


# ---------------- Voice router (audio -> tool choice) ----------------
def _gemini_tools():
    from google.genai import types

    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t["function"]["name"], description=t["function"]["description"],
                                  parameters=t["function"]["parameters"]) for t in TOOL_DEFS])]


def _extract_function_call(response) -> tuple[str, dict] | None:
    try:
        for cand in response.candidates or []:
            for part in cand.content.parts or []:
                fc = getattr(part, "function_call", None)
                if fc:
                    return fc.name, (dict(fc.args) if fc.args else {})
    except Exception:
        return None
    return None


def voice_first_turn(audio_bytes: bytes, mime_type: str, language: str = "hi") -> dict[str, Any]:
    from google.genai import types

    contents = [types.Content(role="user", parts=[
        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type or "audio/wav"),
        types.Part.from_text(text=f"Audio from a verified bank caller, expected language: {LANG_NAME.get(language, 'Hindi')}. Follow your instructions."),
    ])]
    config = _config(
        system_instruction=SYSTEM_VOICE_ROUTER,
        tools=_gemini_tools(),
        temperature=0.2,
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
    )
    t0 = time.perf_counter()
    response, model = _generate(contents, config)
    dt = time.perf_counter() - t0
    call = _extract_function_call(response)
    name, args = call if call else (None, {})
    return {"model": model, "latency_s": dt, "function_name": name, "function_args": args,
            "transcript": args.get("customer_utterance") or "", "text": _text_of(response)}


# ---------------- Controlled reply step (Galileo checks input + output) ----------------
_REPLY_LOCK = threading.Lock()
_REPLY: dict[str, Any] = {}


@gc.controlled_llm("customer_reply")
def customer_reply(customer_utterance: str, language: str) -> str:
    tool_result = _REPLY.get("tool_result") or {}
    messages = [
        {"role": "system", "content": SYSTEM_REPLY.format(language=LANG_NAME.get(language, "Hindi"))},
        {"role": "user", "content": f"CALLER SAID: {customer_utterance}\n\nTOOL_RESULT: {json.dumps(tool_result, ensure_ascii=False)}"},
    ]
    t0 = time.perf_counter()
    text, model, provider = _text_llm(messages, reply_provider(), max_tokens=200)
    _REPLY["meta"] = {"model": model, "provider": provider, "messages": messages, "latency_s": time.perf_counter() - t0}
    return text


def generate_reply(utterance: str, language: str, tool_result: dict[str, Any]) -> dict[str, Any]:
    with _REPLY_LOCK:
        _REPLY.clear()
        _REPLY["tool_result"] = tool_result
        t0 = time.perf_counter()
        out = gc.run_step(customer_reply, utterance, language)
        meta = dict(_REPLY.get("meta") or {})
    meta.setdefault("latency_s", time.perf_counter() - t0)
    return {**out, **meta}


# ---------------- Controlled KB answer step ----------------
_KB_LOCK = threading.Lock()
_KB: dict[str, Any] = {}


def _rag_messages(question: str, hits: list[dict[str, Any]]) -> list[dict[str, str]]:
    articles = "\n\n".join(f"[{h['id']}] {h['title']}\n{h['snippet']}" for h in hits) or "(no articles retrieved)"
    user_prompt = (f"Customer question:\n{question}\n\nRetrieved specimen articles:\n{articles}\n\n"
                   "Answer using only these articles. Cite [KB-xx]. "
                   "If they are insufficient, say you do not have confirmed information.")
    return [{"role": "system", "content": SYSTEM_RAG}, {"role": "user", "content": user_prompt}]


@gc.controlled_llm("kb_answer")
def kb_answer(question: str) -> str:
    messages = _rag_messages(question, _KB.get("hits") or [])
    t0 = time.perf_counter()
    text, model, provider = _text_llm(messages, rag_provider(), max_tokens=400)
    _KB["meta"] = {"model": model, "provider": provider, "messages": messages, "latency_s": time.perf_counter() - t0}
    return text


def rag_answer(question: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Returns Galileo's decision plus answer metadata: decision, result, control, message, model, provider, messages, latency_s."""
    with _KB_LOCK:
        _KB.clear()
        _KB["hits"] = hits
        t0 = time.perf_counter()
        out = gc.run_step(kb_answer, question)
        meta = dict(_KB.get("meta") or {})
    meta.setdefault("messages", _rag_messages(question, hits))
    meta.setdefault("latency_s", time.perf_counter() - t0)
    return {**out, **meta}


# ---------------- V-CIP (unchanged signatures) ----------------
def extract_card_fields(image_bytes: bytes, mime_type: str) -> tuple[dict[str, Any], float, str, str]:
    from google.genai import types

    t0 = time.perf_counter()
    response, _ = _generate([
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type or "image/jpeg"),
        types.Part.from_text(text=CARD_PROMPT),
    ], _config(temperature=0.1))
    dt = time.perf_counter() - t0
    raw = _text_of(response)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    try:
        data = json.loads(cleaned)
    except Exception:
        data = {"id_type": None, "name": None, "dob": None, "id_number": None, "parse_error": True}
    return data, dt, raw, CARD_PROMPT


def vcip_summary(payload: dict[str, Any]) -> tuple[str, float, str]:
    prompt = SUMMARY_PROMPT + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    t0 = time.perf_counter()
    response, _ = _generate(prompt, _config(temperature=0.2))
    return _text_of(response), time.perf_counter() - t0, prompt