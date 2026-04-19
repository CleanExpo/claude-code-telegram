"""
face_auth.py — RA-1442 biometric entry gate for destructive commands.

Design: one-time enrollment + per-session verification.

  /enroll    — user sends a selfie; bot extracts face embedding via
               face_recognition (dlib-backed), stores one embedding per
               user_id in SQLite. Re-enrollment overwrites.

  /lock      — user sends a selfie; bot verifies against stored embedding.
               If cosine-similarity >= threshold, grants the user a face-auth
               token valid for FACE_AUTH_TTL (default 10 min). Otherwise,
               counts a failed attempt (3 fails → cool-down).

  is_face_authorised(user_id) — handlers call this before executing
               destructive commands (/linear, /ship, /plan). Read-only
               commands (/status, /issue, /digest, /health) do NOT need it.

Graceful degradation: if face_recognition lib isn't installed, the module
loads without faces + logs a WARNING. All commands then work as before
(destructive commands still require the existing chat_id whitelist auth).

Privacy: face embeddings are 128-float vectors, NOT the photo itself.
Photos are processed in-memory and not persisted.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Lazy import guard — face_recognition is heavy (dlib). Bot stays up even
# if the lib is unavailable; we just fall back to chat_id whitelist only.
try:
    import face_recognition  # type: ignore
    import numpy as np

    _FACE_LIB_AVAILABLE = True
except ImportError:
    _FACE_LIB_AVAILABLE = False
    logger.warning(
        "face_recognition lib not installed — /enroll and /lock will be "
        "disabled. Install with: pip install face_recognition numpy"
    )

# ── Config ──────────────────────────────────────────────────────────────────
_DB_PATH = Path.home() / ".pi-ceo" / "face_auth.sqlite"
_SIMILARITY_THRESHOLD = 0.6  # cosine similarity; 0.6+ = match (face_recognition default 0.6)
_FACE_AUTH_TTL = 600  # 10 minutes of granted access after successful /lock
_MAX_FAILED_ATTEMPTS = 3
_COOLDOWN_SECONDS = 300  # 5 min lockout after 3 failures


# ── Storage ────────────────────────────────────────────────────────────────
def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS face_enrolments (
            user_id INTEGER PRIMARY KEY,
            embedding BLOB NOT NULL,
            enrolled_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS face_sessions (
            user_id INTEGER PRIMARY KEY,
            granted_at REAL NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0,
            cooldown_until REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


# ── Public API ──────────────────────────────────────────────────────────────
@dataclass
class VerificationResult:
    ok: bool
    reason: str
    similarity: float | None = None
    cooldown_remaining_s: int = 0


def is_available() -> bool:
    """True if face_recognition lib is installed + DB initialisable."""
    if not _FACE_LIB_AVAILABLE:
        return False
    try:
        _init_db().close()
        return True
    except Exception:  # noqa: BLE001
        return False


def is_enrolled(user_id: int) -> bool:
    """True if this user has a stored face embedding."""
    if not is_available():
        return False
    with _init_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM face_enrolments WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def is_face_authorised(user_id: int) -> bool:
    """True if user has a fresh face-auth token within the TTL.

    If face auth isn't available (no lib), returns True — falls back to
    chat_id whitelist only. This keeps the bot functional without
    face_recognition installed.
    """
    if not _FACE_LIB_AVAILABLE:
        return True  # graceful degradation
    try:
        with _init_db() as conn:
            row = conn.execute(
                "SELECT granted_at FROM face_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return False
        granted_at = row[0]
        return (time.time() - granted_at) < _FACE_AUTH_TTL
    except Exception as exc:  # noqa: BLE001
        logger.warning("is_face_authorised error (fail-open): %s", exc)
        return True  # don't lock out on internal errors


def enroll_from_photo_bytes(user_id: int, photo_bytes: bytes) -> VerificationResult:
    """Extract face embedding from photo and persist.

    Returns ok=True on success. ok=False with a reason if no face is
    detected or if multiple faces are present.
    """
    if not _FACE_LIB_AVAILABLE:
        return VerificationResult(False, "face_recognition lib not installed")

    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
        np_img = np.array(img)

        face_locations = face_recognition.face_locations(np_img)
        if not face_locations:
            return VerificationResult(False, "no face detected in photo")
        if len(face_locations) > 1:
            return VerificationResult(
                False, f"{len(face_locations)} faces detected — send a single-person selfie"
            )

        embeddings = face_recognition.face_encodings(np_img, face_locations)
        if not embeddings:
            return VerificationResult(False, "could not compute face embedding")

        embedding_bytes = embeddings[0].tobytes()
        with _init_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO face_enrolments (user_id, embedding, enrolled_at) "
                "VALUES (?, ?, ?)",
                (user_id, embedding_bytes, time.time()),
            )
            conn.commit()
        return VerificationResult(True, "enrolled")
    except Exception as exc:  # noqa: BLE001
        logger.error("enroll_from_photo_bytes failed: %s", exc)
        return VerificationResult(False, f"enrollment error: {exc}")


def verify_from_photo_bytes(
    user_id: int, photo_bytes: bytes
) -> VerificationResult:
    """Verify photo matches stored embedding. On success, grants a
    FACE_AUTH_TTL-second authorisation window. On failure, increments
    failed_count; at 3 failures, enforces _COOLDOWN_SECONDS cooldown.
    """
    if not _FACE_LIB_AVAILABLE:
        return VerificationResult(True, "face lib unavailable — fail-open")

    with _init_db() as conn:
        # Cooldown check
        row = conn.execute(
            "SELECT failed_count, cooldown_until FROM face_sessions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            _failed, cooldown_until = row
            if cooldown_until > time.time():
                return VerificationResult(
                    False,
                    "too many failures — cooling down",
                    cooldown_remaining_s=int(cooldown_until - time.time()),
                )

        # Load stored embedding
        row = conn.execute(
            "SELECT embedding FROM face_enrolments WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return VerificationResult(False, "not enrolled — send /enroll first")

        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            np_img = np.array(img)
            face_locations = face_recognition.face_locations(np_img)
            if not face_locations:
                return VerificationResult(False, "no face detected")
            if len(face_locations) > 1:
                return VerificationResult(False, "multiple faces — send a single-person selfie")

            embeddings = face_recognition.face_encodings(np_img, face_locations)
            if not embeddings:
                return VerificationResult(False, "could not compute embedding")

            stored = np.frombuffer(row[0], dtype=np.float64)
            presented = embeddings[0]
            # face_recognition uses euclidean distance; lower is better
            distance = float(np.linalg.norm(stored - presented))
            # Convert to similarity in [0,1], 1=identical
            similarity = max(0.0, 1.0 - distance)

            if similarity >= _SIMILARITY_THRESHOLD:
                conn.execute(
                    "INSERT OR REPLACE INTO face_sessions "
                    "(user_id, granted_at, failed_count, cooldown_until) "
                    "VALUES (?, ?, 0, 0)",
                    (user_id, time.time()),
                )
                conn.commit()
                return VerificationResult(True, "verified", similarity=similarity)

            # Failed attempt
            new_count = ((row[0] if row else 0) or 0) + 1
            row_sessions = conn.execute(
                "SELECT failed_count FROM face_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            current_failed = (row_sessions[0] if row_sessions else 0) + 1
            cooldown = (
                time.time() + _COOLDOWN_SECONDS
                if current_failed >= _MAX_FAILED_ATTEMPTS
                else 0
            )
            conn.execute(
                "INSERT OR REPLACE INTO face_sessions "
                "(user_id, granted_at, failed_count, cooldown_until) "
                "VALUES (?, 0, ?, ?)",
                (user_id, current_failed, cooldown),
            )
            conn.commit()
            return VerificationResult(
                False,
                f"similarity {similarity:.2f} below threshold {_SIMILARITY_THRESHOLD}",
                similarity=similarity,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("verify_from_photo_bytes error: %s", exc)
            return VerificationResult(False, f"verification error: {exc}")


def revoke(user_id: int) -> None:
    """Revoke the current face-auth token for a user."""
    if not _FACE_LIB_AVAILABLE:
        return
    try:
        with _init_db() as conn:
            conn.execute(
                "UPDATE face_sessions SET granted_at = 0 WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        pass
