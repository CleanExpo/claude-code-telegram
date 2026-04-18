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
# RA-1404 — Two-stage capture:
#   (1) Post directly to Linear at call time. Returns RA-xxxx + URL in the reply.
#       Removes the ~12h gap from the old GitHub Actions cron flow AND removes
#       the dependency on .harness/ideas-from-phone/ being mounted into the
#       Railway container (it isn't — the old writer wrote to an ephemeral
#       /app/.harness/ path that was never pushed to git).
#   (2) Always append to .harness/ideas-from-phone/YYYY-MM-DD.jsonl as a local
#       audit trail. If stage (1) fails, the user sees the failure and still
#       has the audit entry. If both fail, the user sees a clear error.
_LINEAR_API_URL = "https://api.linear.app/graphql"
# Default triage: RestoreAssist team → Pi-Dev-Ops project (canonical in CLAUDE.md)
_DEFAULT_TEAM_ID = "a8a52f07-63cf-4ece-9ad2-3e3bd3c15673"
_DEFAULT_PROJECT_ID = "f45212be-3259-4bfb-89b1-54c122c939a7"


def _linear_create_issue_from_idea(
    api_key: str, text: str, user_name: str, user_id: int, chat_id: int, message_id: int
) -> tuple[bool, str, str]:
    """POST a Linear issue. Returns (ok, identifier_or_error, url).

    Stdlib-only to keep the bot's dependency surface minimal.
    """
    import json as _json
    import urllib.error
    import urllib.request

    title = text.splitlines()[0][:250] if text else "(empty idea)"
    description = (
        f"Captured via Telegram `/idea` from **{user_name}** "
        f"(tg_user={user_id}, tg_chat={chat_id}, tg_msg={message_id}).\n\n"
        f"---\n\n{text}"
    )
    query = (
        "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { "
        "success issue { identifier url } } }"
    )
    variables = {
        "input": {
            "teamId": _DEFAULT_TEAM_ID,
            "projectId": _DEFAULT_PROJECT_ID,
            "title": title,
            "description": description,
            "priority": 3,  # Normal — triage queue
        }
    }
    payload = _json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        _LINEAR_API_URL,
        data=payload,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}", ""
    except Exception as exc:  # noqa: BLE001 — stdlib can raise many; surface all
        return False, f"{type(exc).__name__}: {str(exc)[:120]}", ""

    if data.get("errors"):
        return False, str(data["errors"])[:200], ""
    result = (data.get("data") or {}).get("issueCreate") or {}
    if not result.get("success"):
        return False, "issueCreate.success=false", ""
    issue = result.get("issue") or {}
    return True, issue.get("identifier", "?"), issue.get("url", "")


async def idea_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture an idea: create a Linear ticket immediately + append JSONL audit trail."""
    if not update.message or not update.message.text:
        return

    import json
    import os
    from datetime import datetime, timezone

    text = update.message.text
    if text.startswith("/idea"):
        text = text[5:].strip()
    if not text:
        await update.message.reply_text(
            "💡 <b>Capture an idea</b>\n\n"
            "Usage: <code>/idea your thought here</code>\n"
            "Include URLs, project names, anything. Creates a Linear triage "
            "ticket immediately and the Senior PM agent routes it.",
            parse_mode="HTML",
        )
        return

    user = update.effective_user
    user_id = user.id if user else 0
    user_name = user.first_name if user else "unknown"
    chat_id = update.effective_chat.id if update.effective_chat else 0

    # Stage 1 — Linear direct call (primary path)
    api_key = (os.environ.get("LINEAR_API_KEY") or "").strip()
    linear_ok = False
    linear_id = ""
    linear_url = ""
    linear_err = ""
    if api_key:
        linear_ok, payload, linear_url = _linear_create_issue_from_idea(
            api_key, text, user_name, user_id, chat_id, update.message.message_id
        )
        if linear_ok:
            linear_id = payload
        else:
            linear_err = payload
    else:
        linear_err = "LINEAR_API_KEY env var not set on bot"

    # Stage 2 — JSONL audit trail (always, for idempotency + offline debugging)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "user_name": user_name,
        "chat_id": chat_id,
        "message_id": update.message.message_id,
        "text": text,
        "processed": linear_ok,
        "linear_identifier": linear_id,
        "linear_url": linear_url,
        "source": "telegram_idea_command",
    }
    jsonl_ok = False
    jsonl_err = ""
    try:
        inbox = _harness_dir() / "ideas-from-phone"
        inbox.mkdir(parents=True, exist_ok=True)
        with (inbox / f"{today}.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        jsonl_ok = True
    except Exception as exc:  # noqa: BLE001
        jsonl_err = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("idea_command: jsonl audit write failed: %s", exc)

    # Terminal state — surface honestly, no silent success (RA-1109)
    preview = text[:80] + ("…" if len(text) > 80 else "")
    if linear_ok:
        await update.message.reply_text(
            f"✅ <b>Linear ticket created:</b> <code>{_escape_html(linear_id)}</code>\n"
            f"{_escape_html(linear_url)}\n\n"
            f"<i>{_escape_html(preview)}</i>",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        return

    # Linear failed — tell the user why, both stages
    msg = (
        f"⚠ <b>Linear ticket NOT created.</b>\n"
        f"Reason: <code>{_escape_html(linear_err[:160])}</code>\n\n"
    )
    if jsonl_ok:
        msg += "Captured to local JSONL audit trail only.\n\n"
    else:
        msg += f"❌ JSONL audit also failed: <code>{_escape_html(jsonl_err)}</code>\n\n"
    msg += f"<i>{_escape_html(preview)}</i>"
    await update.message.reply_text(msg, parse_mode="HTML")
