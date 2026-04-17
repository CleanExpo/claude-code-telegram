"""
remote_control.py — RA-1101 — iPhone/iPad remote control handlers.

Three high-leverage agentic commands that turn the bot into a real
mobile control surface:

  /projects  — InlineKeyboardMarkup dropdown of every registered project.
               Tap a project → sets current_directory + sends a "ready" prompt
               so the next free-text message routes to the right repo.

  /health    — Pulls portfolio health from the Pi-CEO backend and formats
               it as a Telegram-safe message. One score per project, sorted
               worst-first so problems jump out.

  /idea      — Captures a free-text idea (everything after "/idea ") to
               `.harness/ideas-from-phone/YYYY-MM-DD.jsonl`. Future cron
               processes the file, pulls source links if present, routes to
               the right project's next board meeting.

All three are designed for one-thumb iPhone use:
- Short responses (Telegram preview is small)
- Tappable buttons rather than typed commands wherever possible
- HTML escape everything user-supplied (XSS-class issues in renderer)
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ..utils.html_format import escape_html

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────
# Resolve .harness/ relative to the telegram-bot package — works in both the
# Railway container (where Pi-Dev-Ops repo isn't fully present) and locally.
_HARNESS_FALLBACK = Path("/app/.harness")
_HARNESS_LOCAL = Path(__file__).resolve().parents[5] / ".harness"
HARNESS_DIR = _HARNESS_LOCAL if _HARNESS_LOCAL.exists() else _HARNESS_FALLBACK
PROJECTS_REGISTRY = HARNESS_DIR / "projects.json"
IDEAS_INBOX_DIR = HARNESS_DIR / "ideas-from-phone"


# ── /projects ──────────────────────────────────────────────────────────────
async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render an InlineKeyboardMarkup of registered projects (RA-1101)."""
    if not update.message:
        return

    projects = _load_projects()
    if not projects:
        await update.message.reply_text(
            "📁 No projects registered.\n\n"
            f"Looked in: <code>{escape_html(str(PROJECTS_REGISTRY))}</code>",
            parse_mode="HTML",
        )
        return

    # Build 2-column keyboard — easier on a phone screen than a single column
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for p in projects:
        # callback_data format: "proj:<project_id>" — handled by registered callback
        row.append(InlineKeyboardButton(
            text=p.get("id", "unknown"),
            callback_data=f"proj:{p.get('id', 'unknown')}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await update.message.reply_text(
        f"📁 <b>Pick a project</b> ({len(projects)} available):",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


async def projects_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'proj:<id>' callbacks from the /projects InlineKeyboard.

    Sets the user's working directory + sends a confirmation message so the
    next free-text message lands in the right repo's session.
    """
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("proj:"):
        return

    project_id = query.data[5:]  # strip "proj:"
    projects = _load_projects()
    project = next((p for p in projects if p.get("id") == project_id), None)

    if not project:
        await query.answer("Project not found", show_alert=True)
        return

    repo = project.get("repo", "")
    deployments = project.get("deployments", {})
    frontend = deployments.get("frontend", "")

    # Stash on user_data — agentic_text reads current_directory next time
    if context.user_data is not None:
        context.user_data["current_project_id"] = project_id
        # If the project's local path exists under approved_directory, switch to it
        # (best-effort — the agentic flow will validate)

    msg = (
        f"✅ <b>Now working on:</b> <code>{escape_html(project_id)}</code>\n"
        f"Repo: <code>{escape_html(repo)}</code>"
    )
    if frontend:
        msg += f"\nLive: {escape_html(frontend)}"
    msg += "\n\nWhat do you want to do? Just describe it in plain English."

    await query.answer()  # dismiss spinner
    await query.edit_message_text(msg, parse_mode="HTML")


# ── /health ────────────────────────────────────────────────────────────────
async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pull portfolio health from the Pi-CEO backend, render as a compact message."""
    if not update.message:
        return

    # Use the backend URL from env — same one Vercel proxy uses
    import os
    backend_url = os.environ.get("PI_CEO_URL", "https://pi-dev-ops-production.up.railway.app").rstrip("/")
    pw = os.environ.get("PI_CEO_PASSWORD", os.environ.get("TAO_PASSWORD", "")).strip()

    if not pw:
        await update.message.reply_text(
            "⚠ <b>Health check unavailable</b>\n"
            "PI_CEO_PASSWORD / TAO_PASSWORD env var not set on the bot.",
            parse_mode="HTML",
        )
        return

    try:
        # Login → get cookie → fetch /api/projects/health
        login_data = json.dumps({"password": pw}).encode()
        login_req = urllib.request.Request(
            f"{backend_url}/api/login",
            data=login_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(login_req, timeout=10) as resp:
            cookie_header = resp.headers.get("set-cookie", "")
            cookie = cookie_header.split(";")[0] if cookie_header else ""

        if not cookie:
            await update.message.reply_text(
                "⚠ Backend login failed — check PI_CEO_PASSWORD matches Railway TAO_PASSWORD.",
                parse_mode="HTML",
            )
            return

        health_req = urllib.request.Request(
            f"{backend_url}/api/projects/health",
            headers={"Cookie": cookie},
        )
        with urllib.request.urlopen(health_req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        await update.message.reply_text(
            f"❌ Health check failed: {escape_html(str(exc)[:200])}",
            parse_mode="HTML",
        )
        return

    if not isinstance(data, list) or not data:
        await update.message.reply_text("📊 No project health data available yet.")
        return

    # Sort worst-first
    data_sorted = sorted(data, key=lambda p: p.get("overall_health", 100))
    avg = sum(p.get("overall_health", 0) for p in data_sorted) / len(data_sorted)

    lines = [f"📊 <b>Portfolio Health</b> · avg <b>{avg:.0f}/100</b>", ""]
    for p in data_sorted:
        score = p.get("overall_health", 0)
        emoji = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"
        pid = escape_html(p.get("project_id", "?"))
        lines.append(f"{emoji} <code>{pid}</code> — {score}/100")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /idea ──────────────────────────────────────────────────────────────────
async def idea_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture a free-text idea to .harness/ideas-from-phone/YYYY-MM-DD.jsonl.

    Usage: /idea Anything I think of, including https://link.com sources.
    Future cron processes the file, pulls source links via Perplexity if any,
    routes to the right project's next board meeting via the Senior PM agent.
    """
    if not update.message or not update.message.text:
        return

    # Strip the command prefix to get the idea text
    text = update.message.text
    if text.startswith("/idea"):
        text = text[5:].strip()
    if not text:
        await update.message.reply_text(
            "💡 <b>Capture an idea</b>\n\n"
            "Usage: <code>/idea your thought here</code>\n"
            "Include URLs, project names, anything. The Senior PM agent will "
            "process it overnight and route to the right project's next board meeting.",
            parse_mode="HTML",
        )
        return

    user = update.effective_user
    user_id = user.id if user else 0
    user_name = user.first_name if user else "unknown"
    chat_id = update.effective_chat.id if update.effective_chat else 0

    # Save as JSONL — append-only, no race conditions, easy to tail
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "user_name": user_name,
        "chat_id": chat_id,
        "message_id": update.message.message_id,
        "text": text,
        "processed": False,
        "source": "telegram_idea_command",
    }

    try:
        IDEAS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
        with (IDEAS_INBOX_DIR / f"{today}.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.error("idea_command: failed to save: %s", exc)
        await update.message.reply_text(
            f"❌ Failed to save idea: {escape_html(str(exc)[:120])}",
            parse_mode="HTML",
        )
        return

    # Confirm with a short preview so the user can scroll-back-verify
    preview = text[:80] + ("…" if len(text) > 80 else "")
    await update.message.reply_text(
        f"💡 <b>Captured.</b> Will be processed in the next overnight cycle.\n\n"
        f"<i>{escape_html(preview)}</i>",
        parse_mode="HTML",
    )


# ── Helpers ────────────────────────────────────────────────────────────────
def _load_projects() -> list[dict]:
    """Load .harness/projects.json. Returns [] on any error (logged)."""
    try:
        with PROJECTS_REGISTRY.open() as f:
            data = json.load(f)
        return [p for p in data.get("projects", []) if p.get("id")]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("remote_control: failed to load projects: %s", exc)
        return []
