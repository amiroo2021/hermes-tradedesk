#!/usr/bin/env bash
# test_clean_base_install.sh — Regression test for clean-base Hermes install.
#
# This test simulates a Kamatera-faithful starting state, runs the
# install.sh, and verifies:
#   - install.sh exit 0
#   - All 14 tradedesk modules + 4 telegram overlay files are present
#   - 4 __init__.py marker files at plugins/, plugins/platforms/,
#     plugins/platforms/telegram/, plugins/platforms/telegram/trade_menu/
#   - 8/8 always-required post-install imports succeed
#   - urllib3 stays at 2.7.0 (NO downgrade)
#   - pip check only reports the documented Lighter/urllib3 conflict
#   - import lighter + from lighter import SignerClient both PASS
#   - lighter-sdk was NEVER installed via the normal pip resolver
#     (only via --no-deps)
#
# The test uses a Kamatera-faithful venv (hermes-agent 0.19.0,
# urllib3 2.7.0, no TradeDesk deps, no lighter-sdk) — NOT the
# production DigitalOcean venv, which would have lighter-sdk
# pre-installed and a stale urllib3.
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

# Build a CLEAN base-Hermes tree mirroring the actual Kamatera layout.
CLEAN_FAKE="/tmp/test-clean-base-$$"
mkdir -p "$CLEAN_FAKE"

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

# ---- Capture PRE-install state ----
PRE_URLLIB3=$("$hermes_py" -c "import urllib3; print(urllib3.__version__)" 2>/dev/null || echo "(none)")
echo "=== PRE-install urllib3: $PRE_URLLIB3 ==="

# Confirm lighter is NOT installed at start.
PRE_LIGHTER=$("$hermes_py" -c "import lighter" 2>&1 | head -1 || echo "(none)")
echo "=== PRE-install lighter: $PRE_LIGHTER ==="

# Capture install.sh output for grep-based assertions.
INSTALL_LOG="$CLEAN_FAKE/install.log"

# Run install.sh.
echo "=== Running install.sh against Kamatera-faithful clean-base Hermes ==="
HERMES_PY="$CLEAN_FAKE/venv/bin/python" \
HERMES_TRADESK_SKIP_STRUCT_CHECK=0 \
HERMES_TRADESK_SRC_ROOT="$PUB_DIR" \
HERMES_TRADESK_HERMES_HOME="$CLEAN_FAKE" \
HERMES_TRADESK_HERMES_BIN="$CLEAN_FAKE/bin/hermes" \
HOME="$CLEAN_FAKE/fake-home" \
PATH="/usr/bin:/bin" \
PYTHONPATH="$CLEAN_FAKE" \
bash "$CLEAN_FAKE/install.sh" > "$INSTALL_LOG" 2>&1
rc=$?
echo
echo "=== install.sh exit: $rc ==="

# ---- Verify post-install state ----
echo
echo "=== Post-install state ==="

# urllib3 must still be 2.7.0.
POST_URLLIB3=$("$hermes_py" -c "import urllib3; print(urllib3.__version__)" 2>/dev/null || echo "(none)")
echo "  urllib3 after install: $POST_URLLIB3"
URLLIB3_OK=1
if [[ "$POST_URLLIB3" != "2.7.0" ]]; then
    echo "  [FAIL] urllib3 changed (was 2.7.0, now $POST_URLLIB3)"
    URLLIB3_OK=0
else
    echo "  [OK]   urllib3 NOT downgraded (still 2.7.0)"
fi

# lighter must be importable.
LIGHT_IMPORT=$("$hermes_py" -c "import lighter; print('lighter at:', lighter.__file__)" 2>&1)
echo "  import lighter: $LIGHT_IMPORT"

SIGNER_IMPORT=$("$hermes_py" -c "from lighter import SignerClient; print('SignerClient OK')" 2>&1)
echo "  from lighter import SignerClient: $SIGNER_IMPORT"

LIGHT_OK=1
if [[ "$SIGNER_IMPORT" != *"SignerClient OK"* ]]; then
    echo "  [FAIL] from lighter import SignerClient FAILED"
    LIGHT_OK=0
else
    echo "  [OK]   lighter-sdk imports OK"
fi

# Check pip check output.
echo
echo "=== pip check ==="
PIP_CHECK=$("$hermes_py" -m pip check 2>&1)
PIP_CHECK_RC=$?
echo "  exit: $PIP_CHECK_RC"
echo "  output:"
while IFS= read -r line; do
    echo "    $line"
done <<< "$PIP_CHECK"

# pip check should report only the documented Lighter conflict (and the
# aiohttp-retry missing). Both are accepted.
PIP_CHECK_HAS_OTHER_CONFLICT=0
if [[ "$PIP_CHECK" == *"No broken requirements found."* ]]; then
    echo "  [OK]   pip check: No broken requirements found"
    PIP_CHECK_HAS_OTHER_CONFLICT=0
else
    # Check that ONLY lighter-related lines are present.
    while IFS= read -r line; do
        if [[ "$line" =~ lighter-sdk ]] || [[ -z "$line" ]]; then
            continue
        fi
        echo "  [FAIL]  unexpected pip-check conflict: $line"
        PIP_CHECK_HAS_OTHER_CONFLICT=1
    done <<< "$PIP_CHECK"
    if [[ $PIP_CHECK_HAS_OTHER_CONFLICT -eq 0 ]]; then
        echo "  [OK]   pip check: only documented lighter-sdk metadata conflicts"
    fi
fi

# Verify the install.sh log shows the documented Lighter separation.
echo
echo "=== install.sh log assertions ==="
LOG_OK=1
if ! grep -q "Step A:" "$INSTALL_LOG"; then
    echo "  [FAIL] install.sh log does not show 'Step A:' (normal resolver)"
    LOG_OK=0
fi
if ! grep -q "Step B:" "$INSTALL_LOG"; then
    echo "  [FAIL] install.sh log does not show 'Step B:' (lighter install)"
    LOG_OK=0
fi
if ! grep -q -- "--no-deps" "$INSTALL_LOG"; then
    echo "  [FAIL] install.sh log does not contain '--no-deps'"
    LOG_OK=0
fi
# Verify the lighter package was NOT installed via the normal resolver.
if grep -q "Step A.*lighter" "$INSTALL_LOG"; then
    echo "  [FAIL] lighter was installed via Step A (normal resolver); should only be Step B"
    LOG_OK=0
else
    echo "  [OK]   lighter was NOT installed via normal resolver"
fi
if [[ $LOG_OK -eq 1 ]]; then
    echo "  [OK]   install.sh used Step A + Step B correctly"
fi

# All expected files.
echo
echo "=== Expected files ==="
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
    "plugins/__init__.py"
    "plugins/platforms/__init__.py"
    "plugins/platforms/telegram/__init__.py"
)
FILES_OK=1
for f in "${EXPECTED_FILES[@]}"; do
    if [[ -f "$CLEAN_FAKE/$f" ]]; then
        echo "  OK    $f"
    else
        echo "  FAIL  $f missing"
        FILES_OK=0
    fi
done

# Post-install imports (always-required: 8/8).
echo
echo "=== Post-install imports (8/8) ==="
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
if [[ $rc -eq 0 && $URLLIB3_OK -eq 1 && $LIGHT_OK -eq 1 && $PIP_CHECK_HAS_OTHER_CONFLICT -eq 0 && $LOG_OK -eq 1 && $FILES_OK -eq 1 && $import_rc -eq 0 ]]; then
    echo "=== REGRESSION TEST RESULT: PASS ==="
    echo "  install.sh exit:                 $rc"
    echo "  urllib3 preserved at 2.7.0:      yes"
    echo "  lighter-sdk importable:          yes"
    echo "  SignerClient importable:          yes"
    echo "  pip check has only Lighter conflicts: yes"
    echo "  install.sh used Step A + Step B: yes"
    echo "  all expected files present:      yes"
    echo "  post-install imports (8/8):      PASS"
    OVERALL=0
else
    echo "=== REGRESSION TEST RESULT: FAIL ==="
    echo "  install.sh exit:                  $rc"
    echo "  urllib3 preserved (URLLIB3_OK):   $URLLIB3_OK"
    echo "  lighter OK:                       $LIGHT_OK"
    echo "  pip check has no other conflicts: $PIP_CHECK_HAS_OTHER_CONFLICT"
    echo "  install.sh log OK:                $LOG_OK"
    echo "  all files present (FILES_OK):     $FILES_OK"
    echo "  post-install imports (import_rc): $import_rc"
    OVERALL=1
fi

# Cleanup.
rm -rf "$CLEAN_FAKE"
exit $OVERALL