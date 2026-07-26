"""Regression tests for the "positions in Balance menu" bug.

This covers the bug where the Balance menu for AFX, Lighter, and
Raydium did not show open positions + PnL even when the exchange
returned positions correctly. The fix wraps each exchange's
normalized position into a Hyperliquid-style envelope
(``{"position": {coin, szi, entryPx, unrealizedPnl, liquidationPx}}``)
so the existing exchange-agnostic ``_format_balance_message`` renderer
can display them.

The constraint is: tradedesk.py and the telegram /trade wizard remain
exchange-agnostic. All exchange-specific logic stays in the agent.
"""
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path


PUB = Path(__file__).parent.parent


def _extract_balance_method(text, marker="_balance"):
    pattern = (
        r"def " + marker + r"(self.*?(?=\n    def |\nclass |\Z))"
    )
    match = re.search(pattern, text, re.DOTALL)
    return match.group(0) if match else ""


def test_afx_balance_includes_positions():
    """AFx _balance should include positions in its exchange_response."""
    afx_path = PUB / "tradedesk" / "afx_agent.py"
    text = afx_path.read_text()
    body = _extract_balance_method(text)
    assert body, "Could not find AfxAgent._balance"
    assert (
        "_fetch_positions_for_balance" in body
        or "info_client.get_positions" in body
        or "/info/position/list" in body
    ), "AFx _balance does not fetch positions"
    assert (
        'exchange_response["positions"] = positions_wrapped' in body
        or "exchange_response['positions'] = positions_wrapped" in body
    ), "AFx _balance does not write positions into exchange_response"


def test_afx_wrap_position_helper_exists():
    """AFx _wrap_afx_position_for_balance should produce Hyperliquid-style
    envelope."""
    afx_path = PUB / "tradedesk" / "afx_agent.py"
    text = afx_path.read_text()
    assert "_wrap_afx_position_for_balance" in text, (
        "Missing _wrap_afx_position_for_balance helper"
    )


def test_raydium_balance_includes_positions():
    """Raydium _balance should include positions in its exchange_response."""
    ray_path = PUB / "tradedesk" / "raydium_agent.py"
    text = ray_path.read_text()
    body = _extract_balance_method(text)
    assert body, "Could not find RaydiumAgent._balance"
    assert "_fetch_positions_for_balance" in body, (
        "Raydium _balance does not call _fetch_positions_for_balance"
    )


def test_raydium_wrap_position_helper_exists():
    ray_path = PUB / "tradedesk" / "raydium_agent.py"
    text = ray_path.read_text()
    assert "_wrap_raydium_position_for_balance" in text, (
        "Missing _wrap_raydium_position_for_balance helper"
    )


def test_pacifica_balance_includes_positions():
    """Pacifica _balance should include positions in its exchange_response."""
    pac_path = PUB / "tradedesk" / "pacifica_agent.py"
    text = pac_path.read_text()
    body = _extract_balance_method(text)
    assert body, "Could not find PacificaAgent._balance"
    assert "_fetch_positions_for_balance" in body, (
        "Pacifica _balance does not call _fetch_positions_for_balance"
    )


def test_pacifica_wrap_position_helper_exists():
    pac_path = PUB / "tradedesk" / "pacifica_agent.py"
    text = pac_path.read_text()
    assert "_wrap_pacifica_position_for_balance" in text, (
        "Missing _wrap_pacifica_position_for_balance helper"
    )


def test_lighter_balance_uses_normalizer():
    """Lighter _balance should call _hermes_normalize_lighter_positions
    before wrapping so the wire-format keys get mapped to canonical
    Hermes keys.
    """
    lighter_path = PUB / "tradedesk" / "lighter_agent.py"
    text = lighter_path.read_text()
    body = _extract_balance_method(text)
    assert body, "Could not find LighterAgent._balance"
    assert "_hermes_normalize_lighter_positions" in body, (
        "Lighter _balance does not call _hermes_normalize_lighter_positions "
        "before wrapping positions"
    )


def test_afx_wrap_functionality():
    """Functional test for the AFX wrap helper."""
    sys.path.insert(0, str(PUB))
    os.environ["PYTHONPATH"] = "/usr/local/lib/hermes-agent"
    from tradedesk.afx_agent import AfxAgent
    pos = {
        "symbol": "BTCUSDC",
        "side": "long",
        "size": 0.61,
        "entry_price": 64289.0,
        "unrealized_pnl": 83.44,
        "liquidation_price": None,
    }
    wrapped = AfxAgent._wrap_afx_position_for_balance(pos)
    assert wrapped is not None
    p = wrapped["position"]
    assert p["coin"] == "BTCUSDC"
    assert p["szi"] == "0.61"
    assert p["entryPx"] == 64289.0
    assert p["unrealizedPnl"] == 83.44
    pos_short = dict(pos)
    pos_short["side"] = "short"
    pos_short["size"] = 0.5
    wrapped_short = AfxAgent._wrap_afx_position_for_balance(pos_short)
    assert wrapped_short["position"]["szi"] == "-0.5"
    pos_zero = dict(pos)
    pos_zero["size"] = 0
    assert AfxAgent._wrap_afx_position_for_balance(pos_zero) is None


def test_raydium_wrap_functionality():
    """Functional test for the Raydium wrap helper."""
    sys.path.insert(0, str(PUB))
    os.environ["PYTHONPATH"] = "/usr/local/lib/hermes-agent"
    from tradedesk.raydium_agent import _wrap_raydium_position_for_balance
    pos = {
        "symbol": "PERP_BTC_USDC",
        "side": "long",
        "size": 2.37596,
        "entry_price": 64096.38,
        "unrealized_pnl": 680.70,
        "liq_price": 59160.47,
    }
    wrapped = _wrap_raydium_position_for_balance(pos)
    assert wrapped is not None
    p = wrapped["position"]
    assert p["coin"] == "PERP_BTC_USDC"
    assert p["szi"] == "2.37596"
    assert p["entryPx"] == 64096.38
    assert p["unrealizedPnl"] == 680.70
    assert p["liquidationPx"] == 59160.47


def test_pacifica_wrap_functionality():
    """Functional test for the Pacifica wrap helper."""
    sys.path.insert(0, str(PUB))
    os.environ["PYTHONPATH"] = "/usr/local/lib/hermes-agent"
    from tradedesk.pacifica_agent import PacificaAgent
    pos = {
        "symbol": "BTC",
        "side": "long",
        "size": 0.01,
        "entry_price": 64000.0,
        "unrealized_pnl": 100.0,
        "liquidation_price": 50000.0,
    }
    wrapped = PacificaAgent._wrap_pacifica_position_for_balance(pos)
    assert wrapped is not None
    p = wrapped["position"]
    assert p["coin"] == "BTC"
    assert p["szi"] == "0.01"
    assert p["entryPx"] == 64000.0
    assert p["unrealizedPnl"] == 100.0
    assert p["liquidationPx"] == 50000.0


def test_renderer_no_exchange_branches_added():
    """The exchange-agnostic renderer must NOT have new exchange-specific
    branches added for positions.
    """
    pub_tradedesk = (PUB / "tradedesk" / "tradedesk.py").read_text()
    forbidden = [
        "is_afx = ",
        "is_lighter = ",
        "is_raydium = ",
    ]
    for pattern in forbidden:
        assert pattern not in pub_tradedesk, (
            f"Renderer has new exchange-specific branch: {pattern!r}. "
            "Only exchange agents should wrap positions."
        )