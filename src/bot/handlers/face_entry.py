"""
face_entry.py — Telegram handlers for face-auth entry gate (RA-1442).

Commands:
  /enroll   — set step flag; user's next photo = enrolment sample
  /lock     — set step flag; user's next photo = verification challenge
  /revoke   — immediately invalidate current face-auth token
  /whoami   — show face-auth state (enrolled? authorised? cooldown?)
  /cancel   — abort the current /enroll or /lock flow

Photo handler (register as MessageHandler for filters.PHOTO group 5):
  if user_data['face_step'] has 'enroll' intent  → enroll_from_photo_bytes
  if user_data['face_step'] has 'verify' intent  → verify_from_photo_bytes
  else pass through to existing photo handler

The face_step slot carries a {'step', 'at'} dict so we can expire stale
intent. Without a TTL, a /lock + unrelated photo days later would silently
be treated as a verification attempt.

Integration point: other handlers call src.security.face_auth.is_face_authorised(
user_id) before executing destructive commands. If False → they reply asking
the user to /lock first.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from telegram import Update
from telegram.ext import ContextTypes

from src.security import face_auth

logger = logging.getLogger(__name__)


# Seconds the /enroll or /lock intent stays primed before it expires. The
# bot tells the user to "send a selfie now" — a minute is long enough for
# the user to switch to camera, take the shot, and send, but short enough
# that a photo posted hours later is not mistaken for a verification.
_FACE_STEP_TTL_SECONDS = 120


def _set_face_step(context: ContextTypes.DEFAULT_TYPE, step: str) -> None:
    if context.user_data is None:
        return
    context.user_data["face_step"] = {"step": step, "at": time.time()}


def _consume_face_step(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Return the active step if still valid, then clear it. Else None."""
    if context.user_data is None:
        return None
    raw: Any = context.user_data.get("face_step")
    if not raw:
        return None
    # Legacy string form — treat as immediately-consumed, no TTL enforcement.
    if isinstance(raw, str):
        context.user_data["face_step"] = None
        return raw if raw in ("enroll", "verify") else None
    if not isinstance(raw, dict):
        context.user_data["face_step"] = None
        return None
    step = raw.get("step")
    at = raw.get("at", 0)
    context.user_data["face_step"] = None
    if step not in ("enroll", "verify"):
        return None
    if (time.time() - float(at)) > _FACE_STEP_TTL_SECONDS:
        logger.info(
            "face_step expired after %.1fs (TTL %ds) — dropping",
            time.time() - float(at),
            _FACE_STEP_TTL_SECONDS,
        )
        return None
    return step


# ── /enroll ────────────────────────────────────────────────────────────────
async def enroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    if not face_auth.is_available():
        await update.message.reply_text(
            "🔒 Face-auth unavailable — <code>face_recognition</code> library not "
            "installed on the bot host. Destructive commands will fall back to "
            "chat-ID whitelist only.",
            parse_mode="HTML",
        )
        return

    _set_face_step(context, "enroll")

    already = face_auth.is_enrolled(update.effective_user.id)
    prefix = "🔄 <b>Re-enrolment</b>" if already else "📸 <b>Enrolment</b>"
    await update.message.reply_text(
        f"{prefix}\n\n"
        "Send me a clear selfie — single face, good lighting, looking at the "
        "camera. I'll store a face embedding (not the photo itself) and use it "
        "to verify /lock attempts.\n\n"
        f"<i>You have {_FACE_STEP_TTL_SECONDS}s. Send /cancel to abort.</i>",
        parse_mode="HTML",
    )


# ── /lock ──────────────────────────────────────────────────────────────────
async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    if not face_auth.is_available():
        await update.message.reply_text(
            "🔓 Face-auth unavailable — destructive commands fall back to whitelist.",
            parse_mode="HTML",
        )
        return

    if not face_auth.is_enrolled(update.effective_user.id):
        await update.message.reply_text(
            "⚠️ You haven't enrolled yet. Run /enroll first and send a selfie.",
            parse_mode="HTML",
        )
        return

    _set_face_step(context, "verify")

    await update.message.reply_text(
        "🔐 <b>Verify</b>\n\n"
        "Send a selfie now. If the face matches your enrolled embedding, "
        "destructive commands (/linear /ship /plan) are unlocked for the next "
        "10 minutes.\n\n"
        f"<i>You have {_FACE_STEP_TTL_SECONDS}s. Send /cancel to abort.</i>",
        parse_mode="HTML",
    )


# ── /revoke ────────────────────────────────────────────────────────────────
async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    face_auth.revoke(update.effective_user.id)
    if context.user_data is not None:
        context.user_data["face_step"] = None
    await update.message.reply_text(
        "🔒 Face-auth token revoked. /lock again to re-authorise.",
        parse_mode="HTML",
    )


# ── /cancel ────────────────────────────────────────────────────────────────
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abort an in-flight /enroll or /lock flow without verifying."""
    if not update.message:
        return
    had = bool((context.user_data or {}).get("face_step"))
    if context.user_data is not None:
        context.user_data["face_step"] = None
    msg = "🚫 Face-auth flow cancelled." if had else "Nothing to cancel."
    await update.message.reply_text(msg, parse_mode="HTML")


# ── /whoami ────────────────────────────────────────────────────────────────
async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    available = face_auth.is_available()
    enrolled = face_auth.is_enrolled(user_id) if available else False
    authorised = face_auth.is_face_authorised(user_id) if available else True

    parts = [
        "👤 <b>Face-auth state</b>",
        f"  lib available: {'✓' if available else '✗'}",
        f"  enrolled: {'✓' if enrolled else '✗'}",
        f"  currently authorised: {'✓' if authorised else '✗'}",
    ]
    if not enrolled and available:
        parts.append("\nRun /enroll to set a reference selfie.")
    elif not authorised and available:
        parts.append("\nRun /lock and send a selfie to unlock destructive commands.")

    await update.message.reply_text("\n".join(parts), parse_mode="HTML")


# ── photo handler (group 5, runs before the generic agentic photo handler) ─
async def face_photo_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """If user is in /enroll or /lock flow, process the photo and return True.

    Returns False if no face step is active or the intent has expired — the
    caller can then pass the photo to the next handler.
    """
    if not update.message or not update.effective_user or not update.message.photo:
        return False

    step = _consume_face_step(context)
    if step is None:
        return False

    # Download highest-resolution photo
    photo = update.message.photo[-1]
    file = await photo.get_file()
    bio = await file.download_as_bytearray()
    photo_bytes = bytes(bio)

    user_id = update.effective_user.id

    if step == "enroll":
        result = face_auth.enroll_from_photo_bytes(user_id, photo_bytes)
        if result.ok:
            await update.message.reply_text(
                "✅ Enrolled. Destructive commands now require /lock "
                "(send another selfie to verify).",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"❌ Enrolment failed: {result.reason}", parse_mode="HTML"
            )
        return True

    # step == "verify"
    result = face_auth.verify_from_photo_bytes(user_id, photo_bytes)
    if result.ok:
        await update.message.reply_text(
            f"✅ Verified (similarity {result.similarity:.2f}). Unlocked for 10 min.",
            parse_mode="HTML",
        )
    elif result.cooldown_remaining_s:
        await update.message.reply_text(
            f"🚫 Too many failed attempts. Cool-down "
            f"{result.cooldown_remaining_s}s remaining.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"❌ Verification failed: {result.reason}", parse_mode="HTML"
        )
    return True
