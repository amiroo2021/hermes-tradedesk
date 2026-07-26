#!/usr/bin/env bash
# test_clean_base_install.sh — Regression test for clean-base Hermes install.
#
# This test simulates the ACTUAL Kamatera Hermes v0.19.0 layout. The
# previous version created `__init__.py` files at every level of
# plugins/ (mimicking the DigitalOcean layout), which HID a real defect:
# Kamatera does NOT have those __init__.py files, so the previous
# install.sh check `plugins/ has __init__.py files` failed on Kamatera.
#
# The actual contract is:
#   - The destination plugins/platforms/telegram/ directory exists and
#     is writable.
#   - hermes_cli is importable.
#   - pip is available in the Hermes venv.
#
# We test that install.sh completes successfully against a Kamatera-faithful
# layout (NO __init__.py files under plugins/), bootstrapping the entire
# package including:
#   - pip dependencies
#   - tradedesk/ modules
#   - shared_selectors.py, _positions_render.py
#   - trade_menu/ (wizard.py, __init__.py)
#
# Strategy for testing pip install without network:
#   We pre-seed the production hermes venv (which already has the required
#   packages installed) by using a symlink-wrapper Python. The install.sh
#   does `pip install -r requirements.txt` which will detect that all
#   packages are already at the pinned versions and skip the install.
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

# Build a CLEAN base-Hermes tree that MIRRORS the actual Kamatera
# Hermes v0.19.0 layout. Specifically:
#   - NO __init__.py files under plugins/ (PEP 420 namespace package).
#   - Just the bare directory tree.
CLEAN_FAKE="/tmp/test-clean-base-$$"
mkdir -p "$CLEAN_FAKE"

# The install.sh will look for requirements.txt and manifest.json at
# $SCRIPT_DIR (which is $CLEAN_FAKE since we copy install.sh there). We
# pre-copy the public-package versions.
cp "$REQUIREMENTS" "$CLEAN_FAKE/requirements.txt"
cp "$PUB_DIR/manifest.json" "$CLEAN_FAKE/manifest.json"

# venv wrapper pointing at the production hermes venv (which has all
# the production-time packages installed).
hermes_py="/usr/local/lib/hermes-agent/venv/bin/python"
mkdir -p "$CLEAN_FAKE/venv/bin"
cat > "$CLEAN_FAKE/venv/bin/python" <<EOF
#!/usr/bin/env bash
exec $hermes_py "\$@"
EOF
chmod +x "$CLEAN_FAKE/venv/bin/python"

# Base Hermes layout: NO __init__.py anywhere. This is the Kamatera-faithful
# layout. (Production DigitalOcean has __init__.py at every level; the
# previous installer incorrectly assumed that layout.)
mkdir -p "$CLEAN_FAKE/plugins/platforms/telegram"
# Do NOT create __init__.py files. This is the whole point of this test:
# verify the install works without them.

# Fake hermes_cli package (BASE Hermes ships this; we just need a placeholder
# for the importability check). Use a real __init__.py here since this is
# the top-level hermes_cli module, not plugins/.
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

# Copy install.sh to CLEAN_FAKE so $SCRIPT_DIR resolves there and finds
# requirements.txt/manifest.json.
cp "$INSTALL_SH" "$CLEAN_FAKE/install.sh"
chmod +x "$CLEAN_FAKE/install.sh"

# Run install.sh.
echo "=== Running install.sh against Kamatera-faithful clean-base Hermes ==="
echo "  CLEAN_FAKE=$CLEAN_FAKE"
echo "  layout: NO __init__.py files in plugins/ (PEP 420 namespace package)"
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
echo "=== Post-install: integration import check (real Kamatera-faithful layout) ==="
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
