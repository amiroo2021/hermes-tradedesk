#!/usr/bin/env bash
# verify.sh — Hermes TradeDesk offline verification
# -----------------------------------------------------------------------------
# Runs a series of checks against an installed hermes-tradedesk. It:
#   - NEVER places a real or test order.
#   - NEVER cancels a real or test order.
#   - NEVER prints secret values.
#   - Runs locally and reports PASS/FAIL.
#
# Use after install.sh to confirm the install is good without contacting
# any exchange API.
# -----------------------------------------------------------------------------
set -uo pipefail

# Locate self.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TR="$SCRIPT_DIR/tradedesk"
WIZARD="$SCRIPT_DIR/hermes_overlay/telegram/trade_menu"

# To import the bundled tradedesk as a package we need its PARENT
# directory on PYTHONPATH (not the tradedesk/ directory itself).
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
FAILS=0

HERMES_PY="${HERMES_HOME:-/usr/local/lib/hermes-agent}/venv/bin/python"
if [[ ! -x "$HERMES_PY" ]]; then
    HERMES_PY="$(command -v python3 || echo python3)"
fi

echo
echo "HERMES TRADEDESK VERIFICATION"
echo "============================================================"

# ---------------------------------------------------------------------------
# 1. Files installed
# ---------------------------------------------------------------------------
echo
echo "Module inventory:"
if [[ -d "$TR" ]]; then
    for f in "$TR"/*.py; do
        bn="$(basename "$f")"
        [[ "$bn" == "__init__.py" ]] && continue
        pass "  tradedesk/$bn"
    done
else
    fail "  tradedesk/ directory missing"
fi
if [[ -d "$WIZARD" ]]; then
    for f in "$WIZARD"/*.py; do
        pass "  hermes_overlay/.../trade_menu/$(basename "$f")"
    done
else
    fail "  hermes_overlay/.../trade_menu/ directory missing"
fi

# ---------------------------------------------------------------------------
# 2. Imports
# ---------------------------------------------------------------------------
echo
echo "Import check:"
export HERMES_TRADEDESK_TEST_PARENT="$PARENT_DIR"
export HERMES_TRADEDESK_TEST_WIZ="$WIZARD"
"$HERMES_PY" <<'PY'
import os, sys, importlib
sys.path.insert(0, os.environ["HERMES_TRADEDESK_TEST_PARENT"])
sys.path.insert(0, os.environ["HERMES_TRADEDESK_TEST_WIZ"])
modules = [
    "tradedesk.router", "tradedesk.tradedesk", "tradedesk.account_discovery",
    "tradedesk.request_utils", "tradedesk.hyperliquid_agent",
    "tradedesk.lighter_agent", "tradedesk.raydium_agent",
    "tradedesk.raydium_write", "tradedesk.pacifica_agent",
    "tradedesk.pacifica_tpsl", "tradedesk.afx_agent",
    "tradedesk.apex_agent", "tradedesk.rise_agent",
    "wizard",
]
ok = True
for m in modules:
    try:
        importlib.import_module(m)
        print(f"PASS  import {m}")
    except Exception as e:
        ok = False
        print(f"FAIL  import {m}: {e}")
sys.exit(0 if ok else 1)
PY
RC=$?
unset HERMES_TRADEDESK_TEST_PARENT HERMES_TRADEDESK_TEST_WIZ
if [[ $RC -ne 0 ]]; then
    FAILS=$((FAILS+1))
fi

# ---------------------------------------------------------------------------
# 3. .env.example sanity (no real credentials in repo)
# ---------------------------------------------------------------------------
echo
echo ".env.example sanity check:"
EXAMPLE="$SCRIPT_DIR/.env.example"
if [[ -f "$EXAMPLE" ]]; then
    empty_count=$(grep -E "^[A-Z_]+=" "$EXAMPLE" 2>/dev/null | grep -v "=$" | wc -l)
    if [[ "$empty_count" -eq 0 ]]; then
        pass "  .env.example contains only empty values"
    else
        fail "  .env.example contains $empty_count non-empty value lines"
    fi
else
    fail "  .env.example missing"
fi

# ---------------------------------------------------------------------------
# 4. Trading writes performed: 0
# ---------------------------------------------------------------------------
echo
echo "Trading writes performed: 0"
echo "(verify.sh is read-only — never places or cancels orders)"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "============================================================"
if [[ $FAILS -eq 0 ]]; then
    echo "VERIFICATION PASS"
    exit 0
else
    echo "VERIFICATION FAIL ($FAILS)"
    exit 1
fi
