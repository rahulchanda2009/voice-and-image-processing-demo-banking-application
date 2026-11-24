from __future__ import annotations

import json
import os
import time
import traceback
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import settings

_logger = None
_init_tried = False
_init_error: str | None = None


def _ns(seconds: float | None) -> int:
    return max(0, int(float(seconds or 0) * 1e9))


def _s(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _meta(meta: dict[str, Any] | None) -> dict[str, str]:
    return {str(k): _s(v) for k, v in (meta or {}).items() if v is not None}


def _apply_env() -> None:
    os.environ["GALILEO_API_KEY"] = settings.galileo_api_key
    os.environ["GALILEO_CONSOLE_URL"] = settings.galileo_console_url
    os.environ["GALILEO_PROJECT"] = settings.galileo_project
    os.environ["GALILEO_PROJECT_NAME"] = settings.galileo_project
    os.environ["GALILEO_LOG_STREAM"] = settings.galileo_log_stream
    if not os.getenv("GALILEO_API_URL"):
        console = (settings.galileo_console_url or "").rstrip("/")
        if "://console." in console:
            os.environ["GALILEO_API_URL"] = console.replace("://console.", "://api.", 1)
        elif console:
            os.environ["GALILEO_API_URL"] = console


def _get_logger():
    global _logger, _init_tried, _init_error
    if _init_tried:
        return _logger
    _init_tried = True
    if not settings.galileo_enabled:
        _init_error = "GALILEO_API_KEY missing"
        print("Galileo tracing disabled (no GALILEO_API_KEY).")
        return None
    _apply_env()
    try:
        from galileo import GalileoLogger

        _logger = GalileoLogger(project=settings.galileo_project, log_stream=settings.galileo_log_stream)
        print(f"Galileo tracing on: {settings.galileo_project}/{settings.galileo_log_stream}")
        print(f"Galileo API: {os.getenv('GALILEO_API_URL')}")
    except Exception as exc:
        _init_error = f"{type(exc).__name__}: {exc}"
        print(f"Galileo init failed, continuing without traces: {_init_error}")
        traceback.print_exc()
        _logger = None
    return _logger


def _try(fn, attempts: list[dict[str, Any]]) -> bool:
    last: Exception | None = None
    for kwargs in attempts:
        try:
            fn(**kwargs)
            return True
        except Exception as exc:
            last = exc
    print(f"Galileo {getattr(fn, '__name__', 'call')} failed: {last}")
    return False


class Trace:
    def __init__(self, name: str, input_data: Any, metadata: dict[str, Any] | None = None, tags: list[str] | None = None):
        self.name = name
        self.logger = _get_logger()
        self.metadata = _meta(metadata)
        self.tags = list(tags or [])
        self.output: Any = None
        self.status_code = 200
        self.started = False
        self.flushed = False
        self._t0 = time.perf_counter()
        if not self.logger:
            return
        text = _s(input_data)
        self.started = _try(
            self.logger.start_trace,
            [
                {"input": text, "name": name, "metadata": self.metadata, "tags": self.tags},
                {"input": text, "name": name, "tags": self.tags},
                {"input": text, "name": name},
            ],
        )
        if not self.started:
            self.logger = None

    def llm(
        self,
        *,
        name: str,
        model: str,
        messages: list[dict[str, Any]],
        output: Any,
        duration_s: float,
        tools: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        if not self.logger:
            return
        base = {
            "name": name,
            "model": model,
            "duration_ns": _ns(duration_s),
            "metadata": _meta(metadata),
            "tags": (tags or []) + self.tags,
        }
        rich = dict(base, input=messages, output=output)
        if tools:
            rich["tools"] = tools
        plain = dict(base, input=_s(messages), output=_s(output))
        _try(
            self.logger.add_llm_span,
            [rich, plain, {"input": _s(messages), "output": _s(output), "model": model, "name": name}],
        )

    def tool(
        self,
        *,
        name: str,
        input_data: Any,
        output: Any,
        duration_s: float,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        status_code: int | None = None,
    ) -> None:
        if not self.logger:
            return
        kwargs = {
            "input": _s(input_data),
            "output": _s(output),
            "name": name,
            "duration_ns": _ns(duration_s),
            "metadata": _meta(metadata),
            "tags": (tags or []) + self.tags,
        }
        attempts = []
        if status_code is not None:
            attempts.append(dict(kwargs, status_code=status_code))
        attempts += [kwargs, {"input": kwargs["input"], "output": kwargs["output"], "name": name}]
        _try(self.logger.add_tool_span, attempts)

    def retriever(
        self,
        *,
        name: str,
        query: str,
        documents: list[dict[str, Any]],
        duration_s: float,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        if not self.logger:
            return
        docs = [{"content": d["content"], "metadata": _meta(d.get("metadata"))} for d in documents]
        base = {
            "input": query,
            "name": name,
            "duration_ns": _ns(duration_s),
            "metadata": _meta(metadata),
            "tags": (tags or []) + self.tags,
        }
        ok = _try(
            self.logger.add_retriever_span,
            [dict(base, output=docs), dict(base, output=[d["content"] for d in docs])],
        )
        if not ok:
            self.tool(name=name, input_data=query, output=docs, duration_s=duration_s, metadata=metadata, tags=tags)

    def conclude(self, output: Any, status_code: int = 200) -> None:
        if not self.logger:
            return
        dur = _ns(time.perf_counter() - self._t0)
        text = _s(output)
        _try(
            self.logger.conclude,
            [
                {"output": text, "duration_ns": dur, "status_code": status_code},
                {"output": text, "duration_ns": dur},
                {"output": text},
            ],
        )
        try:
            self.logger.flush()
            self.flushed = True
        except Exception as exc:
            print(f"Galileo flush failed: {exc}")
            traceback.print_exc()


@contextmanager
def start_trace(
    name: str,
    input_data: Any,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Iterator[Trace]:
    trace = Trace(name, input_data, metadata=metadata, tags=tags)
    try:
        yield trace
    except Exception as exc:
        code = int(getattr(exc, "status_code", 500) or 500)
        detail = getattr(exc, "detail", None) or str(exc)
        trace.conclude({"error": detail}, status_code=code)
        raise
    else:
        trace.conclude(trace.output if trace.output is not None else {"status": "ok"}, status_code=trace.status_code)


def heartbeat() -> dict[str, Any]:
    logger = _get_logger()
    base = {
        "project": settings.galileo_project,
        "log_stream": settings.galileo_log_stream,
        "console": settings.galileo_console_url,
        "api_url": os.getenv("GALILEO_API_URL"),
        "trace_name": "Galileo Heartbeat — Sahayak POC",
    }
    if not logger:
        return {**base, "ok": False, "error": _init_error or "logger not initialized"}
    with start_trace(
        "Galileo Heartbeat — Sahayak POC",
        "connectivity check",
        metadata={"use_case": "heartbeat"},
        tags=["heartbeat"],
    ) as trace:
        trace.tool(name="heartbeat", input_data={"check": "connectivity"}, output={"status": "ok"}, duration_s=0.01)
        trace.output = {"status": "ok"}
    ok = bool(trace.started and trace.flushed)
    return {**base, "ok": ok, "trace_started": trace.started, "flushed": trace.flushed}