"""Product hook (Sprint 6.6): tools to read/write data_to_inside FastAPI.

Brain talks HTTP to the product surface — no shared SQLite. Auth via a
one-time login that returns a JWT cookie kept in a module-level Session.
Login is lazy: first call logs in; subsequent calls reuse the cookie until
the process restarts.

Endpoints touched:
    POST /auth/login                          (initial login)
    POST /brain/observation                   (write own thought)
    GET  /brain/state                         (read own self-summary)
    GET  /brain/journal?kind=...&limit=...    (read recent journal)
    GET  /bugs                                (read bug reports)

Required env vars (see .env.example):
    OUROBOROS_PRODUCT_API       — default http://127.0.0.1:8765
    OUROBOROS_PRODUCT_USER      — login email (e.g. brain@local)
    OUROBOROS_PRODUCT_PASSWORD  — login password

If creds are missing, all four tools return a polite warning string —
they don't raise, so brain can still run without product (for example
when the product server is down or in dev mode).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_session: Optional[requests.Session] = None


def _api_base() -> str:
    return os.environ.get("OUROBOROS_PRODUCT_API", "http://127.0.0.1:8765").rstrip("/")


def _login_session() -> Optional[requests.Session]:
    """Lazy login. Returns Session with JWT cookie or None if creds missing."""
    global _session
    if _session is not None:
        return _session

    user = os.environ.get("OUROBOROS_PRODUCT_USER", "").strip()
    pwd = os.environ.get("OUROBOROS_PRODUCT_PASSWORD", "").strip()
    if not user or not pwd:
        log.warning(
            "product hook: OUROBOROS_PRODUCT_USER/OUROBOROS_PRODUCT_PASSWORD not set"
        )
        return None

    s = requests.Session()
    try:
        r = s.post(
            f"{_api_base()}/auth/login",
            json={"email": user, "password": pwd},
            timeout=10,
        )
        if not r.ok:
            log.warning("product hook: login failed status=%s body=%s", r.status_code, r.text[:200])
            return None
    except requests.RequestException as exc:
        log.warning("product hook: login error %s", exc)
        return None

    _session = s
    log.info("product hook: logged in as %s", user)
    return _session


# --- Tool handlers ----------------------------------------------------------


def _read_product_journal(ctx: ToolContext, **args: Any) -> str:
    s = _login_session()
    if s is None:
        return "⚠️ product hook not configured (OUROBOROS_PRODUCT_USER/PASSWORD missing)"
    kind = args.get("kind")
    limit = int(args.get("limit", 20))
    params: Dict[str, str] = {"limit": str(limit)}
    if kind:
        params["kind"] = str(kind)
    try:
        r = s.get(f"{_api_base()}/brain/journal", params=params, timeout=10)
        if not r.ok:
            return f"⚠️ /brain/journal {r.status_code}: {r.text[:200]}"
        rows = r.json()
        if not rows:
            return f"(empty journal, kind={kind!r}, limit={limit})"
        out: List[str] = []
        for row in rows[:limit]:
            head = (row.get("headline") or "")[:120]
            ts = (row.get("created_at") or "")[:19]
            out.append(f"- [{row.get('kind')}] {head} (eqs={row.get('eqs')}, at={ts})")
        return "\n".join(out)
    except requests.RequestException as exc:
        return f"⚠️ /brain/journal failed: {exc}"


def _write_brain_observation(ctx: ToolContext, **args: Any) -> str:
    s = _login_session()
    if s is None:
        return "⚠️ product hook not configured"
    headline = str(args.get("headline", "")).strip()
    if not headline:
        return "⚠️ headline is required"

    body_raw = args.get("body")
    body: Dict[str, Any]
    if isinstance(body_raw, dict):
        body = body_raw
    elif isinstance(body_raw, str) and body_raw.strip():
        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            body = {"raw": body_raw}
    else:
        body = {}

    try:
        r = s.post(
            f"{_api_base()}/brain/observation",
            json={"headline": headline[:300], "body": body},
            timeout=10,
        )
        if not r.ok:
            return f"⚠️ /brain/observation {r.status_code}: {r.text[:200]}"
        out = r.json()
        oid = (out.get("id") or "")[:12]
        ts = (out.get("created_at") or "")[:19]
        return f"✓ observation {oid} written at {ts}"
    except requests.RequestException as exc:
        return f"⚠️ /brain/observation failed: {exc}"


def _read_product_state(ctx: ToolContext, **args: Any) -> str:
    s = _login_session()
    if s is None:
        return "⚠️ product hook not configured"
    try:
        r = s.get(f"{_api_base()}/brain/state", timeout=10)
        if not r.ok:
            return f"⚠️ /brain/state {r.status_code}: {r.text[:200]}"
        d = r.json()
        head = (d.get("latest_headline") or "(none)")[:120]
        return (
            f"observation_count_24h={d.get('observation_count_24h', 0)} | "
            f"last_observation_at={d.get('last_observation_at')} | "
            f"latest_headline={head}"
        )
    except requests.RequestException as exc:
        return f"⚠️ /brain/state failed: {exc}"


def _read_product_bugs(ctx: ToolContext, **args: Any) -> str:
    s = _login_session()
    if s is None:
        return "⚠️ product hook not configured"
    status_filter = args.get("status")
    try:
        r = s.get(f"{_api_base()}/bugs", timeout=10)
        if not r.ok:
            return f"⚠️ /bugs {r.status_code}: {r.text[:200]}"
        rows = r.json()
        if status_filter:
            rows = [b for b in rows if b.get("status") == status_filter]
        if not rows:
            return f"(no bugs, status={status_filter!r})"
        out: List[str] = []
        for b in rows[:20]:
            title = (b.get("title") or "")[:100]
            bid = (b.get("id") or "")[:8]
            out.append(
                f"- [{b.get('status')}] {b.get('severity')} {title} (id={bid})"
            )
        return "\n".join(out)
    except requests.RequestException as exc:
        return f"⚠️ /bugs failed: {exc}"


# --- Registry export --------------------------------------------------------


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="read_product_journal",
            schema={
                "name": "read_product_journal",
                "description": (
                    "Read the data_to_inside product journal — recent agent "
                    "decisions, brief verdicts, open questions, observations. "
                    "Use to see what the product agent has been doing before "
                    "deciding what to evolve."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "brief_verdict",
                                "open_question",
                                "lesson",
                                "observation",
                            ],
                            "description": "Optional filter by entry kind",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 20,
                            "minimum": 1,
                            "maximum": 200,
                        },
                    },
                    "required": [],
                },
            },
            handler=_read_product_journal,
        ),
        ToolEntry(
            name="write_brain_observation",
            schema={
                "name": "write_brain_observation",
                "description": (
                    "Write one brain observation back into the product journal. "
                    "Use this to surface what brain learned/noticed for users to "
                    "see in SPA Self/Architecture. Headline is short title (≤300 "
                    "chars); body is optional structured JSON dict."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "headline": {
                            "type": "string",
                            "description": "1-line summary, ≤300 chars",
                        },
                        "body": {
                            "type": "object",
                            "description": "Optional structured payload (any JSON dict)",
                        },
                    },
                    "required": ["headline"],
                },
            },
            handler=_write_brain_observation,
        ),
        ToolEntry(
            name="read_product_state",
            schema={
                "name": "read_product_state",
                "description": (
                    "Read brain's own self-summary in the product (count of "
                    "observations in the last 24h, last_observation_at, latest "
                    "headline). Use to know what you already said today before "
                    "saying it again."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_read_product_state,
        ),
        ToolEntry(
            name="read_product_bugs",
            schema={
                "name": "read_product_bugs",
                "description": (
                    "Read product bug reports. Optional status filter "
                    "(new|open|fixed). Use to find concrete things to fix."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["new", "open", "fixed"],
                            "description": "Filter by bug status",
                        },
                    },
                    "required": [],
                },
            },
            handler=_read_product_bugs,
        ),
    ]
