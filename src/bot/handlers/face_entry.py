"""
face_entry.py — Telegram handlers for face-auth entry gate (RA-1442).

Commands:
  /enroll   — set step flag; user's next photo = enrolment sample
  /lock     — set step flag; user's next photo = verification challenge
  /revoke   — immediately invalidate current face-auth token
  /whoami   — show face-auth state (enrolled? authorised? cooldown?)

Photo handler (register as MessageHandler for filters.PHOTO group 5):
  if user_data['face_step'] == 'enroll'  → call enroll_from_photo_bytes
  if user_data['face_step'] == 'verify'  → call verify_from_photo_bytes
  else pass through to existing photo handler

Integration point: other handlers call src.security.face_auth.is_face_authorised(
user_id) before executing destructive commands. If False → they reply asking
the user to /lock first.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.security import face_auth

logger = logging.getLogger(__name__)


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

    if context.user_data is not None:
        context.user_data["face_step"] = "enroll"

    already = face_auth.is_enrolled(update.effective_user.id)
    prefix = "🔄 <b>Re-enrolment</b>" if already else "📸 <b>Enrolment</b>"
    await update.message.reply_text(
        f"{prefix}\n\n"
        "Send me a clear selfie — single face, good lighting, looking at the "
        "camera. I'll store a face embedding (not the photo itself) and use it "
        "to verify /lock attempts.\n\n"
        "<i>Send /cancel to abort.</i>",
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

    if context.user_data is not None:
        context.user_data["face_step"] = "verify"

    await update.message.reply_text(
        "🔐 <b>Verify</b>\n\n"
        "Send a selfie now. If the face matches your enrolled embedding, "
        "destructive commands (/linear /ship /plan) are unlocked for the next "
        "10 minutes.",
        parse_mode="HTML",
    )


# ── /revoke ────────────────────────────────────────────────────────────────
async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    face_auth.revoke(update.effective_user.id)
    await update.message.reply_text(
        "🔒 Face-auth token revoked. /lock again to re-authorise.",
        parse_mode="HTML",
    )


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

    Returns False if no face step is active — caller can then pass the photo
    to the next handler.
    """
    if not update.message or not update.effective_user or not update.message.photo:
        return False
    step = (context.user_data or {}).get("face_step")
    if step not in ("enroll", "verify"):
        return False

    if context.user_data is not None:
        context.user_data["face_step"] = None  # consume

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
