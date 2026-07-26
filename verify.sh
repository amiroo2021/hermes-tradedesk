#!/usr/bin/env bash
# verify.sh — Hermes TradeDesk offline verification
# -----------------------------------------------------------------------------
# Runs a series of checks against the bundled package at $SCRIPT_DIR.
#
#   - NEVER places a real or test order.
#   - NEVER cancels a real or test order.
#   - NEVER prints secret values.
#   - Runs locally and reports PASS/FAIL.
#
# Use after install.sh to confirm the install is good without contacting
# any exchange API.
#
# This script is for the PUBLIC PACKAGE — it does NOT assume the install
# has already been run on the target machine. It verifies that the package
# itself is structurally complete and importable.
# -----------------------------------------------------------------------------
set -uo pipefail

# Locate self.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
FAILS=0

# To import the bundled tradedesk as a package we need its PARENT
# directory on PYTHONPATH (not the package itself).
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

HERMES_PY="${HERMES_PY:-/usr/local/lib/hermes-agent/venv/bin/python}"
if [[ ! -x "$HERMES_PY" ]]; then
    HERMES_PY="$(command -v python3 || echo python3)"
fi

echo
echo "HERMES TRADEDESK VERIFICATION"
echo "============================================================"

# ---------------------------------------------------------------------------
# 1. File inventory
# ---------------------------------------------------------------------------
echo
echo "Module inventory:"
if [[ -d "$SCRIPT_DIR/tradedesk" ]]; then
    for f in "$SCRIPT_DIR/tradedesk"/*.py; do
        bn="$(basename "$f")"
        [[ "$bn" == "__init__.py" ]] && continue
        pass "  tradedesk/$bn"
    done
else
    fail "  tradedesk/ directory missing"
fi
if [[ -d "$SCRIPT_DIR/hermes_overlay/telegram" ]]; then
    for f in "$SCRIPT_DIR/hermes_overlay/telegram"/*.py; do
        pass "  hermes_overlay/.../telegram/$(basename "$f")"
    done
    for f in "$SCRIPT_DIR/hermes_overlay/telegram/trade_menu"/*.py; do
        pass "  hermes_overlay/.../telegram/trade_menu/$(basename "$f")"
    done
else
    fail "  hermes_overlay/.../telegram/ directory missing"
fi

# ---------------------------------------------------------------------------
# 2. Required Telegram plugin integration files exist
# ---------------------------------------------------------------------------
echo
echo "Telegram integration files:"
for f in shared_selectors.py _positions_render.py; do
    if [[ -f "$SCRIPT_DIR/hermes_overlay/telegram/$f" ]]; then
        pass "  hermes_overlay/telegram/$f"
    else
        fail "  hermes_overlay/telegram/$f missing"
    fi
done

# ---------------------------------------------------------------------------
# 3. Import check (uses the bundled source via PYTHONPATH)
# ---------------------------------------------------------------------------
echo
echo "Import check:"
export HERMES_TRADEDESK_TEST_PARENT="$PARENT_DIR"
# The wizard module is at hermes_overlay/telegram/trade_menu/wizard.py.
# We add both the telegram/ root and the trade_menu/ directory so the
# full set of overlay modules (wizard, shared_selectors, _positions_render)
# can be imported as top-level modules.
export HERMES_TRADEDESK_TEST_TG="$SCRIPT_DIR/hermes_overlay/telegram"
export HERMES_TRADEDESK_TEST_TRADE_MENU="$SCRIPT_DIR/hermes_overlay/telegram/trade_menu"
"$HERMES_PY" <<'PY'
import os, sys, importlib
sys.path.insert(0, os.environ["HERMES_TRADEDESK_TEST_PARENT"])
sys.path.insert(0, os.environ["HERMES_TRADEDESK_TEST_TG"])
sys.path.insert(0, os.environ["HERMES_TRADEDESK_TEST_TRADE_MENU"])
modules = [
    "tradedesk.router",
    "tradedesk.tradedesk",
    "tradedesk.account_discovery",
    "tradedesk.request_utils",
    "tradedesk.hyperliquid_agent",
    "tradedesk.lighter_agent",
    "tradedesk.raydium_agent",
    "tradedesk.raydium_write",
    "tradedesk.pacifica_agent",
    "tradedesk.pacifica_tpsl",
    "tradedesk.afx_agent",
    "tradedesk.apex_agent",
    "tradedesk.rise_agent",
    "wizard",
    "shared_selectors",
    "_positions_render",
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
unset HERMES_TRADEDESK_TEST_PARENT HERMES_TRADEDESK_TEST_TG HERMES_TRADEDESK_TEST_TRADE_MENU
if [[ $RC -ne 0 ]]; then
    FAILS=$((FAILS+1))
fi

# ---------------------------------------------------------------------------
# 4. .env.example sanity
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
# 5. No credential / secret files in package
# ---------------------------------------------------------------------------
echo
echo "Forbidden-file check:"
forbidden=(".env" "auth.json")
for f in "${forbidden[@]}"; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        fail "  $f present in package"
    fi
done
if ! [[ -f "$SCRIPT_DIR/.env" ]] && ! [[ -f "$SCRIPT_DIR/auth.json" ]]; then
    pass "  no .env / auth.json in package root"
fi

# ---------------------------------------------------------------------------
# 6. requirements.txt sanity
# ---------------------------------------------------------------------------
echo
echo "requirements.txt sanity check:"
if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
    pass "  requirements.txt present"
    n=$(grep -cE "^[a-zA-Z0-9_-]+>=" "$SCRIPT_DIR/requirements.txt" 2>/dev/null || echo 0)
    echo "  declared packages: $n"
else
    fail "  requirements.txt missing"
fi

# ---------------------------------------------------------------------------
# 7. Production-only personal aliases absent from public tree
# ---------------------------------------------------------------------------
echo
echo "Privacy check (no production-only personal aliases):"
# Production-only personal aliases that should NEVER appear in the public tree.
# (We use generic placeholders like 'EXAMPLE', 'ACCOUNT1', etc. in the public
# tree; the production names must not leak.)
ALIASES=("amiroo" "fibo" "flex" "metamask" "bitget" "dramiroo" "delta" "phantom")
ALIAS_HITS=0
# We intentionally exclude 'based' from this list because the word 'based'
# is a common English word used in technical documentation
# (e.g. "0-based indexing", "transaction-based verification"). The
# production operator's 'based' account identifier is checked separately
# below as a string-literal token.
for alias in "${ALIASES[@]}"; do
    while IFS= read -r f; do
        # Skip verify.sh itself (it contains the list of names we are checking).
        if [[ "$f" == *"verify.sh" ]]; then
            continue
        fi
        fail "  production alias '$alias' present in $f"
        ALIAS_HITS=$((ALIAS_HITS + 1))
    done < <(grep -lE "(^|[^A-Za-z0-9_])${alias}([^A-Za-z0-9_]|$)" \
        -r --include="*.py" --include="*.md" --include="*.sh" --include="*.example" --include="*.json" \
        "$SCRIPT_DIR" 2>/dev/null | grep -v __pycache__ || true)
done
# Special check for 'based' as a string-literal account identifier.
# We look only for the patterns '"account": "based"', "account: 'based'",
# or env-var-like tokens like RISE_BASED, RAYDIUM_BASED, etc.
while IFS= read -r f; do
    if [[ "$f" == *"verify.sh" ]]; then
        continue
    fi
    fail "  production account identifier 'based' present in $f"
    ALIAS_HITS=$((ALIAS_HITS + 1))
done < <(grep -lE '("account"\s*:\s*"based"|"account":\s*\\"based\\"|RISE_BASED|RAYDIUM_BASED|AFX_BASED|PACIFICA_BASED|LIGHTER_BASED|HYPERLIQUID_BASED|APEX_BASED|account:[[:space:]]*[\x27"]based[\x27"])' \
    -r --include="*.py" --include="*.md" --include="*.sh" --include="*.example" --include="*.json" \
    "$SCRIPT_DIR" 2>/dev/null | grep -v __pycache__ || true)
if [[ "$ALIAS_HITS" -eq 0 ]]; then
    pass "  no production-only personal aliases in package"
fi

# ---------------------------------------------------------------------------
# 8. Trading writes performed: 0
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
