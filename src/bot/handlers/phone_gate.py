"""phone_gate.py — RA-1457 S-slice

Callback handler for the Authority-Prompt buttons the backend sends to
Telegram. The backend (Pi-Dev-Ops FastAPI) creates and edits the cards
directly — this module handles only the `approve:{gate_id}` and
`deny:{gate_id}` button taps and forwards them to the backend's
/api/phone/gate/{gid}/resolve endpoint.

Defensive-on-import pattern matches remote_control.py: every stdlib import
below the docstring is done lazily inside the function body so a missing
config in production can't crash the bot at startup.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_CALLBACK_PREFIXES = ("approve:", "deny:")


def _backend_base() -> str:
    import os
    base = os.environ.get("PICEO_BACKEND_URL", "").strip()
    if not base:
        base = "https://pi-dev-ops-production.up.railway.app"
    return base.rstrip("/")


def _backend_password() -> str:
    import os
    return os.environ.get("TAO_PASSWORD", "").strip()


def _escape_html(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _backend_resolve(gate_id: str, status: str, user_id: int) -> tuple[bool, str]:
    """POST /api/phone/gate/{gid}/resolve. Returns (ok, detail)."""
    import json
    import urllib.error
    import urllib.request

    base = _backend_base()
    pw = _backend_password()
    if not pw:
        return False, "TAO_PASSWORD not set on bot"

    # Login to obtain session cookie — backend auth is bcrypt password -> token
    try:
        login_req = urllib.request.Request(
            f"{base}/api/login",
            data=json.dumps({"password": pw}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(login_req, timeout=10) as resp:
            cookies = resp.headers.get_all("Set-Cookie") or []
            token = None
            for c in cookies:
                if c.startswith("tao_session="):
                    token = c.split(";", 1)[0].split("=", 1)[1]
                    break
            if not token:
                body = json.loads(resp.read() or b"{}")
                token = body.get("token", "")
    except urllib.error.URLError as exc:
        return False, f"login transport: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"login error: {exc}"

    if not token:
        return False, "backend login returned no token"

    payload = json.dumps({"status": status, "by_user_id": user_id}).encode()
    req = urllib.request.Request(
        f"{base}/api/phone/gate/{gate_id}/resolve",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, resp.read().decode(errors="ignore")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="ignore") if exc.fp else ""
        return False, f"resolve HTTP {exc.code}: {detail}"
    except urllib.error.URLError as exc:
        return False, f"resolve transport: {exc}"


async def phone_gate_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle approve:<gid> / deny:<gid> button taps.

    The backend already edits the card to its terminal state when the
    resolve call succeeds. We only need to answer the callback — but if
    the backend round-trip fails, we must surface that on the card so the
    user isn't left staring at a pending card that will silently expire.
    """
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if not data.startswith(_CALLBACK_PREFIXES):
        return

    try:
        action, gate_id = data.split(":", 1)
    except ValueError:
        await query.answer("Malformed callback", show_alert=True)
        return

    status = "approved" if action == "approve" else "denied"
    user_id = query.from_user.id if query.from_user else 0

    ok, detail = await _backend_resolve(gate_id, status, user_id)

    if ok:
        # Backend edits the card itself — just ack the tap with a toast
        await query.answer(
            "✓ Approved" if status == "approved" else "✗ Denied",
        )
        logger.info(
            "phone_gate resolved gate=%s status=%s user=%s", gate_id, status, user_id
        )
        return

    # Backend unreachable — surface it so the user knows to retry or ssh in
    logger.error("phone_gate resolve failed gate=%s: %s", gate_id, detail)
    await query.answer("Backend unreachable — tap again in a moment", show_alert=True)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"⚠️ <b>Authority prompt resolve failed</b>\n\n"
            f"Gate <code>{_escape_html(gate_id)}</code> could not be resolved: "
            f"<code>{_escape_html(detail[:200])}</code>\n\n"
            f"The Mac-side hook will time out and default to <b>Deny</b>.",
            parse_mode="HTML",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("phone_gate failure-surface edit failed: %s", exc)
