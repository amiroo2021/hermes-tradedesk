#!/usr/bin/env bash
# test_clean_base_install.sh — Regression test for clean-base Hermes install.
#
# This test simulates a Kamatera-faithful starting state, runs the
# install.sh, and verifies:
#   - install.sh exit 0
#   - All 14 tradedesk modules + 4 telegram overlay files are present
#   - 4 __init__.py marker files at plugins/, plugins/platforms/,
#     plugins/platforms/telegram/, plugins/platforms/telegram/trade_menu/
#   - 6 always-required post-install imports succeed
#   - urllib3 stays at 2.7.0 (NO downgrade)
#   - python -m pip check returns clean
#
# The test uses a Kamatera-faithful venv (hermes-agent 0.19.0, urllib3 2.7.0,
# no TradeDesk deps, no lighter-sdk) — NOT the production DigitalOcean
# venv, which would have lighter-sdk pre-installed and a stale urllib3.
# -----------------------------------------------------------------------------
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUB_DIR="$(dirname "$SCRIPT_DIR")"

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

# Build a Kamatera-faithful venv if not already present.
KAMATERA_VENV="/tmp/kamatera-faithful-venv-clean-test"
if [[ ! -f "$KAMATERA_VENV/bin/python" ]]; then
    echo "=== Building Kamatera-faithful venv (one-time setup) ==="
    rm -rf "$KAMATERA_VENV"
    mkdir -p "$KAMATERA_VENV"
    /usr/local/lib/hermes-agent/venv/bin/python -m venv "$KAMATERA_VENV" 2>&1 | head -5
    "$KAMATERA_VENV/bin/python" -m pip install --quiet --upgrade pip
    "$KAMATERA_VENV/bin/python" -m pip install --quiet "hermes-agent==0.19.0"
    echo "  installed hermes-agent 0.19.0"
fi

# Build a CLEAN base-Hermes tree mirroring the actual Kamatera layout:
# - NO __init__.py files under plugins/ initially (hermes-agent 0.19.0's
#   site-packages has them, but the user's home dir does NOT).
# - Just the bare directory tree.
CLEAN_FAKE="/tmp/test-clean-base-$$"
mkdir -p "$CLEAN_FAKE"

# install.sh looks for requirements.txt and manifest.json at $SCRIPT_DIR.
# Pre-copy them to $CLEAN_FAKE so install.sh finds them.
cp "$REQUIREMENTS" "$CLEAN_FAKE/requirements.txt"
cp "$PUB_DIR/manifest.json" "$CLEAN_FAKE/manifest.json"

hermes_py="$KAMATERA_VENV/bin/python"
mkdir -p "$CLEAN_FAKE/venv/bin"
cat > "$CLEAN_FAKE/venv/bin/python" <<EOF
#!/usr/bin/env bash
exec $hermes_py "\$@"
EOF
chmod +x "$CLEAN_FAKE/venv/bin/python"

# Bare directory tree (no __init__.py anywhere in plugins/).
mkdir -p "$CLEAN_FAKE/plugins/platforms/telegram"

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

# Copy install.sh to CLEAN_FAKE so $SCRIPT_DIR resolves there.
cp "$INSTALL_SH" "$CLEAN_FAKE/install.sh"
chmod +x "$CLEAN_FAKE/install.sh"

# Run install.sh.
echo "=== Running install.sh against Kamatera-faithful clean-base Hermes ==="
echo "  CLEAN_FAKE=$CLEAN_FAKE"
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

# Verify post-install state.
echo "=== Post-install: check files ==="
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
    # __init__.py markers
    "plugins/__init__.py"
    "plugins/platforms/__init__.py"
    "plugins/platforms/telegram/__init__.py"
)
ALL_OK=1
for f in "${EXPECTED_FILES[@]}"; do
    if [[ -f "$CLEAN_FAKE/$f" ]]; then
        echo "  OK    $f"
    else
        echo "  FAIL  $f missing"
        ALL_OK=0
    fi
done
echo

# Check that urllib3 stayed at 2.7.0.
echo "=== urllib3 version ==="
URRLIB3_VER=$("$hermes_py" -c "import urllib3; print(urllib3.__version__)" 2>&1)
echo "  urllib3: $URRLIB3_VER"
if [[ "$URRLIB3_VER" == "2.7.0" ]]; then
    echo "  [OK]   urllib3 NOT downgraded"
else
    echo "  [FAIL] urllib3 changed (was 2.7.0, now $URRLIB3_VER)"
    ALL_OK=0
fi
echo

# Run pip check.
echo "=== pip check ==="
PIP_CHECK=$("$hermes_py" -m pip check 2>&1)
PIP_CHECK_RC=$?
echo "  exit: $PIP_CHECK_RC"
echo "  output: $PIP_CHECK"
if [[ "$PIP_CHECK" == *"No broken requirements found."* ]]; then
    echo "  [OK]   pip check: No broken requirements found"
else
    echo "  [FAIL] pip check reports broken requirements"
    ALL_OK=0
fi
echo

# Post-install imports.
echo "=== Post-install imports ==="
PYTHONPATH="$CLEAN_FAKE" "$hermes_py" -c "
import importlib
mods = [
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
    echo "  urllib3 preserved: yes"
    echo "  pip check clean: yes"
    echo "  post-install imports: 6/6 OK"
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