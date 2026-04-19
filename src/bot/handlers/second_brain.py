"""
second_brain.py — Mobile second-brain commands backed by Pi-CEO backend.

Adds six high-leverage phone commands so Telegram is a complete remote for the
portfolio:

  /linear   <title>          Create a Linear issue in the current project
  /issue    RA-xxxx          Fetch a Linear issue + latest comment
  /pipeline RA-xxxx          Show build-session phase progress
  /ship     RA-xxxx          Trigger ship_build MCP tool
  /plan     <brief>          Trigger plan_build MCP tool
  /digest                    On-demand portfolio snapshot

Every handler delegates to the Pi-CEO backend via the same auth cookie pattern
as /health in remote_control.py (RA-1101). Backend proxy endpoints live in
app/server/routes/telegram_proxy.py.

Hardened per RA-1109 surface-treatment rules:
  * every command replies with a terminal-state message (success or named error)
  * every async > 2 s emits a "working..." acknowledgement first
  * never .catch(() => {}); every exception surfaces to the user
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# ── Module-load env validation (RA-1441) ───────────────────────────────────
def _validate_env_at_load() -> None:
    """Log WARNING once at import time if required env is missing.

    Handlers still degrade gracefully per-call (the user sees
    "PI_CEO_PASSWORD ... not set on bot"), but surfacing the misconfiguration
    at process boot means operators see it in the very first log line rather
    than only when a user tries to use /linear at 2am.
    """
    import os

    missing = []
    if not (os.environ.get("PI_CEO_PASSWORD") or os.environ.get("TAO_PASSWORD")):
        missing.append("PI_CEO_PASSWORD or TAO_PASSWORD")
    if missing:
        logger.warning(
            "second_brain: bot will reject /linear /issue /pipeline /ship /plan "
            "/digest until env is fixed. Missing: %s",
            ", ".join(missing),
        )


_validate_env_at_load()


# ── Shared backend client (lazy) ────────────────────────────────────────────
def _backend_url() -> str:
    import os

    return os.environ.get(
        "PI_CEO_URL", "https://pi-dev-ops-production.up.railway.app"
    ).rstrip("/")


def _backend_pw() -> str:
    import os

    return (
        os.environ.get("PI_CEO_PASSWORD")
        or os.environ.get("TAO_PASSWORD")
        or ""
    ).strip()


def _escape_html(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _backend_call(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 20,
) -> tuple[bool, dict | str]:
    """Call a Pi-CEO backend route with the TAO password cookie.

    Returns (ok, result). On failure, result is an error string.
    """
    import json
    import urllib.error
    import urllib.request

    pw = _backend_pw()
    if not pw:
        return False, "PI_CEO_PASSWORD / TAO_PASSWORD env var not set on bot"

    backend = _backend_url()

    try:
        login_data = json.dumps({"password": pw}).encode()
        login_req = urllib.request.Request(
            f"{backend}/api/login",
            data=login_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(login_req, timeout=10) as resp:
            cookie_header = resp.headers.get("set-cookie", "")
            cookie = cookie_header.split(";")[0] if cookie_header else ""
        if not cookie:
            return False, "backend login failed — check password"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"login transport error: {exc}"

    try:
        headers = {"Cookie": cookie}
        if method == "GET":
            req = urllib.request.Request(f"{backend}{path}", headers=headers)
        else:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload or {}).encode()
            req = urllib.request.Request(
                f"{backend}{path}", data=body, headers=headers, method=method
            )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return True, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="ignore")[:300]
        return False, f"HTTP {exc.code}: {body}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"transport error: {exc}"
    except json.JSONDecodeError as exc:
        return False, f"bad JSON from backend: {exc}"


# ── /linear ─────────────────────────────────────────────────────────────────
async def linear_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Create a Linear issue with everything after /linear as the title."""
    if not update.message or not update.message.text:
        return

    text = update.message.text
    if text.startswith("/linear"):
        text = text[len("/linear") :].strip()

    if not text:
        await update.message.reply_text(
            "📝 <b>Create a Linear issue</b>\n\n"
            "Usage: <code>/linear Fix login bug on CARSI</code>\n"
            "Project is inferred from current_project_id (set via /projects) "
            "or falls back to Pi-Dev-Ops.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text("📝 Creating ticket...", parse_mode="HTML")

    project_id = None
    if context.user_data:
        project_id = context.user_data.get("current_project_id")

    ok, result = await _backend_call(
        "/api/telegram/linear/create",
        method="POST",
        payload={"title": text, "project_id": project_id},
    )
    if not ok:
        await update.message.reply_text(
            f"❌ Failed: {_escape_html(str(result))}", parse_mode="HTML"
        )
        return

    issue_id = result.get("identifier", "?")
    url = result.get("url", "")
    reply = f"✅ Created <code>{_escape_html(issue_id)}</code>"
    if url:
        reply += f"\n{_escape_html(url)}"

    await update.message.reply_text(reply, parse_mode="HTML")


# ── /status ─────────────────────────────────────────────────────────────────
async def status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Fetch a Linear issue status + latest comment."""
    if not update.message or not update.message.text:
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "🔎 <b>Issue lookup</b>\n\nUsage: <code>/issue RA-1234</code>",
            parse_mode="HTML",
        )
        return

    issue_id = parts[1].strip().upper()
    await update.message.reply_text(
        f"🔎 Looking up {_escape_html(issue_id)}...", parse_mode="HTML"
    )

    ok, result = await _backend_call(f"/api/telegram/linear/status/{issue_id}")
    if not ok:
        await update.message.reply_text(
            f"❌ {_escape_html(str(result))}", parse_mode="HTML"
        )
        return

    title = result.get("title", "?")
    state = result.get("state", "?")
    assignee = result.get("assignee", "unassigned")
    url = result.get("url", "")
    latest = result.get("latest_comment") or ""

    lines = [
        f"🔎 <b>{_escape_html(issue_id)}</b> — {_escape_html(state)}",
        f"<b>{_escape_html(title)}</b>",
        f"👤 {_escape_html(assignee)}",
    ]
    if latest:
        snippet = latest[:200] + ("…" if len(latest) > 200 else "")
        lines.append(f"💬 <i>{_escape_html(snippet)}</i>")

    buttons = []
    if url:
        buttons.append([InlineKeyboardButton("Open in Linear", url=url)])
    buttons.append(
        [
            InlineKeyboardButton("Pipeline", callback_data=f"sbpipe:{issue_id}"),
            InlineKeyboardButton("Ship", callback_data=f"sbship:{issue_id}"),
        ]
    )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


# ── /pipeline ───────────────────────────────────────────────────────────────
async def pipeline_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show build-session phase progress for a ticket."""
    if not update.message or not update.message.text:
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "🛠 <b>Pipeline</b>\n\nUsage: <code>/pipeline RA-1234</code>",
            parse_mode="HTML",
        )
        return

    issue_id = parts[1].strip().upper()
    await _render_pipeline(update, issue_id)


async def _render_pipeline(update: Update, issue_id: str) -> None:
    target = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if not target:
        return

    ok, result = await _backend_call(f"/api/telegram/pipeline/{issue_id}")
    if not ok:
        await target.reply_text(
            f"❌ {_escape_html(str(result))}", parse_mode="HTML"
        )
        return

    phases = result.get("phases", []) or []
    if not phases:
        await target.reply_text(
            f"🛠 No active pipeline for {_escape_html(issue_id)}",
            parse_mode="HTML",
        )
        return

    # Each phase: {"name": "plan", "status": "complete|running|failed|pending"}
    emoji = {
        "complete": "✅",
        "running": "⏳",
        "failed": "❌",
        "pending": "▫️",
    }
    lines = [f"🛠 <b>{_escape_html(issue_id)}</b> pipeline"]
    for ph in phases:
        name = ph.get("name", "?")
        st = ph.get("status", "pending")
        lines.append(f"{emoji.get(st, '▫️')} {_escape_html(name)}")

    await target.reply_text("\n".join(lines), parse_mode="HTML")


# ── /ship ───────────────────────────────────────────────────────────────────
async def ship_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Trigger ship_build via the Pi-CEO backend."""
    if not update.message or not update.message.text:
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "🚀 <b>Ship</b>\n\nUsage: <code>/ship RA-1234</code>",
            parse_mode="HTML",
        )
        return

    issue_id = parts[1].strip().upper()
    await update.message.reply_text(
        f"🚀 Triggering ship for {_escape_html(issue_id)}... "
        "This can take minutes; I'll report the result.",
        parse_mode="HTML",
    )

    ok, result = await _backend_call(
        "/api/telegram/ship",
        method="POST",
        payload={"issue_id": issue_id},
        timeout=120,
    )
    if not ok:
        await update.message.reply_text(
            f"❌ Ship failed: {_escape_html(str(result))}", parse_mode="HTML"
        )
        return

    session_id = result.get("session_id", "?")
    status = result.get("status", "?")
    await update.message.reply_text(
        f"🚀 Ship session <code>{_escape_html(session_id)}</code> → "
        f"<b>{_escape_html(status)}</b>",
        parse_mode="HTML",
    )


# ── /plan ───────────────────────────────────────────────────────────────────
async def plan_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Trigger plan_build with a free-text brief."""
    if not update.message or not update.message.text:
        return

    text = update.message.text
    if text.startswith("/plan"):
        text = text[len("/plan") :].strip()

    if not text:
        await update.message.reply_text(
            "🧭 <b>Plan</b>\n\n"
            "Usage: <code>/plan add dark mode toggle to CARSI dashboard</code>",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "🧭 Planning... (Sonnet-backed, ~30–60 s)", parse_mode="HTML"
    )

    project_id = None
    if context.user_data:
        project_id = context.user_data.get("current_project_id")

    ok, result = await _backend_call(
        "/api/telegram/plan",
        method="POST",
        payload={"brief": text, "project_id": project_id},
        timeout=90,
    )
    if not ok:
        await update.message.reply_text(
            f"❌ Plan failed: {_escape_html(str(result))}", parse_mode="HTML"
        )
        return

    outline = result.get("outline", "") or "(empty plan returned)"
    # Telegram message hard cap ~4096; be conservative
    snippet = outline[:3500] + ("\n…(truncated)" if len(outline) > 3500 else "")
    await update.message.reply_text(
        f"🧭 <b>Plan</b>\n\n<pre>{_escape_html(snippet)}</pre>",
        parse_mode="HTML",
    )


# ── /digest ─────────────────────────────────────────────────────────────────
async def digest_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Fetch an on-demand portfolio digest."""
    if not update.message:
        return

    await update.message.reply_text("📰 Assembling digest...", parse_mode="HTML")

    ok, result = await _backend_call("/api/telegram/digest", timeout=30)
    if not ok:
        await update.message.reply_text(
            f"❌ {_escape_html(str(result))}", parse_mode="HTML"
        )
        return

    text = result.get("text", "") or "(empty digest)"
    snippet = text[:3800] + ("\n…(truncated)" if len(text) > 3800 else "")
    await update.message.reply_text(snippet, parse_mode="HTML")


# ── Callback handlers for inline keyboards on /status ───────────────────────
async def second_brain_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route inline-button callbacks from /status cards."""
    query = update.callback_query
    if not query or not query.data:
        return

    if query.data.startswith("sbpipe:"):
        issue_id = query.data[len("sbpipe:") :]
        await query.answer()
        await _render_pipeline(update, issue_id)
        return

    if query.data.startswith("sbship:"):
        issue_id = query.data[len("sbship:") :]
        await query.answer(f"Triggering ship for {issue_id}...")
        ok, result = await _backend_call(
            "/api/telegram/ship",
            method="POST",
            payload={"issue_id": issue_id},
            timeout=120,
        )
        msg_target = query.message
        if not msg_target:
            return
        if not ok:
            await msg_target.reply_text(
                f"❌ Ship failed: {_escape_html(str(result))}",
                parse_mode="HTML",
            )
            return
        session_id = result.get("session_id", "?")
        status = result.get("status", "?")
        await msg_target.reply_text(
            f"🚀 <code>{_escape_html(session_id)}</code> → "
            f"<b>{_escape_html(status)}</b>",
            parse_mode="HTML",
        )
