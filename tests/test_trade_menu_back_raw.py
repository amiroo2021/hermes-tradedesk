"""Regression tests for the Back/Raw keyboard added to read-only
report screens (balance, positions, open orders) under /trade.

The exchange agent already returns both the formatted, exchange-agnostic
message (``result["message"]``) and the full debug payload
(``result["data"]["exchange_response"]`` and
``result["data"]["raw_response"]``). The wizard stores the full
result in ``state["_final_raw_result"]`` and renders it via the
``raw`` callback when the operator clicks the "Raw" button.

Constraint reminder: only the wizard changed. The exchange agents
already produce the structured debug payload.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock


PUB = Path(__file__).parent.parent
WIZARD_PATH = PUB / "hermes_overlay" / "telegram" / "trade_menu" / "wizard.py"


def _import_wizard():
    sys.path.insert(0, str(PUB))
    return __import__("plugins.platforms.telegram.trade_menu.wizard", fromlist=["*"])


def test_final_view_keyboard_exists():
    """_final_view_keyboard should produce a keyboard with Back, Raw, Cancel."""
    wiz = _import_wizard()
    assert hasattr(wiz, "_final_view_keyboard"), (
        "_final_view_keyboard helper missing"
    )


def test_final_view_keyboard_has_back_raw_cancel():
    """The keyboard should have Back, Raw, and Cancel buttons (no submit)."""
    wiz = _import_wizard()
    # _markup wraps rows into InlineKeyboardMarkup (or fallback stub).
    # Walk the keyboard's structure.
    kb = wiz._final_view_keyboard()
    # Find all button labels in the keyboard.
    text = str(kb)
    assert "Back" in text or "\u2b05\ufe0f Back" in text or "\u2b05 Back" in text, (
        f"final_view_keyboard missing Back button: {text[:300]}"
    )
    assert "Raw" in text or "\ud83d\udccb Raw" in text, (
        f"final_view_keyboard missing Raw button: {text[:300]}"
    )
    assert "Cancel" in text or "\u274c Cancel" in text or "\u274c Cancel" in text, (
        f"final_view_keyboard missing Cancel button: {text[:300]}"
    )


def test_raw_view_text_helper_exists():
    """_raw_view_text helper should be importable."""
    wiz = _import_wizard()
    assert hasattr(wiz, "_raw_view_text"), (
        "_raw_view_text helper missing"
    )


def test_raw_view_text_formats_result():
    """_raw_view_text should JSON-format the raw_result from state."""
    wiz = _import_wizard()
    state = {
        "_final_raw_result": {
            "success": True,
            "message": "Test",
            "data": {
                "exchange_response": {"raw": {"foo": "bar"}},
                "raw_response": {"baz": "qux"},
            },
        }
    }
    text = wiz._raw_view_text(state)
    assert "foo" in text
    assert "bar" in text
    assert "baz" in text
    assert "qux" in text


def test_raw_view_text_handles_missing_state():
    """If _final_raw_result is missing, show a fallback message."""
    wiz = _import_wizard()
    text = wiz._raw_view_text({})
    assert "unavailable" in text.lower()


def test_raw_view_text_truncates_long_payloads():
    """Telegram has a 4096-char limit. _raw_view_text should truncate."""
    wiz = _import_wizard()
    big_payload = {"data": {"x": "a" * 10000}}
    text = wiz._raw_view_text({"_final_raw_result": big_payload})
    assert len(text) <= 4096


def test_screen_for_state_has_final_display_keyboard():
    """_screen_for_state with step='final_display' should attach a keyboard
    (not None)."""
    wiz = _import_wizard()
    state = {
        "workflow": "balance",
        "step": "final_display",
        "data": {"exchange": "raydium", "account": "example"},
        "_final_text": "Sample balance text",
    }
    screen = wiz._screen_for_state(state)
    # Screen has .text and .keyboard attributes.
    assert screen.keyboard is not None, (
        "final_display step has no keyboard; Back/Raw buttons are missing"
    )


def test_screen_for_state_has_raw_view_keyboard():
    """_screen_for_state with step='raw_view' should render the raw JSON
    and attach a keyboard."""
    wiz = _import_wizard()
    state = {
        "workflow": "balance",
        "step": "raw_view",
        "data": {"exchange": "raydium", "account": "example"},
        "_final_raw_result": {
            "success": True,
            "message": "Sample",
            "data": {"exchange_response": {"x": 1}},
        },
    }
    screen = wiz._screen_for_state(state)
    assert screen.keyboard is not None, (
        "raw_view step has no keyboard; Back button is missing"
    )
    assert screen.text, "raw_view text is empty"
    assert "exchange_response" in screen.text, (
        f"raw_view text doesn't contain expected fields: {screen.text[:300]}"
    )


def test_submit_action_captures_raw_result():
    """When the wizard receives action='submit', it should capture the
    raw result in state['_final_raw_result'] (so the Raw button works)
    AND attach a keyboard with Back/Raw/Cancel.
    """
    wiz = _import_wizard()
    # Build a state for balance workflow.
    state = {
        "workflow": "balance",
        "step": "final_display",
        "data": {"exchange": "raydium", "account": "example"},
    }
    # Mock TradeDesk.
    captured = {}
    def fake_execute_request(request):
        captured["request"] = request
        return {
            "success": True,
            "message": "Sample formatted message",
            "data": {
                "exchange_response": {"some_key": "some_value"},
                "raw_response": {"raw": True},
            },
        }
    wiz._execute_trade_request = fake_execute_request
    wiz._render_trade_result = lambda r: r.get("message", "")

    # Mock the query and adapter.
    states = {}
    states["user:1"] = state
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    adapter = MagicMock()
    # Wire _state_map to return our state.
    wiz._state_map = lambda a: states

    # Patch _build_request to return a fake request.
    wiz._build_request = lambda s: {
        "operation": "balance",
        "exchange": "raydium",
        "account": "example",
    }

    import asyncio
    async def run():
        await wiz.handle_trade_callback(adapter, query, "tm:submit")
        # state should have _final_raw_result.
        assert "_final_raw_result" in state, (
            "submit did not store _final_raw_result; Raw button won't work"
        )
        assert state["_final_raw_result"]["success"] is True
        assert state["_final_raw_result"]["data"]["exchange_response"]["some_key"] == "some_value"
        # query should have been edited with a keyboard.
        assert query.edit_message_text.await_count >= 1
        # Check the keyboard was attached.
        call_kwargs = query.edit_message_text.await_args.kwargs
        assert call_kwargs.get("reply_markup") is not None, (
            "submit did not attach reply_markup; Back/Raw buttons missing"
        )
    asyncio.run(run())


def test_raw_action_pushes_raw_view_step():
    """When the wizard receives action='raw', it should push the raw_view
    step onto the state and re-render the screen."""
    wiz = _import_wizard()
    state = {
        "workflow": "balance",
        "step": "final_display",
        "data": {"exchange": "raydium", "account": "example"},
        "_final_raw_result": {
            "success": True,
            "message": "Sample",
            "data": {},
        },
    }
    states = {"user:1": state}
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    adapter = MagicMock()
    wiz._state_map = lambda a: states

    import asyncio
    async def run():
        await wiz.handle_trade_callback(adapter, query, "tm:raw")
        assert state["step"] == "raw_view", (
            f"raw action did not transition to raw_view; step={state['step']}"
        )
        # The history should have a snapshot so Back returns to final_display.
        assert state.get("history"), (
            "raw action did not push history; Back from raw_view is broken"
        )
    asyncio.run(run())