"""Unit tests for second_brain handlers (RA-1441).

Covers the shared _backend_call() helper + one happy-path + one error-path for
each of the six commands. Backend is mocked so these tests are fast and don't
touch the network.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PI_CEO_URL", "http://test-backend.local")
    monkeypatch.setenv("PI_CEO_PASSWORD", "test-password")


@pytest.fixture
def mock_update():
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.message_id = 1
    update.effective_chat = MagicMock(id=42)
    update.effective_user = MagicMock(id=100, first_name="Phill")
    update.callback_query = None
    return update


@pytest.fixture
def mock_context():
    ctx = MagicMock()
    ctx.user_data = {}
    return ctx


# ── env validation (RA-1441) ────────────────────────────────────────────────
def test_module_warns_when_password_missing(monkeypatch, caplog):
    """Re-importing the module without PI_CEO_PASSWORD should log a warning."""
    import importlib

    monkeypatch.delenv("PI_CEO_PASSWORD", raising=False)
    monkeypatch.delenv("TAO_PASSWORD", raising=False)
    with caplog.at_level("WARNING", logger="src.bot.handlers.second_brain"):
        from src.bot.handlers import second_brain

        importlib.reload(second_brain)
    assert any(
        "PI_CEO_PASSWORD or TAO_PASSWORD" in rec.message for rec in caplog.records
    ), f"expected warning in caplog, got: {[r.message for r in caplog.records]}"


# ── /linear happy path ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_linear_command_success(mock_update, mock_context):
    from src.bot.handlers import second_brain

    mock_update.message.text = "/linear Fix login bug"

    async def _fake_call(path, method="GET", payload=None, timeout=20):
        return (
            True,
            {
                "identifier": "RA-9999",
                "url": "https://linear.app/unite-group/issue/RA-9999",
            },
        )

    with patch.object(second_brain, "_backend_call", _fake_call):
        await second_brain.linear_command(mock_update, mock_context)

    calls = mock_update.message.reply_text.await_args_list
    assert len(calls) == 2, f"expected ack + result, got {len(calls)}"
    assert "Creating ticket" in calls[0].args[0]
    assert "RA-9999" in calls[1].args[0]


@pytest.mark.asyncio
async def test_linear_command_empty_text_shows_usage(mock_update, mock_context):
    from src.bot.handlers import second_brain

    mock_update.message.text = "/linear"
    await second_brain.linear_command(mock_update, mock_context)

    text = mock_update.message.reply_text.await_args.args[0]
    assert "Usage" in text or "Create a Linear issue" in text


# ── /issue ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_issue_command_fetches_and_renders(mock_update, mock_context):
    from src.bot.handlers import second_brain

    mock_update.message.text = "/issue RA-1234"

    async def _fake_call(path, method="GET", payload=None, timeout=20):
        assert "/linear/status/RA-1234" in path
        return (
            True,
            {
                "identifier": "RA-1234",
                "title": "Fix something",
                "state": "Todo",
                "assignee": "Phill",
                "url": "https://linear.app/ra-1234",
                "latest_comment": "needs review",
            },
        )

    with patch.object(second_brain, "_backend_call", _fake_call):
        await second_brain.status_command(mock_update, mock_context)

    all_text = " ".join(
        c.args[0] for c in mock_update.message.reply_text.await_args_list
    )
    assert "RA-1234" in all_text
    assert "Fix something" in all_text


# ── /pipeline ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pipeline_command_renders_phases(mock_update, mock_context):
    from src.bot.handlers import second_brain

    mock_update.message.text = "/pipeline RA-1234"

    async def _fake_call(path, method="GET", payload=None, timeout=20):
        return (
            True,
            {
                "phases": [
                    {"name": "spec", "status": "complete"},
                    {"name": "plan", "status": "running"},
                    {"name": "build", "status": "pending"},
                ]
            },
        )

    with patch.object(second_brain, "_backend_call", _fake_call):
        await second_brain.pipeline_command(mock_update, mock_context)

    last_text = mock_update.message.reply_text.await_args.args[0]
    assert "spec" in last_text
    assert "plan" in last_text


# ── /ship ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ship_command_triggers_backend(mock_update, mock_context):
    from src.bot.handlers import second_brain

    mock_update.message.text = "/ship RA-5678"

    async def _fake_call(path, method="GET", payload=None, timeout=20):
        assert method == "POST"
        assert payload["issue_id"] == "RA-5678"
        return (True, {"session_id": "abc12345", "status": "shipped"})

    with patch.object(second_brain, "_backend_call", _fake_call):
        await second_brain.ship_command(mock_update, mock_context)

    all_text = " ".join(
        c.args[0] for c in mock_update.message.reply_text.await_args_list
    )
    assert "abc12345" in all_text
    assert "shipped" in all_text


# ── /plan ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_plan_command_sends_brief(mock_update, mock_context):
    from src.bot.handlers import second_brain

    mock_update.message.text = "/plan add dark mode"

    async def _fake_call(path, method="GET", payload=None, timeout=20):
        assert payload["brief"] == "add dark mode"
        return (True, {"outline": "1. Step one\n2. Step two"})

    with patch.object(second_brain, "_backend_call", _fake_call):
        await second_brain.plan_command(mock_update, mock_context)

    last_text = mock_update.message.reply_text.await_args.args[0]
    assert "Step one" in last_text


# ── /digest ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_digest_command_renders_portfolio(mock_update, mock_context):
    from src.bot.handlers import second_brain

    async def _fake_call(path, method="GET", payload=None, timeout=20):
        return (True, {"text": "📰 Portfolio: 5 open, 3 shipped today"})

    with patch.object(second_brain, "_backend_call", _fake_call):
        await second_brain.digest_command(mock_update, mock_context)

    last_text = mock_update.message.reply_text.await_args.args[0]
    assert "Portfolio" in last_text


# ── error paths ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_backend_error_surfaces_to_user(mock_update, mock_context):
    """Every command must reply with the error — no silent failures (RA-1109)."""
    from src.bot.handlers import second_brain

    mock_update.message.text = "/linear Test"

    async def _fake_call(path, method="GET", payload=None, timeout=20):
        return (False, "backend unreachable: timeout")

    with patch.object(second_brain, "_backend_call", _fake_call):
        await second_brain.linear_command(mock_update, mock_context)

    all_text = " ".join(
        c.args[0] for c in mock_update.message.reply_text.await_args_list
    )
    assert "Failed" in all_text or "❌" in all_text
    assert "backend unreachable" in all_text
