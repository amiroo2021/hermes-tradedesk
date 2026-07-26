#!/usr/bin/env bash
# install.sh — Install hermes-tradedesk onto a fresh Hermes installation
# -----------------------------------------------------------------------------
# This installer is INTENTIONALLY conservative:
#   - Performs STRUCTURAL compatibility checks against the BASE destination
#     Hermes (does NOT require an exact Hermes commit).
#   - Distinguishes BASE-HERMES prerequisites from PACKAGE-PROVIDED
#     components: package-provided components (TradeDesk modules, the
#     wizard, the Telegram overlay files, TradeDesk-specific Python
#     dependencies) are installed by THIS script, not required to
#     pre-exist.
#   - Refuses to overwrite ~/.hermes/.env or ~/.hermes/auth.json.
#   - Performs ZERO live trading actions. Verification is read-only.
#   - Backs up every file it modifies in the system install.
#
# Usage:
#   sudo ./install.sh
#
# Override the structural-compatibility check (NOT recommended — only for
# ops-time use when you have manually verified integration contracts):
#   sudo HERMES_TRADESK_SKIP_STRUCT_CHECK=1 ./install.sh
#
# This script is idempotent: re-running it reinstalls over the prior version
# after backing up the existing files.
# -----------------------------------------------------------------------------
set -euo pipefail

# ---- Locate self (so the script works regardless of cwd) ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source-tree paths: derive from SCRIPT_DIR by default. Allow override via
# env var (used by automated install-compatibility tests that drop install.sh
# into a temp dir while pointing the source paths back at this repo).
SRC_TRADEDESK="$SCRIPT_DIR/tradedesk"
SRC_OVERLAY="$SCRIPT_DIR/hermes_overlay"
if [[ -n "${HERMES_TRADESK_SRC_ROOT:-}" ]]; then
    SRC_TRADEDESK="$HERMES_TRADESK_SRC_ROOT/tradedesk"
    SRC_OVERLAY="$HERMES_TRADESK_SRC_ROOT/hermes_overlay"
fi

err() { echo "ERROR: $*" >&2; }
say() { echo "$@"; }
warn() { echo "WARN  $*"; }

# ---------------------------------------------------------------------------
# 0. Root check
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    err "install.sh must be run as root (try: sudo ./install.sh)"
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Linux environment check
# ---------------------------------------------------------------------------
say "=== Hermes TradeDesk installer ==="

if [[ ! -f /etc/os-release ]]; then
    err "Cannot detect Linux distribution (no /etc/os-release)"
    exit 1
fi
. /etc/os-release
say "  host distro: ${PRETTY_NAME:-$ID}"

# ---------------------------------------------------------------------------
# 2. BASE Hermes detection (the install is anchored to an existing Hermes
#    install; the user must already have Hermes on this machine).
# ---------------------------------------------------------------------------
HERMES_HOME="${HERMES_TRADESK_HERMES_HOME:-/usr/local/lib/hermes-agent}"
HERMES_BIN="${HERMES_TRADESK_HERMES_BIN:-/usr/local/bin/hermes}"
USER_HOME_ROOT="/root"
USER_ENV="$USER_HOME_ROOT/.hermes/.env"
USER_AUTH="$USER_HOME_ROOT/.hermes/auth.json"

if [[ ! -d "$HERMES_HOME" ]]; then
    err "No Hermes source at $HERMES_HOME"
    err "This package is meant to be installed ON TOP OF an existing Hermes"
    exit 1
fi
if [[ ! -x "$HERMES_BIN" ]]; then
    err "Hermes CLI not found at $HERMES_BIN"
    exit 1
fi
say "  Hermes source:  $HERMES_HOME"
say "  Hermes CLI:     $HERMES_BIN"

# ---------------------------------------------------------------------------
# 3. Detect installed Hermes commit (informational only — NOT a hard gate).
# ---------------------------------------------------------------------------
if command -v git >/dev/null 2>&1; then
    HERMES_COMMIT="$(cd "$HERMES_HOME" && git rev-parse HEAD 2>/dev/null || echo unknown)"
    HERMES_BRANCH="$(cd "$HERMES_HOME" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    say "  Hermes commit:  $HERMES_COMMIT (informational)"
    say "  Hermes branch:  $HERMES_BRANCH"
else
    HERMES_COMMIT="(git unavailable)"
    say "  Hermes commit:  $HERMES_COMMIT (informational)"
fi

HERMES_PY="$HERMES_HOME/venv/bin/python"
if [[ ! -x "$HERMES_PY" ]]; then
    err "Hermes venv Python not found at $HERMES_PY"
    exit 1
fi

# ---------------------------------------------------------------------------
# PHASE 1 — Validate BASE-Hermes structural contracts only.
#
# The user-instructional contracts:
#   - hermes_cli Python package importable (BASE Hermes ships this)
#   - pip available in the Hermes venv (BASE Hermes ships this)
#   - destination plugins/platforms/telegram/ directory exists and is
#     writable (so we can copy our overlay there)
#
# We do NOT require:
#   - plugins/__init__.py, plugins/platforms/__init__.py,
#     plugins/platforms/telegram/__init__.py
# These are NOT required by the actual import semantics: Python 3.3+ supports
# PEP 420 implicit namespace packages, and `from plugins.platforms.telegram...
# import ...` works whether or not those __init__.py files exist (as long
# as the directories exist on PYTHONPATH and contain the .py modules we
# ship).
#
# The previous installer required these __init__.py files; that was an
# over-strict structural assumption that does not exist in real compatible
# Hermes installations (e.g. Kamatera Hermes v0.19.0).
# ---------------------------------------------------------------------------
say ""
say "Phase 1: validate base-Hermes structural contracts..."

HERMES_BASIC_PASSED=0
HERMES_BASIC_FAILED=0
hermes_basic_failures=()

check_hermes_basic() {
    local label="$1"
    local result="$2"
    if [[ "$result" == "ok" ]]; then
        say "    [OK]   $label"
        HERMES_BASIC_PASSED=$((HERMES_BASIC_PASSED + 1))
    else
        say "    [FAIL] $label"
        HERMES_BASIC_FAILED=$((HERMES_BASIC_FAILED + 1))
        hermes_basic_failures+=("$label")
    fi
}

# Check: hermes_cli Python package is importable (this is provided by base
# Hermes, not by us). We need it because the wizard eventually submits to
# TradeDesk which is part of hermes_agent.
if "$HERMES_PY" -c "import hermes_cli" 2>/dev/null; then
    check_hermes_basic "hermes_cli Python package importable" ok
else
    check_hermes_basic "hermes_cli Python package importable" fail
fi

# Check: hermes_cli exports __version__ (sanity — it's not optional).
if "$HERMES_PY" -c "from hermes_cli import __version__" 2>/dev/null; then
    check_hermes_basic "hermes_cli.__version__ importable" ok
else
    check_hermes_basic "hermes_cli.__version__ importable" fail
fi

# Check: pip is available in the Hermes venv (we need it to install
# this package's pip dependencies).
if "$HERMES_PY" -m pip --version 2>/dev/null; then
    check_hermes_basic "pip available in Hermes venv" ok
else
    check_hermes_basic "pip available in Hermes venv" fail
fi

# Check: the destination plugins/platforms/telegram/ directory exists and
# is writable. We do NOT check for __init__.py (the actual import contract
# is satisfied by PEP 420 namespace packages). We only need:
#   1. The directory exists
#   2. We can create files there
# Note: this is the directory layout required by the wizard (which uses
# `from plugins.platforms.telegram... import ...`).
if [[ -d "$HERMES_HOME/plugins/platforms/telegram" ]]; then
    # The directory exists. Check we can write to it.
    test_file="$HERMES_HOME/plugins/platforms/telegram/.hermes-tradedesk-writable-test"
    if ( : > "$test_file" ) 2>/dev/null; then
        rm -f "$test_file"
        check_hermes_basic "plugins/platforms/telegram/ is writable" ok
    else
        check_hermes_basic "plugins/platforms/telegram/ is writable" fail
    fi
else
    check_hermes_basic "plugins/platforms/telegram/ exists" fail
fi

# Check: we need a place for the tradedesk/ package. We install it under
# $HERMES_HOME/tradesk/. Check that the parent directory is writable.
test_file="$HERMES_HOME/.hermes-tradedesk-writable-test"
if ( : > "$test_file" ) 2>/dev/null; then
    rm -f "$test_file"
    check_hermes_basic "Hermes home directory is writable" ok
else
    check_hermes_basic "Hermes home directory is writable" fail
fi

# Final base-Hermes verdict.
say ""
say "Base-Hermes structural compatibility: $HERMES_BASIC_PASSED passed, $HERMES_BASIC_FAILED failed"
if [[ "$HERMES_BASIC_FAILED" -gt 0 ]]; then
    if [[ "${HERMES_TRADESK_SKIP_STRUCT_CHECK:-0}" == "1" ]]; then
        warn "Skipping structural compatibility gate (HERMES_TRADESK_SKIP_STRUCT_CHECK=1)."
        warn "Installation will likely fail at runtime."
    else
        err "Refusing to install: BASE Hermes does not provide the required"
        err "structural contracts:"
        for f in "${hermes_basic_failures[@]}"; do
            err "  - $f"
        done
        err ""
        err "If you have manually verified these contracts, you may override"
        err "with HERMES_TRADESK_SKIP_STRUCT_CHECK=1. (NOT recommended.)"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# PHASE 2 — Validate public package source files exist.
# ---------------------------------------------------------------------------
say ""
say "Phase 2: validate public package source files..."

pkg_missing=()
for f in "$SRC_TRADEDESK"/*.py \
         "$SRC_TRADEDESK"/__init__.py \
         "$SRC_OVERLAY/telegram/shared_selectors.py" \
         "$SRC_OVERLAY/telegram/_positions_render.py" \
         "$SRC_OVERLAY/telegram/trade_menu/__init__.py" \
         "$SRC_OVERLAY/telegram/trade_menu/wizard.py"; do
    if [[ ! -f "$f" ]]; then
        pkg_missing+=("$f")
    fi
done
if [[ ${#pkg_missing[@]} -gt 0 ]]; then
    err "Public package is missing required source files:"
    for f in "${pkg_missing[@]}"; do
        err "  - $f"
    done
    exit 1
else
    say "  [OK]   all $(($(echo "$SRC_TRADEDESK"/*.py | wc -w) + 4)) package source files present"
fi

# ---------------------------------------------------------------------------
# PHASE 3 — Validate requirements.txt is valid/nonempty.
# ---------------------------------------------------------------------------
say ""
say "Phase 3: validate requirements.txt..."

REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
    err "Missing $REQUIREMENTS_FILE"
    exit 1
fi
# Count non-comment lines that look like package specifiers.
PKG_COUNT=$(grep -cE "^[A-Za-z0-9_.-]+(==|>=|<=|~=|!=|===|>|<)" "$REQUIREMENTS_FILE" 2>/dev/null || echo 0)
if [[ "$PKG_COUNT" -lt 1 ]]; then
    err "requirements.txt has no valid package specifiers"
    exit 1
fi
say "  [OK]   requirements.txt has $PKG_COUNT package specifiers"

# ---------------------------------------------------------------------------
# PHASE 4 — Create backups for destination files that will be overwritten.
# ---------------------------------------------------------------------------
say ""
say "Phase 4: create backups for files that will be overwritten..."

BACKUP_DIR="${HOME:-/root}/.hermes/tradedesk-install-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
say "  Backup dir: $BACKUP_DIR"

backup_file() {
    local f="$1"
    if [[ -e "$f" ]]; then
        local rel="${f#$HERMES_HOME/}"
        local dest="$BACKUP_DIR/$rel"
        mkdir -p "$(dirname "$dest")"
        cp -p "$f" "$dest"
        say "    backup: $rel"
    fi
}

# Back up existing tradedesk files.
for f in "$SRC_TRADEDESK"/*.py; do
    fname="$(basename "$f")"
    target="$HERMES_HOME/tradedesk/$fname"
    if [[ -e "$target" ]]; then
        backup_file "$target"
    fi
done
# Back up existing telegram plugin files.
for f in shared_selectors.py _positions_render.py; do
    target="$HERMES_HOME/plugins/platforms/telegram/$f"
    if [[ -e "$target" ]]; then
        backup_file "$target"
    fi
done
# Back up existing trade_menu files.
for f in __init__.py wizard.py; do
    target="$HERMES_HOME/plugins/platforms/telegram/trade_menu/$f"
    if [[ -e "$target" ]]; then
        backup_file "$target"
    fi
done

# ---------------------------------------------------------------------------
# PHASE 5 — Install declared pip dependencies into the Hermes venv.
#
# Strategy: use `pip install` (with default dep resolution) WITHOUT
# `--no-deps`. The previous public release used `--no-deps`, which broke
# on a clean Hermes because transitive deps (eth-hash, eth-rlp, etc.)
# were missing. We have audited the package set against the actual
# DigitalOcean production runtime and pinned exact versions.
#
# We use a constraints-style approach: pin all top-level packages, but
# let pip resolve transitive deps. This is the standard approach.
#
# For packages NOT on PyPI (e.g. afx-python-sdk which is a private
# package only on the DigitalOcean server), we mark them as
# documentation-only in requirements.txt (prefixed with # private/).
# They will be reported as missing in PHASE 9 but are NOT required for
# the wizard to function — only for the AFx exchange.
# ---------------------------------------------------------------------------
say ""
say "Phase 5: install Python dependencies into Hermes venv..."

# Split requirements.txt into installable and private-only sections.
# Lines starting with `# private/` are documentation-only.
INST_FILE="$BACKUP_DIR/requirements-installable.txt"
PRIVATE_FILE="$BACKUP_DIR/requirements-private.txt"
: > "$INST_FILE"
: > "$PRIVATE_FILE"
while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*#[[:space:]]*private/ ]]; then
        echo "$line" >> "$PRIVATE_FILE"
    elif [[ -n "$line" && ! "$line" =~ ^[[:space:]]*# ]]; then
        echo "$line" >> "$INST_FILE"
    fi
done < "$REQUIREMENTS_FILE"

# Run pip install.
say "  Installing pip packages from $INST_FILE..."
if "$HERMES_PY" -m pip install -r "$INST_FILE" 2>&1 | tail -20; then
    say "  [OK]   pip install completed"
else
    rc=$?
    err "pip install failed (rc=$rc)"
    err "Continuing to PHASE 6 to verify which packages are importable"
    err "and which need manual installation. The private packages"
    err "(marked # private/ in requirements.txt) must be installed"
    err "manually by the operator if they want those exchanges."
fi

# ---------------------------------------------------------------------------
# PHASE 6 — Verify that the just-installed dependencies are importable.
#
# We check the package list from requirements.txt. For any package
# that is still not importable, we report it clearly. Some packages
# may be marked `# private/` meaning they cannot be installed from PyPI
# (e.g. afx-python-sdk). These are documented but NOT required for
# the wizard to function.
# ---------------------------------------------------------------------------
say ""
say "Phase 6: verify dependencies are importable..."

import_ok=0
import_fail=0
import_failures=()

verify_import() {
    local pkg_name="$1"
    local import_name="$2"
    if "$HERMES_PY" -c "import $import_name" 2>/dev/null; then
        import_ok=$((import_ok + 1))
    else
        import_fail=$((import_fail + 1))
        import_failures+=("$pkg_name (import $import_name)")
    fi
}

# Always-required (these are imported at module level by agents).
verify_import "eth-account"        "eth_account"
verify_import "cryptography"       "cryptography"
verify_import "base58"            "base58"
verify_import "requests"           "requests"
verify_import "hyperliquid-python-sdk" "hyperliquid"
verify_import "lighter-sdk"       "lighter"
verify_import "python-telegram-bot" "telegram"

# Optional / lazy-imported (only needed when those specific agents run).
# We don't fail the install on these, but we report which are missing.
optional_ok=0
optional_missing=()
verify_optional() {
    local pkg_name="$1"
    local import_name="$2"
    if "$HERMES_PY" -c "import $import_name" 2>/dev/null; then
        optional_ok=$((optional_ok + 1))
    else
        optional_missing+=("$pkg_name (import $import_name)")
    fi
}
verify_optional "solders"        "solders"
verify_optional "apexomni"       "apexomni"
verify_optional "pycryptodome"   "Crypto"
verify_optional "eth-abi"        "eth_abi"
verify_optional "eth-utils"      "eth_utils"

say "  Always-required imports: $import_ok passed, $import_fail failed"
if [[ $import_fail -gt 0 ]]; then
    err "The following required packages did not become importable:"
    for f in "${import_failures[@]}"; do
        err "  - $f"
    done
    err ""
    err "Installation cannot continue. Check that pip is functional and"
    err "that pip can reach PyPI (e.g. behind a corporate proxy?)."
    exit 1
fi

if [[ ${#optional_missing[@]} -gt 0 ]]; then
    warn "Optional packages not importable (only needed when using specific exchanges):"
    for f in "${optional_missing[@]}"; do
        warn "  - $f"
    done
    warn "The wizard will work but specific exchanges may fail at runtime."
    warn "Some of these may be private/edge packages (e.g. afx-python-sdk)"
    warn "that are not on PyPI and must be installed manually."
fi

# ---------------------------------------------------------------------------
# PHASE 7 — Copy/install TradeDesk modules.
# ---------------------------------------------------------------------------
say ""
say "Phase 7: install TradeDesk modules..."

mkdir -p "$HERMES_HOME/tradedesk"
for f in "$SRC_TRADEDESK"/*.py; do
    fname="$(basename "$f")"
    cp "$f" "$HERMES_HOME/tradedesk/$fname"
done
say "  Installed $(ls "$SRC_TRADEDESK"/*.py | wc -l) TradeDesk modules"

# ---------------------------------------------------------------------------
# PHASE 8 — Copy Telegram overlay.
# ---------------------------------------------------------------------------
say ""
say "Phase 8: install Telegram overlay (wizard, shared_selectors, _positions_render)..."

mkdir -p "$HERMES_HOME/plugins/platforms/telegram/trade_menu"
cp "$SRC_OVERLAY/telegram/shared_selectors.py" \
   "$HERMES_HOME/plugins/platforms/telegram/shared_selectors.py"
cp "$SRC_OVERLAY/telegram/_positions_render.py" \
   "$HERMES_HOME/plugins/platforms/telegram/_positions_render.py"
cp "$SRC_OVERLAY/telegram/trade_menu/__init__.py" \
   "$HERMES_HOME/plugins/platforms/telegram/trade_menu/__init__.py"
cp "$SRC_OVERLAY/telegram/trade_menu/wizard.py" \
   "$HERMES_HOME/plugins/platforms/telegram/trade_menu/wizard.py"
say "  Installed 4 Telegram overlay files"

# ---------------------------------------------------------------------------
# PHASE 9 — POST-INSTALL integration/import verification.
#
# This is the final check: with PYTHONPATH set to the Hermes install
# root, can we actually import every required module? If not, fail
# truthfully so the operator sees what's broken.
# ---------------------------------------------------------------------------
say ""
say "Phase 9: post-install integration verification..."

post_ok=0
post_fail=0
post_failures=()

verify_post_install() {
    local label="$1"
    local module="$2"
    # Set PYTHONPATH to the Hermes install root so plugins.* imports
    # work. We run the import under a fresh subprocess to keep state
    # isolated.
    if PYTHONPATH="$HERMES_HOME" "$HERMES_PY" -c "import $module" 2>/dev/null; then
        say "    [OK]   $label"
        post_ok=$((post_ok + 1))
    else
        say "    [FAIL] $label"
        post_fail=$((post_fail + 1))
        post_failures+=("$label")
    fi
}

verify_post_install "tradedesk.tradedesk"                                  "tradedesk.tradedesk"
verify_post_install "tradedesk.lighter_agent"                              "tradedesk.lighter_agent"
verify_post_install "tradedesk.raydium_agent"                              "tradedesk.raydium_agent"
verify_post_install "tradesdesk.account_discovery"                            "tradedesk.account_discovery"
verify_post_install "plugins.platforms.telegram.shared_selectors"          "plugins.platforms.telegram.shared_selectors"
verify_post_install "plugins.platforms.telegram._positions_render"         "plugins.platforms.telegram._positions_render"
verify_post_install "plugins.platforms.telegram.trade_menu.wizard"          "plugins.platforms.telegram.trade_menu.wizard"
verify_post_install "plugins.platforms.telegram.trade_menu"                "plugins.platforms.telegram.trade_menu"

say ""
say "Post-install verification: $post_ok passed, $post_fail failed"
if [[ $post_fail -gt 0 ]]; then
    err "POST-INSTALL VERIFICATION FAILED."
    err "The following modules could not be imported after install:"
    for f in "${post_failures[@]}"; do
        err "  - $f"
    done
    err ""
    err "This is a real failure. Do NOT assume the install succeeded."
    err "Inspect the install log and the destination Hermes manually."
    exit 1
fi

# ---------------------------------------------------------------------------
# PHASE 10 — Compatibility manifest (informational).
# ---------------------------------------------------------------------------
MANIFEST="$SCRIPT_DIR/manifest.json"
REF_COMMIT="(no manifest)"
if [[ -f "$MANIFEST" ]]; then
    REF_COMMIT="$("$HERMES_PY" - <<EOF
import json
with open("$MANIFEST") as f:
    print(json.load(f).get("reference_hermes_commit", "(none)"))
EOF
)"
    say ""
    say "  Reference Hermes commit (informational): $REF_COMMIT"
fi

# ---------------------------------------------------------------------------
# PHASE 11 — Final report (sanitized) + verify.sh.
# ---------------------------------------------------------------------------
say ""
say "Phase 11: finalizing install..."

SANITIZED_REPORT="$BACKUP_DIR/install-report.txt"
{
    echo "Hermes TradeDesk install report"
    echo "================================"
    echo "Hermes commit:                  $HERMES_COMMIT"
    echo "Reference commit:               $REF_COMMIT"
    echo "Base-Hermes basic checks:       $HERMES_BASIC_PASSED passed, $HERMES_BASIC_FAILED failed"
    echo "Always-required imports:        $import_ok passed, $import_fail failed"
    echo "Optional imports available:     $optional_ok"
    echo "Post-install integration:        $post_ok passed, $post_fail failed"
    echo ""
    echo "Configured account aliases (NAMES ONLY):"
    echo "  (not enumerated to keep report sanitized)"
    echo ""
    echo "Credential values printed: NO"
    echo "Trading writes performed:  0"
} | tee "$SANITIZED_REPORT"

# Run verify.sh (if it's available) — note that verify.sh expects to run
# against the bundled package source, not the install destination.
if [[ -x "$SCRIPT_DIR/verify.sh" ]]; then
    say ""
    say "Running offline verification (verify.sh)..."
    "$SCRIPT_DIR/verify.sh" || say "  (verify.sh reported a non-zero exit; investigate)"
else
    say ""
    say "  (verify.sh not executable; skipping)"
fi

# ---------------------------------------------------------------------------
# 12. NEVER copy credentials from this repo into the system
# 13. NEVER print secret values
# ---------------------------------------------------------------------------
say ""
say "Preserving Hermes AI configuration..."
if [[ -e "$USER_AUTH" ]]; then
    say "  keeping existing ~/.hermes/auth.json (NOT TOUCHED)"
fi
if [[ -e "$USER_ENV" ]]; then
    say "  keeping existing ~/.hermes/.env    (NOT TOUCHED)"
fi

# ---------------------------------------------------------------------------
# Final instructions
# ---------------------------------------------------------------------------
say ""
say "Install complete."
say "Backups retained at: $BACKUP_DIR"
say ""
say "Next steps for the operator:"
say "  1. Edit ~/.hermes/.env with your exchange credentials (use .env.example for names)."
say "  2. systemctl --user restart hermes-gateway.service"
say "  3. Open Telegram, send /trade to your bot, and verify balance / positions / orders."

exit 0
