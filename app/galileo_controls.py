from __future__ import annotations

import asyncio
import functools
import inspect
import os
import re
import traceback
from typing import Any, Callable

from app.config import settings  # loads .env before agent_control is imported anywhere

_state: dict[str, Any] = {"ready": False, "error": None, "target_id": None, "bridge": False, "server_url": None}

SAFETY_BLOCK_MARKERS = ("execution blocked for safety", "control check failed")
_CLONE_RE = re.compile(r"-clone-[0-9a-f]+$", re.I)


class ControlsUnavailable(RuntimeError):
    pass


def enabled() -> bool:
    return os.getenv("AGENT_CONTROL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def status() -> dict[str, Any]:
    return {"enabled": enabled(), **_state}


def base_name(name: Any) -> str | None:
    if not name:
        return None
    return _CLONE_RE.sub("", str(name))


def clean_message(msg: Any) -> str:
    s = str(msg or "").strip()
    if s.lower().startswith("steering message:"):
        s = s.split(":", 1)[1].strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1]
    return s


def _log_stream_id(galileo_logger) -> str | None:
    for attr in ("log_stream_id", "agent_stream_id"):
        val = getattr(galileo_logger, attr, None)
        if val:
            return str(val)
    return os.getenv("GALILEO_LOG_STREAM_ID") or None


def init(galileo_logger) -> dict[str, Any]:
    """Connect Agent Control to the Galileo log stream. Call once at startup."""
    if _state["ready"] or not enabled():
        return status()
    if galileo_logger is None:
        _state["error"] = "Galileo logger is not initialised"
        return status()
    server_url = os.getenv("AGENT_CONTROL_URL", "").strip()
    if not server_url:
        _state["error"] = "AGENT_CONTROL_URL is missing in .env"
        print(f"Agent Control init failed: {_state['error']}")
        return status()
    try:
        os.environ.setdefault("AGENT_CONTROL_API_KEY", settings.galileo_api_key)
        import agent_control
        from galileo.agent_control import get_agent_control_target

        target = get_agent_control_target(
            target_type=os.getenv("AGENT_CONTROL_TARGET_TYPE", "log_stream"),
            log_stream_id=_log_stream_id(galileo_logger),
        )
        kwargs: dict[str, Any] = {
            "agent_name": os.getenv("AGENT_CONTROL_AGENT_NAME", "sahayak-agent"),
            "agent_description": "Sahayak Bank voice, V-CIP and KB agent",
            "server_url": server_url,
            "api_key": settings.galileo_api_key,
            "api_key_header": os.getenv("AGENT_CONTROL_API_KEY_HEADER", "Galileo-API-Key"),
            "observability_enabled": True,
            "observability_sink_name": "registered",
            "target_type": target.target_type,
            "target_id": target.target_id,
        }
        header = os.getenv("AGENT_CONTROL_RUNTIME_TOKEN_HEADER")
        if header:
            kwargs["runtime_token_header"] = header
        try:
            agent_control.init(**kwargs)
        except TypeError as exc:
            print(f"agent_control.init rejected an option ({exc}); retrying with core options")
            for k in ("runtime_token_header", "observability_sink_name"):
                kwargs.pop(k, None)
            agent_control.init(**kwargs)
        _state.update(ready=True, error=None, target_id=target.target_id, server_url=server_url)
        print(f"Agent Control ready: {server_url} -> target {target.target_type}/{target.target_id}")
        try:
            from galileo.handlers.agent_control.bridge import setup_agent_control_bridge

            setup_agent_control_bridge(galileo_logger)
            _state["bridge"] = True
            print("Agent Control bridge registered: control spans will appear in Galileo traces")
        except Exception as exc:
            print(f"Agent Control bridge not registered: {exc}")
    except Exception as exc:
        _state["error"] = f"{type(exc).__name__}: {exc}"
        print(f"Agent Control init failed: {_state['error']}")
        traceback.print_exc()
    return status()


def _wrap(fn: Callable, make_controlled: Callable[[], Callable]) -> Callable:
    cache: dict[str, Callable] = {}

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not enabled():
            return fn(*args, **kwargs)
        if not _state["ready"]:
            raise ControlsUnavailable(_state["error"] or "Agent Control is not initialised")
        if "c" not in cache:
            cache["c"] = make_controlled()
        return cache["c"](*args, **kwargs)

    return wrapper


def controlled_tool(tool_name: str):
    """Register a function as a Galileo-controlled tool step."""

    def deco(fn: Callable) -> Callable:
        fn.name = tool_name
        fn.tool_name = tool_name

        def make():
            from agent_control import control

            return control()(fn)

        wrapped = _wrap(fn, make)
        wrapped.name = tool_name
        wrapped.tool_name = tool_name
        return wrapped

    return deco


def controlled_llm(step_name: str):
    """Register a function as a Galileo-controlled LLM step."""

    def deco(fn: Callable) -> Callable:
        def make():
            from agent_control import control

            return control(step_name=step_name)(fn)

        return _wrap(fn, make)

    return deco


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    for method in ("model_dump", "dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return {"message": str(value)}


def _unavailable(exc: Exception) -> dict[str, Any]:
    return {
        "decision": "unavailable",
        "control": None,
        "control_full": None,
        "message": "Safety controls are unavailable right now, so this action was not performed.",
        "error": str(exc),
        "exception_type": type(exc).__name__,
    }


def _decision_from(exc: Exception) -> dict[str, Any] | None:
    name = type(exc).__name__
    if isinstance(exc, ControlsUnavailable):
        return _unavailable(exc)
    try:
        from agent_control import ControlSteerError, ControlViolationError
    except Exception:
        ControlSteerError = ControlViolationError = None  # type: ignore

    full = getattr(exc, "control_name", None)
    if ControlSteerError is not None and isinstance(exc, ControlSteerError):
        steering = _as_dict(getattr(exc, "steering_context", None))
        return {
            "decision": "steer",
            "control": base_name(full),
            "control_full": full,
            "message": clean_message(steering.get("message") or getattr(exc, "message", None) or str(exc)),
            "steering_context": steering or None,
            "exception_type": name,
        }
    if ControlViolationError is not None and isinstance(exc, ControlViolationError):
        return {
            "decision": "deny",
            "control": base_name(full),
            "control_full": full,
            "message": clean_message(getattr(exc, "message", None) or str(exc)),
            "exception_type": name,
        }
    if any(m in str(exc).lower() for m in SAFETY_BLOCK_MARKERS):
        return _unavailable(exc)
    return None


def run_step(fn: Callable, *args, **kwargs) -> dict[str, Any]:
    """Run a controlled step and return Galileo's decision: allow, steer, deny or unavailable.

    Must be called from a thread without a running event loop (the SDK uses asyncio.run).
    """
    try:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        return {"decision": "allow", "result": result, "control": None}
    except Exception as exc:
        decision = _decision_from(exc)
        if decision is not None:
            return decision
        raise