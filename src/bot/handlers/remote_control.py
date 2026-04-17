"""
remote_control.py — RA-1101 — iPhone/iPad remote control handlers.

Three high-leverage agentic commands:
  /projects  — InlineKeyboardMarkup dropdown of every registered project
  /health    — Portfolio health pulled from the Pi-CEO backend
  /idea      — Captures a free-text idea to .harness/ideas-from-phone/

Designed to be ULTRA-DEFENSIVE on import: every Python-stdlib import below the
module docstring is lazy (inside the function body) so a configuration miss in
production can't take the bot down at startup. The orchestrator imports this
module synchronously — module-level errors would crash the bot.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# ── Helpers (lazy) ─────────────────────────────────────────────────────────
def _harness_dir():
    """Resolve .harness/ at call time — works in both Mac dev and Railway."""
    import os
    from pathlib import Path
    # Allow override via env var (Railway can set this to mount the harness if needed)
    env_path = os.environ.get("PI_CEO_HARNESS_DIR", "")
    if env_path:
        return Path(env_path)
    # Mac dev: walk up from this file to find .harness/ in parent monorepo
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".harness"
        if candidate.is_dir():
            return candidate
    # Railway fallback (won't have .harness mounted)
    return Path("/app/.harness")


def _escape_html(text: str) -> str:
    """Local fallback escape — avoids import-time dependency on utils.html_format."""
    if not isinstance(text, str):
        text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _load_projects() -> list[dict]:
    """Load .harness/projects.json. Returns [] on any error (logged)."""
    import json
    try:
        registry = _harness_dir() / "projects.json"
        with registry.open() as f:
            data = json.load(f)
        return [p for p in data.get("projects", []) if p.get("id")]
    except Exception as exc:  # noqa: BLE001 — we want to swallow everything here
        logger.warning("remote_control: failed to load projects: %s", exc)
        return []


# ── /projects ──────────────────────────────────────────────────────────────
async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render an InlineKeyboardMarkup of registered projects (RA-1101)."""
    if not update.message:
        return

    projects = _load_projects()
    if not projects:
        await update.message.reply_text(
            "📁 No projects registered.\n\n"
            f"Looked in: <code>{_escape_html(str(_harness_dir() / 'projects.json'))}</code>",
            parse_mode="HTML",
        )
        return

    # 2-column keyboard, easier to tap on a phone
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for p in projects:
        pid = p.get("id", "unknown")
        row.append(InlineKeyboardButton(text=pid, callback_data=f"proj:{pid}"))
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
    """Handle 'proj:<id>' callbacks from the /projects InlineKeyboard."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("proj:"):
        return

    project_id = query.data[5:]
    projects = _load_projects()
    project = next((p for p in projects if p.get("id") == project_id), None)

    if not project:
        await query.answer("Project not found", show_alert=True)
        return

    repo = project.get("repo", "")
    deployments = project.get("deployments", {}) or {}
    frontend = deployments.get("frontend", "")

    if context.user_data is not None:
        context.user_data["current_project_id"] = project_id

    msg = (
        f"✅ <b>Now working on:</b> <code>{_escape_html(project_id)}</code>\n"
        f"Repo: <code>{_escape_html(repo)}</code>"
    )
    if frontend:
        msg += f"\nLive: {_escape_html(frontend)}"
    msg += "\n\nWhat do you want to do? Just describe it in plain English."

    await query.answer()
    await query.edit_message_text(msg, parse_mode="HTML")


# ── /health ────────────────────────────────────────────────────────────────
async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pull portfolio health from the Pi-CEO backend, render as a compact message."""
    if not update.message:
        return

    import json
    import os
    import urllib.request
    import urllib.error

    backend_url = os.environ.get("PI_CEO_URL", "https://pi-dev-ops-production.up.railway.app").rstrip("/")
    pw = (os.environ.get("PI_CEO_PASSWORD") or os.environ.get("TAO_PASSWORD") or "").strip()

    if not pw:
        await update.message.reply_text(
            "⚠ <b>Health check unavailable</b>\n"
            "PI_CEO_PASSWORD / TAO_PASSWORD env var not set on the bot.",
            parse_mode="HTML",
        )
        return

    try:
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

    except Exception as exc:  # noqa: BLE001 — surface any failure to the user
        await update.message.reply_text(
            f"❌ Health check failed: {_escape_html(str(exc)[:200])}",
            parse_mode="HTML",
        )
        return

    if not isinstance(data, list) or not data:
        await update.message.reply_text("📊 No project health data available yet.")
        return

    data_sorted = sorted(data, key=lambda p: p.get("overall_health", 100))
    avg = sum(p.get("overall_health", 0) for p in data_sorted) / len(data_sorted)

    lines = [f"📊 <b>Portfolio Health</b> · avg <b>{avg:.0f}/100</b>", ""]
    for p in data_sorted:
        score = p.get("overall_health", 0)
        emoji = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"
        pid = _escape_html(p.get("project_id", "?"))
        lines.append(f"{emoji} <code>{pid}</code> — {score}/100")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /idea ──────────────────────────────────────────────────────────────────
async def idea_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture a free-text idea to .harness/ideas-from-phone/YYYY-MM-DD.jsonl."""
    if not update.message or not update.message.text:
        return

    import json
    from datetime import datetime, timezone

    text = update.message.text
    if text.startswith("/idea"):
        text = text[5:].strip()
    if not text:
        await update.message.reply_text(
            "💡 <b>Capture an idea</b>\n\n"
            "Usage: <code>/idea your thought here</code>\n"
            "Include URLs, project names, anything. The Senior PM agent processes "
            "it overnight and routes to the right project's next board meeting.",
            parse_mode="HTML",
        )
        return

    user = update.effective_user
    user_id = user.id if user else 0
    user_name = user.first_name if user else "unknown"
    chat_id = update.effective_chat.id if update.effective_chat else 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    record = {
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
        inbox = _harness_dir() / "ideas-from-phone"
        inbox.mkdir(parents=True, exist_ok=True)
        with (inbox / f"{today}.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.error("idea_command: failed to save: %s", exc)
        await update.message.reply_text(
            f"❌ Failed to save idea: {_escape_html(str(exc)[:120])}",
            parse_mode="HTML",
        )
        return

    preview = text[:80] + ("…" if len(text) > 80 else "")
    await update.message.reply_text(
        f"💡 <b>Captured.</b> Will be processed in the next overnight cycle.\n\n"
        f"<i>{_escape_html(preview)}</i>",
        parse_mode="HTML",
    )
