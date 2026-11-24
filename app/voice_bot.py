from __future__ import annotations

import json
import time
from typing import Any

from app import galileo_controls as gc
from app import llm_client, tools

TOOLS = {
    "get_my_balance": tools.get_my_balance,
    "get_my_credit_card_bill": tools.get_my_credit_card_bill,
    "waive_credit_card_bill": tools.waive_credit_card_bill,
    "lookup_customer_account": tools.lookup_customer_account,
    "report_unsupported_request": tools.report_unsupported_request,
}
SESSION_TOOLS = {"get_my_balance", "get_my_credit_card_bill", "waive_credit_card_bill"}

# Generic text for decisions that carry no customer-facing message. The rule itself lives in Galileo.
BLOCKED = {
    "hi": "यह अनुरोध बैंक के सुरक्षा नियंत्रण द्वारा रोक दिया गया है।",
    "en": "This request was blocked by the bank's safety controls.",
}
UNAVAILABLE = {
    "hi": "सुरक्षा नियंत्रण अभी उपलब्ध नहीं हैं, इसलिए यह कार्य नहीं किया गया। कृपया थोड़ी देर बाद प्रयास करें।",
    "en": "Safety controls are unavailable right now, so this action was not performed. Please try again shortly.",
}


def _fallback(decision: str, lang: str) -> str:
    table = UNAVAILABLE if decision == "unavailable" else BLOCKED
    return table.get(lang, table["en"])


def handle_voice_turn(customer_id: str, audio_bytes: bytes, mime_type: str, language: str, trace) -> dict[str, Any]:
    lang = language if language in ("hi", "en") else "hi"
    first = llm_client.voice_first_turn(audio_bytes, mime_type, lang)
    fn = first["function_name"] if first["function_name"] in TOOLS else "report_unsupported_request"
    args = first["function_args"] or {}
    transcript = first["transcript"]

    messages = [
        {"role": "system", "content": llm_client.SYSTEM_VOICE_ROUTER},
        {"role": "user", "content": f"[{llm_client.LANG_NAME[lang]} audio from verified caller] {transcript}".strip()},
    ]
    assistant = {"role": "assistant", "content": first["text"] or "", "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": fn, "arguments": json.dumps(args, ensure_ascii=False)}}]}
    trace.llm(name="gemini_intent_and_tool_choice", model=first["model"], messages=messages, output=assistant,
              tools=llm_client.TOOL_DEFS, duration_s=first["latency_s"],
              metadata={"step": "intent", "selected_tool": fn, "language": lang})

    # Tool step: Galileo Agent Control decides whether it runs.
    if fn in SESSION_TOOLS:
        call_args = {"customer_id": customer_id}
    elif fn == "lookup_customer_account":
        call_args = {"customer_name": str(args.get("customer_name") or "")}
    else:
        call_args = {"requested_topic": str(args.get("requested_topic") or "other")}

    t0 = time.perf_counter()
    tool_out = gc.run_step(TOOLS[fn], **call_args)
    tool_decision = tool_out["decision"]
    trace.tool(
        name=fn,
        input_data=call_args,
        output=tool_out.get("result") if tool_decision == "allow" else {
            "executed": False, "galileo_decision": tool_decision,
            "galileo_control": tool_out.get("control"), "galileo_message": tool_out.get("message")},
        duration_s=time.perf_counter() - t0,
        metadata={"galileo_decision": tool_decision, "galileo_control": tool_out.get("control")},
        status_code=200 if tool_decision == "allow" else 403,
    )

    result = {"transcript": transcript, "intent": fn, "tool": fn, "language": lang,
              "tool_decision": tool_decision, "tool_control": tool_out.get("control"),
              "reply_decision": None, "reply_control": None}

    if tool_decision == "allow":
        tool_result = tool_out["result"]
    elif tool_decision == "steer":
        tool_result = {"action_performed": False, "galileo_guidance": tool_out.get("message")}
    else:
        result["reply"] = _fallback(tool_decision, lang)
        return result

    # Reply step: Galileo checks the input (incl. transcript) before, and the reply text after.
    rep = llm_client.generate_reply(transcript, lang, tool_result)
    result["reply_decision"] = rep["decision"]
    result["reply_control"] = rep.get("control")
    reply = rep["result"] if rep["decision"] == "allow" else _fallback(rep["decision"], lang)
    if rep["decision"] == "allow" and not reply:
        reply = _fallback("deny", lang)
    trace.llm(
        name="customer_reply",
        model=rep.get("model") or "not-called",
        messages=rep.get("messages") or messages,
        output={"role": "assistant", "content": reply},
        duration_s=rep.get("latency_s") or 0,
        metadata={"step": "reply", "provider": rep.get("provider"), "language": lang,
                  "galileo_decision": rep["decision"], "galileo_control": rep.get("control")},
    )
    result["reply"] = reply
    return result