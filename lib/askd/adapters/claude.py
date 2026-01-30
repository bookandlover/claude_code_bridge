"""
Claude provider adapter for the unified ask daemon.

Wraps existing laskd_* modules to provide a consistent interface.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from askd.adapters.base import BaseProviderAdapter, ProviderRequest, ProviderResult, QueuedTask
from askd_runtime import log_path, write_log
from ccb_protocol import REQ_ID_PREFIX
from claude_comm import ClaudeLogReader
from completion_hook import notify_completion
from laskd_protocol import extract_reply_for_req, is_done_text, wrap_claude_prompt
from laskd_session import compute_session_key, load_project_session
from providers import LASKD_SPEC
from terminal import get_backend_for_session


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_log(line: str) -> None:
    write_log(log_path(LASKD_SPEC.log_file_name), line)


def _tail_state_for_log(log_path_val: Optional[Path], *, tail_bytes: int) -> dict:
    if not log_path_val or not log_path_val.exists():
        return {"session_path": log_path_val, "offset": 0, "carry": b""}
    try:
        size = log_path_val.stat().st_size
    except OSError:
        size = 0
    offset = max(0, size - max(0, int(tail_bytes)))
    return {"session_path": log_path_val, "offset": offset, "carry": b""}


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DONE_RE_CACHE: dict[str, re.Pattern[str]] = {}
_SPINNER_CHARS = {"·", ".", "*", "✶", "✻", "✽", "✢", "✣", "✤", "✥", "✦", "✧", "✩", "✪", "✫", "✬", "✭", "✮", "✯"}
_NOISE_PREFIXES = ("❯", "🤖")
_NOISE_CONTAINS = (
    "Bootstrapping",
    "thinking",
    "Frolicking",
    "Claude Code",
    "tokens",
    "bypass permissions",
    "CCB_REQ_ID:",
    "IMPORTANT:",
    "End your reply",
)


def _done_line_re(req_id: str) -> re.Pattern[str]:
    cached = _DONE_RE_CACHE.get(req_id)
    if cached:
        return cached
    pat = re.compile(rf"^\s*CCB_DONE:\s*{re.escape(req_id)}\s*$")
    _DONE_RE_CACHE[req_id] = pat
    return pat


def _clean_line(line: str) -> str:
    return _ANSI_RE.sub("", line.replace("\r", "")).rstrip("\n")


def _pane_log_state(log_path_val: Optional[Path]) -> dict:
    if not log_path_val:
        return {"log_path": None, "offset": 0, "carry": b""}
    try:
        size = log_path_val.stat().st_size
    except OSError:
        size = 0
    return {"log_path": log_path_val, "offset": size, "carry": b""}


def _read_pane_log(state: dict) -> tuple[list[str], dict]:
    log_path_val = state.get("log_path")
    if not log_path_val:
        return [], state
    try:
        size = log_path_val.stat().st_size
    except OSError:
        return [], state

    offset = int(state.get("offset") or 0)
    carry = state.get("carry") or b""
    if size < offset:
        offset = 0
        carry = b""

    try:
        with log_path_val.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], state

    new_offset = offset + len(data)
    buf = (carry + data).replace(b"\r", b"\n")
    lines = buf.split(b"\n")
    if buf and not buf.endswith(b"\n"):
        carry = lines.pop()
    else:
        carry = b""

    out: list[str] = []
    for raw in lines:
        if not raw:
            out.append("")
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("utf-8", errors="replace")
        out.append(_clean_line(text))

    new_state = {"log_path": log_path_val, "offset": new_offset, "carry": carry}
    return out, new_state


def _looks_like_prompt_context(lines: list[str]) -> bool:
    if not lines:
        return False
    if any(ln.strip().startswith("IMPORTANT:") for ln in lines):
        return True
    if any("End your reply with this exact final line" in ln for ln in lines):
        return True
    return False


def _strip_leading_marker(line: str) -> str:
    text = line.lstrip()
    if text.startswith("●") or text.startswith("•"):
        return text[1:].lstrip()
    return text


def _has_alnum(line: str) -> bool:
    return any(ch.isalnum() for ch in line)


def _is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith(_NOISE_PREFIXES):
        return True
    for needle in _NOISE_CONTAINS:
        if needle in stripped:
            return True
    if len(stripped) <= 2 and all(ch in _SPINNER_CHARS for ch in stripped):
        return True
    if not _has_alnum(stripped) and all(ch in "─-_=·*·•" or ch.isspace() for ch in stripped):
        return True
    return False


def _has_response_context(window: list[str]) -> bool:
    for line in window:
        if "CCB_DONE:" in line or "CCB_REQ_ID:" in line:
            continue
        if _is_noise_line(line):
            continue
        if "●" in line:
            return True
    return False


def _extract_reply_from_pane(lines: list[str], req_id: str) -> str:
    done_i: Optional[int] = None
    needle = f"CCB_DONE: {req_id}"
    for i in range(len(lines) - 1, -1, -1):
        if needle in lines[i]:
            done_i = i
            break
    if done_i is None:
        return ""

    out_rev: list[str] = []
    gaps = 0
    max_gap = 2
    max_lines = 200
    for i in range(done_i - 1, -1, -1):
        raw = lines[i]
        if _is_noise_line(raw):
            if out_rev:
                gaps += 1
                if gaps >= max_gap:
                    break
            continue
        gaps = 0
        cleaned = _strip_leading_marker(raw)
        if cleaned.strip() == "":
            continue
        out_rev.append(cleaned.rstrip())
        if len(out_rev) >= max_lines:
            break

    out = list(reversed(out_rev))
    while out and out[0].strip() == "":
        out = out[1:]
    while out and out[-1].strip() == "":
        out = out[:-1]
    return "\n".join(out).rstrip()


class ClaudeAdapter(BaseProviderAdapter):
    """Adapter for Claude provider."""

    @property
    def key(self) -> str:
        return "claude"

    @property
    def spec(self):
        return LASKD_SPEC

    @property
    def session_filename(self) -> str:
        return ".claude-session"

    def load_session(self, work_dir: Path) -> Optional[Any]:
        return load_project_session(work_dir)

    def compute_session_key(self, session: Any) -> str:
        return compute_session_key(session) if session else "claude:unknown"

    def handle_task(self, task: QueuedTask) -> ProviderResult:
        started_ms = _now_ms()
        req = task.request
        work_dir = Path(req.work_dir)
        _write_log(f"[INFO] start provider=claude req_id={task.req_id} work_dir={req.work_dir}")

        session = load_project_session(work_dir)
        session_key = self.compute_session_key(session)

        if not session:
            return ProviderResult(
                exit_code=1,
                reply="No active Claude session found for work_dir.",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
            )

        ok, pane_or_err = session.ensure_pane()
        if not ok:
            return ProviderResult(
                exit_code=1,
                reply=f"Session pane not available: {pane_or_err}",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
            )
        pane_id = pane_or_err

        backend = get_backend_for_session(session.data)
        if not backend:
            return ProviderResult(
                exit_code=1,
                reply="Terminal backend not available",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
            )

        deadline = None if float(req.timeout_s) < 0.0 else (time.time() + float(req.timeout_s))

        log_reader = ClaudeLogReader(work_dir=Path(session.work_dir))
        if session.claude_session_path:
            try:
                log_reader.set_preferred_session(Path(session.claude_session_path))
            except Exception:
                pass
        state = log_reader.capture_state()

        refresh = getattr(backend, "refresh_pane_logs", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass

        pane_log_path: Optional[Path] = None
        ensure_log = getattr(backend, "ensure_pane_log", None)
        if callable(ensure_log):
            try:
                pane_log_path = ensure_log(pane_id)
            except Exception:
                pane_log_path = None
        if pane_log_path is None:
            getter = getattr(backend, "pane_log_path", None)
            if callable(getter):
                try:
                    pane_log_path = getter(pane_id)
                except Exception:
                    pane_log_path = None
        pane_state = _pane_log_state(pane_log_path) if pane_log_path else None

        if req.no_wrap:
            prompt = req.message
        else:
            prompt = wrap_claude_prompt(req.message, task.req_id)
        backend.send_text(pane_id, prompt)

        if pane_log_path and pane_state:
            result = self._wait_for_response_pane(
                task, session, session_key, started_ms, backend, pane_id, pane_state, deadline
            )
            if result.exit_code == 1 or result.done_seen or deadline is None or time.time() >= deadline:
                self._finalize_result(result, req)
                return result
        return self._wait_for_response(task, session, session_key, started_ms, log_reader, state, backend, pane_id, deadline)

    def _wait_for_response_pane(
        self, task: QueuedTask, session: Any, session_key: str,
        started_ms: int, backend: Any, pane_id: str, pane_state: dict,
        deadline: Optional[float]
    ) -> ProviderResult:
        req = task.request
        lines: list[str] = []
        anchor_seen = False
        anchor_ms: Optional[int] = None
        done_seen = False
        done_ms: Optional[int] = None
        done_re = _done_line_re(task.req_id)
        pane_check_interval = float(os.environ.get("CCB_LASKD_PANE_CHECK_INTERVAL", "2.0"))
        last_pane_check = time.time()

        while True:
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                wait_step = min(remaining, 0.5)
            else:
                wait_step = 0.5

            if time.time() - last_pane_check >= pane_check_interval:
                try:
                    alive = bool(backend.is_alive(pane_id))
                except Exception:
                    alive = False
                if not alive:
                    _write_log(f"[ERROR] Pane {pane_id} died req_id={task.req_id}")
                    return ProviderResult(
                        exit_code=1,
                        reply="Claude pane died during request",
                        req_id=task.req_id,
                        session_key=session_key,
                        done_seen=False,
                        anchor_seen=anchor_seen,
                        anchor_ms=anchor_ms,
                    )
                last_pane_check = time.time()

            new_lines, pane_state = _read_pane_log(pane_state)
            if not new_lines:
                time.sleep(wait_step)
                continue

            start_idx = len(lines)
            for line in new_lines:
                lines.append(line)

            for i in range(start_idx, len(lines)):
                line = lines[i]
                if (not anchor_seen) and (REQ_ID_PREFIX in line) and (task.req_id in line):
                    anchor_seen = True
                    anchor_ms = _now_ms() - started_ms
                if done_re.match(line.strip() or ""):
                    window = lines[max(0, i - 12) : i]
                    if not _has_response_context(window):
                        continue
                    done_seen = True
                    done_ms = _now_ms() - started_ms
                    break
            if done_seen:
                break

        final_reply = _extract_reply_from_pane(lines, task.req_id)

        return ProviderResult(
            exit_code=0 if done_seen else 2,
            reply=final_reply,
            req_id=task.req_id,
            session_key=session_key,
            done_seen=done_seen,
            done_ms=done_ms,
            anchor_seen=anchor_seen,
            anchor_ms=anchor_ms,
            fallback_scan=False,
        )

    def _finalize_result(self, result: ProviderResult, req: ProviderRequest) -> None:
        _write_log(f"[INFO] done provider=claude req_id={result.req_id} exit={result.exit_code}")
        notify_completion(
            provider="claude",
            output_file=req.output_path,
            reply=result.reply,
            req_id=result.req_id,
            done_seen=result.done_seen,
            caller=req.caller,
        )

    def _wait_for_response(
        self, task: QueuedTask, session: Any, session_key: str,
        started_ms: int, log_reader: ClaudeLogReader, state: dict,
        backend: Any, pane_id: str, deadline: Optional[float] = None
    ) -> ProviderResult:
        req = task.request
        if deadline is None:
            deadline = None if float(req.timeout_s) < 0.0 else (time.time() + float(req.timeout_s))
        chunks: list[str] = []
        anchor_seen = False
        fallback_scan = False
        anchor_ms: Optional[int] = None
        done_seen = False
        done_ms: Optional[int] = None

        anchor_grace_deadline = min(deadline, time.time() + 1.5) if deadline else (time.time() + 1.5)
        anchor_collect_grace = min(deadline, time.time() + 2.0) if deadline else (time.time() + 2.0)
        rebounded = False
        tail_bytes = int(os.environ.get("CCB_LASKD_REBIND_TAIL_BYTES", str(2 * 1024 * 1024)))
        pane_check_interval = float(os.environ.get("CCB_LASKD_PANE_CHECK_INTERVAL", "2.0"))
        last_pane_check = time.time()

        while True:
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                wait_step = min(remaining, 0.5)
            else:
                wait_step = 0.5

            if time.time() - last_pane_check >= pane_check_interval:
                try:
                    alive = bool(backend.is_alive(pane_id))
                except Exception:
                    alive = False
                if not alive:
                    _write_log(f"[ERROR] Pane {pane_id} died req_id={task.req_id}")
                    return ProviderResult(
                        exit_code=1,
                        reply="Claude pane died during request",
                        req_id=task.req_id,
                        session_key=session_key,
                        done_seen=False,
                        anchor_seen=anchor_seen,
                        fallback_scan=fallback_scan,
                        anchor_ms=anchor_ms,
                    )
                last_pane_check = time.time()

            events, state = log_reader.wait_for_events(state, wait_step)
            if not events:
                if (not rebounded) and (not anchor_seen) and time.time() >= anchor_grace_deadline:
                    log_reader = ClaudeLogReader(work_dir=Path(session.work_dir), use_sessions_index=False)
                    log_hint = log_reader.current_session_path()
                    state = _tail_state_for_log(log_hint, tail_bytes=tail_bytes)
                    fallback_scan = True
                    rebounded = True
                continue

            for role, text in events:
                if role == "user":
                    if f"{REQ_ID_PREFIX} {task.req_id}" in text:
                        anchor_seen = True
                        if anchor_ms is None:
                            anchor_ms = _now_ms() - started_ms
                    continue
                if role != "assistant":
                    continue
                if (not anchor_seen) and time.time() < anchor_collect_grace:
                    continue
                chunks.append(text)
                combined = "\n".join(chunks)
                if is_done_text(combined, task.req_id):
                    done_seen = True
                    done_ms = _now_ms() - started_ms
                    break

            if done_seen:
                break

        combined = "\n".join(chunks)
        final_reply = extract_reply_for_req(combined, task.req_id)

        result = ProviderResult(
            exit_code=0 if done_seen else 2,
            reply=final_reply,
            req_id=task.req_id,
            session_key=session_key,
            done_seen=done_seen,
            done_ms=done_ms,
            anchor_seen=anchor_seen,
            anchor_ms=anchor_ms,
            fallback_scan=fallback_scan,
        )
        self._finalize_result(result, req)
        return result
