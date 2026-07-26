#!/usr/bin/env bash
# test_clean_base_install.sh — Regression test for clean-base Hermes install.
#
# This test simulates a GENUINELY CLEAN base Hermes (Kamatera-style):
#   - No tradedesk/ directory
#   - No shared_selectors.py
#   - No _positions_render.py
#   - No TradeDesk-specific exchange dependencies initially installed
#
# The test creates a CLEAN fake-Hermes tree under /tmp, then:
#   1. Calls install.sh with HERMES_HOME pointed at the fake tree
#   2. install.sh runs its 11 phases
#   3. We verify post-install that all expected files are present
#   4. We verify post-install that imports succeed
#
# To avoid network during pip install, we pre-seed the production hermes
# venv (which already has the required packages) and use it via a
# symlink-wrapper Python.
# -----------------------------------------------------------------------------
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUB_DIR="$(dirname "$SCRIPT_DIR")"

# Locate install.sh and requirements.txt
INSTALL_SH="$PUB_DIR/install.sh"
REQUIREMENTS="$PUB_DIR/requirements.txt"

if [[ ! -f "$INSTALL_SH" ]]; then
    echo "FAIL  install.sh not found at $INSTALL_SH"
    exit 1
fi
if [[ ! -f "$REQUIREMENTS" ]]; then
    echo "FAIL  requirements.txt not found at $REQUIREMENTS"
    exit 1
fi

# Build a CLEAN base-Hermes tree (Kamatera-style).
CLEAN_FAKE="/tmp/test-clean-base-$$"
mkdir -p "$CLEAN_FAKE"

# We need the python wrapper to resolve requirements.txt and manifest.json
# from the PUBLIC PACKAGE (since the test's $CLEAN_FAKE doesn't have them).
# Solution: pre-copy requirements.txt and manifest.json to $CLEAN_FAKE so
# install.sh finds them at $SCRIPT_DIR/requirements.txt.
cp "$REQUIREMENTS" "$CLEAN_FAKE/requirements.txt"
cp "$PUB_DIR/manifest.json" "$CLEAN_FAKE/manifest.json"

hermes_py="/usr/local/lib/hermes-agent/venv/bin/python"
mkdir -p "$CLEAN_FAKE/venv/bin"
cat > "$CLEAN_FAKE/venv/bin/python" <<EOF
#!/usr/bin/env bash
exec $hermes_py "\$@"
EOF
chmod +x "$CLEAN_FAKE/venv/bin/python"

# Base Hermes structure (no TradeDesk, no shared_selectors, no _positions_render).
mkdir -p "$CLEAN_FAKE/plugins"
echo "" > "$CLEAN_FAKE/plugins/__init__.py"
mkdir -p "$CLEAN_FAKE/plugins/platforms"
echo "" > "$CLEAN_FAKE/plugins/platforms/__init__.py"
mkdir -p "$CLEAN_FAKE/plugins/platforms/telegram"
echo "" > "$CLEAN_FAKE/plugins/platforms/telegram/__init__.py"

# Fake hermes_cli package.
mkdir -p "$CLEAN_FAKE/hermes_cli"
cat > "$CLEAN_FAKE/hermes_cli/__init__.py" <<EOF
__version__ = "test-clean-base"
EOF

# Fake hermes binary.
mkdir -p "$CLEAN_FAKE/bin"
cat > "$CLEAN_FAKE/bin/hermes" <<EOF
#!/usr/bin/env bash
echo "fake hermes"
EOF
chmod +x "$CLEAN_FAKE/bin/hermes"

# Copy install.sh to CLEAN_FAKE (so SCRIPT_DIR resolves there and finds
# requirements.txt/manifest.json).
cp "$INSTALL_SH" "$CLEAN_FAKE/install.sh"
chmod +x "$CLEAN_FAKE/install.sh"

# Run install.sh.  install.sh uses $SCRIPT_DIR for requirements.txt and
# manifest.json, which now resolves to $CLEAN_FAKE. The source files for
# tradedesk/ and hermes_overlay/ come from $PUB_DIR via HERMES_TRADESK_SRC_ROOT.
echo "=== Running install.sh against clean base-Hermes fixture ==="
echo "  CLEAN_FAKE=$CLEAN_FAKE"
echo "  PUB_DIR=$PUB_DIR"
echo

HERMES_PY="$CLEAN_FAKE/venv/bin/python" \
HERMES_TRADESK_SKIP_STRUCT_CHECK=0 \
HERMES_TRADESK_SRC_ROOT="$PUB_DIR" \
HERMES_TRADESK_HERMES_HOME="$CLEAN_FAKE" \
HERMES_TRADESK_HERMES_BIN="$CLEAN_FAKE/bin/hermes" \
HOME="$CLEAN_FAKE/fake-home" \
PATH="/usr/bin:/bin" \
PYTHONPATH="$CLEAN_FAKE" \
bash "$CLEAN_FAKE/install.sh"
rc=$?
echo
echo "=== install.sh exit: $rc ==="
echo

# Verify the install actually placed files.
echo "=== Post-install: check that tradedesk/ was populated ==="
EXPECTED_FILES=(
    "tradedesk/__init__.py"
    "tradedesk/router.py"
    "tradedesk/tradedesk.py"
    "tradedesk/account_discovery.py"
    "tradedesk/request_utils.py"
    "tradedesk/hyperliquid_agent.py"
    "tradedesk/lighter_agent.py"
    "tradedesk/raydium_agent.py"
    "tradedesk/raydium_write.py"
    "tradedesk/pacifica_agent.py"
    "tradedesk/pacifica_tpsl.py"
    "tradedesk/afx_agent.py"
    "tradedesk/apex_agent.py"
    "tradedesk/rise_agent.py"
    "plugins/platforms/telegram/shared_selectors.py"
    "plugins/platforms/telegram/_positions_render.py"
    "plugins/platforms/telegram/trade_menu/__init__.py"
    "plugins/platforms/telegram/trade_menu/wizard.py"
)
ALL_OK=1
for f in "${EXPECTED_FILES[@]}"; do
    if [[ -f "$CLEAN_FAKE/$f" ]]; then
        echo "  OK    $f"
    else
        echo "  FAIL  $f missing after install"
        ALL_OK=0
    fi
done
echo

# Run the post-install integration import check.
echo "=== Post-install: integration import check ==="
PYTHONPATH="$CLEAN_FAKE" "$hermes_py" -c "
import importlib
mods = [
    'tradedesk.tradedesk',
    'tradedesk.lighter_agent',
    'tradedesk.raydium_agent',
    'tradedesk.account_discovery',
    'plugins.platforms.telegram.shared_selectors',
    'plugins.platforms.telegram._positions_render',
    'plugins.platforms.telegram.trade_menu.wizard',
    'plugins.platforms.telegram.trade_menu',
]
ok = 0
for m in mods:
    try:
        importlib.import_module(m)
        print(f'  OK    import {m}')
        ok += 1
    except Exception as e:
        print(f'  FAIL  import {m}: {e}')
import sys
sys.exit(0 if ok == len(mods) else 1)
"
import_rc=$?
echo
echo "  Post-install import check exit: $import_rc"

# Final verdict.
echo
if [[ $rc -eq 0 && $ALL_OK -eq 1 && $import_rc -eq 0 ]]; then
    echo "=== REGRESSION TEST RESULT: PASS ==="
    echo "  install.sh exit: $rc"
    echo "  all expected files present: yes"
    echo "  post-install imports: all OK"
    OVERALL=0
else
    echo "=== REGRESSION TEST RESULT: FAIL ==="
    echo "  install.sh exit: $rc"
    echo "  all expected files present: $ALL_OK"
    echo "  post-install import check exit: $import_rc"
    OVERALL=1
fi

# Cleanup.
rm -rf "$CLEAN_FAKE"
exit $OVERALL
