"""Supabase-backed session storage for the Telegram bot.

RA-924 — Replaces the ephemeral SQLite store with durable Supabase persistence,
fixing cold-start context loss when the Railway container redeploys.

Uses the Supabase REST API (PostgREST) over urllib — no extra dependencies
needed beyond the Python standard library.

Table: telegram_sessions  (see supabase/migration.sql for DDL)

Activation: set SUPABASE_URL + SUPABASE_SERVICE_KEY env vars.
Fallback:   if either var is absent, SQLiteSessionStorage is used instead.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

from ..claude.session import ClaudeSession, SessionStorage

log = logging.getLogger("telegram-bot.supabase_sessions")

_TABLE = "telegram_sessions"


class SupabaseSessionStorage(SessionStorage):
    """SessionStorage backed by a Supabase PostgreSQL table.

    Every public method is fire-and-forget on error — a Supabase outage
    must never crash the bot or lose an ongoing conversation.
    """

    def __init__(self, supabase_url: str, service_key: str) -> None:
        """Initialise with Supabase project URL and service-role key.

        Args:
            supabase_url:  Your project URL, e.g. ``https://xyz.supabase.co``
            service_key:   Service-role secret (bypasses RLS) — never expose
                           this in client-side code.
        """
        self._base = supabase_url.rstrip("/") + "/rest/v1"
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── internal helpers ───────────────────────────────────────────────────────

    def _get(self, params: dict) -> list:
        """GET /rest/v1/telegram_sessions with query params. Returns list of rows."""
        qs = urllib.parse.urlencode(params)
        url = f"{self._base}/{_TABLE}?{qs}"
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:
            log.warning("Supabase GET failed: %s", exc)
            return []

    def _upsert(self, data: dict) -> bool:
        """POST with Prefer: resolution=merge-duplicates for upsert semantics."""
        url = f"{self._base}/{_TABLE}"
        headers = {**self._headers, "Prefer": "resolution=merge-duplicates"}
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()  # consume body
                return True
        except Exception as exc:
            log.warning("Supabase UPSERT failed: %s", exc)
            return False

    def _patch(self, filter_params: dict, data: dict) -> bool:
        """PATCH rows matching filter_params."""
        qs = urllib.parse.urlencode(filter_params)
        url = f"{self._base}/{_TABLE}?{qs}"
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=payload, headers=self._headers, method="PATCH"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                return True
        except Exception as exc:
            log.warning("Supabase PATCH failed: %s", exc)
            return False

    @staticmethod
    def _row_to_session(row: dict) -> ClaudeSession:
        """Convert a PostgREST row dict to a ClaudeSession."""
        def _parse_dt(val: str | None) -> datetime:
            if not val:
                return datetime.now(UTC)
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)

        return ClaudeSession(
            session_id=row["session_id"],
            user_id=int(row["user_id"]),
            project_path=Path(row["project_path"]),
            created_at=_parse_dt(row.get("created_at")),
            last_used=_parse_dt(row.get("last_used")),
            total_cost=float(row.get("total_cost") or 0.0),
            total_turns=int(row.get("total_turns") or 0),
            message_count=int(row.get("message_count") or 0),
            tools_used=[],  # stored separately in SQLite tool_usage table
        )

    # ── SessionStorage interface ───────────────────────────────────────────────

    async def save_session(self, session: ClaudeSession) -> None:
        """Upsert session to Supabase. Never raises."""
        row = {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "project_path": str(session.project_path),
            "created_at": session.created_at.isoformat(),
            "last_used": session.last_used.isoformat(),
            "total_cost": session.total_cost,
            "total_turns": session.total_turns,
            "message_count": session.message_count,
            "is_active": True,
        }
        ok = self._upsert(row)
        if ok:
            log.debug("Session saved to Supabase", extra={"session_id": session.session_id})

    async def load_session(
        self, session_id: str, user_id: int
    ) -> Optional[ClaudeSession]:
        """Load session owned by user_id from Supabase."""
        rows = self._get({
            "session_id": f"eq.{session_id}",
            "user_id": f"eq.{user_id}",
            "is_active": "eq.true",
            "select": "*",
        })
        if not rows:
            return None
        try:
            return self._row_to_session(rows[0])
        except Exception as exc:
            log.warning("Failed to parse Supabase session row: %s", exc)
            return None

    async def delete_session(self, session_id: str) -> None:
        """Mark session as inactive (soft delete)."""
        self._patch(
            {"session_id": f"eq.{session_id}"},
            {"is_active": False},
        )

    async def get_user_sessions(self, user_id: int) -> List[ClaudeSession]:
        """Return all active sessions for a user."""
        rows = self._get({
            "user_id": f"eq.{user_id}",
            "is_active": "eq.true",
            "select": "*",
            "order": "last_used.desc",
        })
        sessions = []
        for row in rows:
            try:
                sessions.append(self._row_to_session(row))
            except Exception as exc:
                log.warning("Skipping malformed Supabase row: %s", exc)
        return sessions

    async def get_all_sessions(self) -> List[ClaudeSession]:
        """Return all active sessions (used by session cleanup cron)."""
        rows = self._get({
            "is_active": "eq.true",
            "select": "*",
            "order": "last_used.desc",
        })
        sessions = []
        for row in rows:
            try:
                sessions.append(self._row_to_session(row))
            except Exception as exc:
                log.warning("Skipping malformed Supabase row: %s", exc)
        return sessions
