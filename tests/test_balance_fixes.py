"""Regression tests for the Balance menu bugs.

This test covers two bugs:
1. Apex "No module named numpy" — apexomni 3.3.1 has `import numpy as np`
   in http_private_sign.py, so numpy must be in the [main] requirements
   for fresh Kamatera installs to work.
2. AFx "Balance unavailable" — the AFx /info/account/wallet response has
   `data: [{balance, equity, availableBalance, ...}]` (a list with one
   wallet dict). The tradedesk._format_balance_message renderer reads
   `marginSummary.accountValue`, `totalMarginUsed`, `totalNtlPos` plus
   top-level fallbacks. Without normalization, the renderer shows
   "Balance unavailable." for every field.

These bugs were introduced when the public package was first released
and are not bugs in the upstream hermes-agent 0.19.0 itself. The
public package must work on a FRESH Kamatera install where numpy is
not preinstalled and the AFx raw response is what's actually returned.
"""
import re
import sys
import os
import shutil
import tempfile
from pathlib import Path
from decimal import Decimal


PUB = Path(__file__).parent.parent


def test_requirements_has_numpy_in_main_section():
    """apexomni 3.3.1 has a hard `import numpy as np` in
    http_private_sign.py. Without numpy, calling Apex _balance raises
    ModuleNotFoundError. numpy must be a [main] dependency so a fresh
    Kamatera install can run the Apex balance.
    """
    req_text = (PUB / "requirements.txt").read_text()
    # Split at [lighter] section.
    main_section = req_text.split("=====[lighter]")[0]
    # Look for a `numpy<op><version>` line.
    m = re.search(r"^numpy[><=!~]\S+", main_section, re.MULTILINE)
    assert m, (
        "numpy is NOT in the [main] section of requirements.txt. "
        "Apex balance will fail with `No module named 'numpy'` on a "
        "fresh Kamatera install because apexomni 3.3.1 does "
        "`import numpy as np` in http_private_sign.py."
    )
    # Verify the version is reasonable.
    constraint = m.group(0)
    assert ">=" in constraint or "==" in constraint, (
        f"numpy constraint {constraint!r} is not a proper version pin"
    )


def test_requirements_main_section_excludes_lighter():
    """The lighter-sdk entry must be in [lighter] section, NOT [main]."""
    req_text = (PUB / "requirements.txt").read_text()
    main_section = req_text.split("=====[lighter]")[0]
    # lighter-sdk should NOT appear as a normal package pin in [main].
    assert not re.search(r"^lighter-sdk[><=!~]", main_section, re.MULTILINE), (
        "lighter-sdk must be in the [lighter] section, not the [main] section"
    )


def test_afx_normalize_afx_balance_produces_margin_summary():
    """AFx _balance must produce a marginSummary block that the
    tradedesk._format_balance_message renderer can use to display
    Account Value, Withdrawable, Margin Used, and Total Position Value.

    Without this normalization, the AFx balance shows
    "Balance unavailable." for every field.
    """
    afx_path = PUB / "tradedesk" / "afx_agent.py"
    text = afx_path.read_text()

    # Verify _normalize_afx_balance helper exists and is called by _balance.
    assert "_normalize_afx_balance" in text, (
        "afx_agent.py is missing the _normalize_afx_balance helper. "
        "This causes the AFx balance menu to show 'Balance unavailable.'"
    )
    balance_match = re.search(
        r"def _balance\(self.*?(?=\n    def |\nclass |\Z)",
        text, re.DOTALL,
    )
    assert balance_match, "Could not find _balance method"
    assert "_normalize_afx_balance" in balance_match.group(0), (
        "_balance does NOT call _normalize_afx_balance(raw). "
        "The AFx raw response is not normalized for the renderer."
    )


def test_afx_normalize_afx_balance_values():
    """Verify the _normalize_afx_balance helper produces correct values
    from a real AFx response shape.
    """
    TMP = Path(tempfile.mkdtemp())
    try:
        # Copy tradedesk/ to a fresh temp dir so the import is clean
        # (no stale pycache from the working tree).
        src = PUB / "tradedesk"
        dst = TMP / "tradedesk"
        shutil.copytree(src, dst)
        sys.path.insert(0, str(TMP))
        os.environ["PYTHONPATH"] = "/usr/local/lib/hermes-agent"

        from tradedesk.afx_agent import AfxAgent

        # A real AFx /info/account/wallet response shape.
        raw = {
            "code": 0,
            "message": "success",
            "data": [{
                "blockHeight": 61819041,
                "userAddr": "0xab6d1588bc0f46adea05906d32297ea47a737d7c",
                "currency": 1,
                "balance": 15775.860760086478,
                "availableBalance": 12874.995194924479,
                "availableTransferBalance": 0.0,
                "equity": 15859.300760086478,
                "status": "NORMAL",
                "blockTime": 1785067200000,
            }],
        }
        norm = AfxAgent._normalize_afx_balance(raw)

        # Top-level fallbacks.
        assert norm.get("accountValue") == 15859.300760086478, (
            f"accountValue should be 15859.30, got {norm.get('accountValue')}"
        )
        assert norm.get("withdrawable") == 12874.995194924479, (
            f"withdrawable should be 12874.99, got {norm.get('withdrawable')}"
        )
        assert norm.get("totalPositionValue") is not None, (
            "totalPositionValue should be populated"
        )
        # Standard marginSummary block.
        ms = norm.get("marginSummary", {})
        assert ms.get("accountValue") == 15859.300760086478
        assert ms.get("totalMarginUsed") is not None
        assert ms.get("totalNtlPos") is not None
        # Original code/message preserved.
        assert norm.get("code") == 0
        assert norm.get("message") == "success"
    finally:
        sys.path.remove(str(TMP))
        shutil.rmtree(TMP, ignore_errors=True)


def test_afx_normalize_afx_balance_handles_empty_data():
    """Edge case: empty data list."""
    TMP = Path(tempfile.mkdtemp())
    try:
        src = PUB / "tradedesk"
        dst = TMP / "tradedesk"
        shutil.copytree(src, dst)
        sys.path.insert(0, str(TMP))
        os.environ["PYTHONPATH"] = "/usr/local/lib/hermes-agent"
        from tradedesk.afx_agent import AfxAgent
        norm = AfxAgent._normalize_afx_balance({"code": 0, "data": []})
        assert norm.get("code") == 0
        # No accountValue populated.
        assert norm.get("accountValue") is None
    finally:
        sys.path.remove(str(TMP))
        shutil.rmtree(TMP, ignore_errors=True)


def test_afx_normalize_afx_balance_handles_missing_data():
    """Edge case: missing data field."""
    TMP = Path(tempfile.mkdtemp())
    try:
        src = PUB / "tradedesk"
        dst = TMP / "tradedesk"
        shutil.copytree(src, dst)
        sys.path.insert(0, str(TMP))
        os.environ["PYTHONPATH"] = "/usr/local/lib/hermes-agent"
        from tradedesk.afx_agent import AfxAgent
        norm = AfxAgent._normalize_afx_balance({"code": 0})
        assert norm.get("code") == 0
        assert norm.get("accountValue") is None
    finally:
        sys.path.remove(str(TMP))
        shutil.rmtree(TMP, ignore_errors=True)


def test_afx_normalize_provides_all_renderer_fallbacks():
    """The normalized response must provide every field the renderer
    looks for as a fallback, so 'Balance unavailable.' never appears for
    AFx.
    """
    TMP = Path(tempfile.mkdtemp())
    try:
        src = PUB / "tradedesk"
        dst = TMP / "tradedesk"
        shutil.copytree(src, dst)
        sys.path.insert(0, str(TMP))
        os.environ["PYTHONPATH"] = "/usr/local/lib/hermes-agent"
        from tradedesk.afx_agent import AfxAgent
        raw = {
            "code": 0,
            "data": [{
                "balance": 100.0,
                "availableBalance": 80.0,
                "equity": 120.0,
            }],
        }
        norm = AfxAgent._normalize_afx_balance(raw)

        def _first_value(*values):
            for v in values:
                if v is None: continue
                if isinstance(v, str) and not v.strip(): continue
                return v
            return None

        # Mimic the renderer's _format_balance_message fallback chain.
        balance_dict = norm.get("balance", {})
        margin_summary = norm.get("marginSummary", {})

        account_value = _first_value(
            balance_dict.get("account_value"),
            balance_dict.get("account_equity"),
            margin_summary.get("accountValue"),
            norm.get("account_value"),
            norm.get("accountValue"),
        )
        assert account_value == 120.0

        withdrawable = _first_value(
            balance_dict.get("withdrawable"),
            balance_dict.get("available_to_withdraw"),
            norm.get("withdrawable"),
            norm.get("withdrawableUsd"),
            norm.get("availableToWithdraw"),
        )
        assert withdrawable == 80.0

        margin_used = _first_value(
            balance_dict.get("margin_used"),
            balance_dict.get("total_margin_used"),
            margin_summary.get("totalMarginUsed"),
            norm.get("margin_used"),
        )
        # margin_used = equity - availableBalance = 120 - 80 = 40
        assert margin_used == 40.0

        total_position_value = _first_value(
            balance_dict.get("total_position_value"),
            margin_summary.get("totalNtlPos"),
            norm.get("total_position_value"),
            norm.get("totalPositionValue"),
        )
        assert total_position_value == 40.0
    finally:
        sys.path.remove(str(TMP))
        shutil.rmtree(TMP, ignore_errors=True)
