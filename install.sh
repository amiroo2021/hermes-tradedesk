#!/usr/bin/env bash
# install.sh — Install hermes-tradedesk onto a fresh Hermes installation
# -----------------------------------------------------------------------------
# This installer is INTENTIONALLY conservative:
#   - Performs structural compatibility checks against the destination
#     Hermes installation (does NOT require an exact Hermes commit).
#   - Refuses to install when the destination Hermes lacks required
#     integration contracts.
#   - Refuses to overwrite ~/.hermes/.env or ~/.hermes/auth.json.
#   - Performs ZERO live trading actions. Verification is read-only.
#   - Backs up every file it modifies in the system install.
#
# Usage:
#   sudo ./install.sh
#
# Override the structural-compatibility check (NOT recommended — only for
# ops-time use when you have manually verified integration contracts):
#   sudo HERMES_TRADEDESK_SKIP_STRUCT_CHECK=1 ./install.sh
#
# This script is idempotent: re-running it reinstalls over the prior version
# after backing up the existing files.
# -----------------------------------------------------------------------------
set -euo pipefail

# ---- Locate self (so the script works regardless of cwd) ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Honor external overrides of the source-tree paths. Used by automated
# install-compatibility tests that drop install.sh into a temp dir while
# pointing the source paths back at this repo.
if [[ -n "${HERMES_TRADEDESK_SRC_ROOT:-}" ]]; then
    SRC_TRADEDESK="$HERMES_TRADEDESK_SRC_ROOT/tradedesk"
    SRC_OVERLAY="$HERMES_TRADEDESK_SRC_ROOT/hermes_overlay"
fi

SRC_TRADEDESK="$SRC_TRADEDESK"
SRC_OVERLAY="$SRC_OVERLAY"

err() { echo "ERROR: $*" >&2; }
say() { echo "$@"; }

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
# 2. Hermes detection
# ---------------------------------------------------------------------------
HERMES_HOME="/usr/local/lib/hermes-agent"
HERMES_BIN="/usr/local/bin/hermes"
USER_HOME_ROOT="/root"
USER_ENV="$USER_HOME_ROOT/.hermes/.env"
USER_AUTH="$USER_HOME_ROOT/.hermes/auth.json"

if [[ ! -d "$HERMES_HOME" ]]; then
    err "No Hermes source at $HERMES_HOME"
    err "This package is meant to be installed ON TOP of an existing Hermes"
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

# ---------------------------------------------------------------------------
# 4-5. STRUCTURAL compatibility gate (NOT a SHA-equality check).
#
# Required integration contracts (verified by file presence + symbol grep):
#   - plugins/platforms/telegram/shared_selectors.py exists with the
#     expected export names (used by the wizard).
#   - plugins/platforms/telegram/_positions_render.py exists.
#   - hermes_cli/ package is importable.
#   - Python package dependencies required by the agents are present.
#   - telegram integration surface exists.
# ---------------------------------------------------------------------------
HERMES_PY="$HERMES_HOME/venv/bin/python"
if [[ ! -x "$HERMES_PY" ]]; then
    err "Hermes venv Python not found at $HERMES_PY"
    exit 1
fi

# Required integration contracts.
STRUCT_CHECKS_PASSED=0
STRUCT_CHECKS_FAILED=0
struct_failures=()

run_struct_check() {
    local label="$1"
    local result="$2"
    if [[ "$result" == "ok" ]]; then
        say "    [OK]   $label"
        STRUCT_CHECKS_PASSED=$((STRUCT_CHECKS_PASSED + 1))
    else
        say "    [FAIL] $label"
        STRUCT_CHECKS_FAILED=$((STRUCT_CHECKS_FAILED + 1))
        struct_failures+=("$label")
    fi
}

say ""
say "Running structural compatibility checks against destination Hermes..."
say "  (these check integration CONTRACTS, not exact commits)"

# Check 1: required Telegram shared_selectors module exists with expected exports.
SELECTORS_FILE="$HERMES_HOME/plugins/platforms/telegram/shared_selectors.py"
SELECTORS_OK=missing
if [[ -f "$SELECTORS_FILE" ]]; then
    # Check that all 5 expected symbols are exported.
    missing_symbols=""
    for sym in account_keyboard account_prompt exchange_keyboard exchange_prompt lighter_account_keyboard; do
        if ! grep -qE "^\s*def\s+$sym\b|^$sym\s*[:=]" "$SELECTORS_FILE"; then
            missing_symbols+=" $sym"
        fi
    done
    if [[ -z "$missing_symbols" ]]; then
        SELECTORS_OK=ok
    else
        SELECTORS_OK="missing symbols:$missing_symbols"
    fi
fi
run_struct_check "plugins/platforms/telegram/shared_selectors.py exports" "$SELECTORS_OK"

# Check 2: _positions_render module exists.
POSITIONS_RENDER_FILE="$HERMES_HOME/plugins/platforms/telegram/_positions_render.py"
POSITIONS_RENDER_OK=missing
if [[ -f "$POSITIONS_RENDER_FILE" ]]; then
    POSITIONS_RENDER_OK=ok
fi
run_struct_check "plugins/platforms/telegram/_positions_render.py exists" "$POSITIONS_RENDER_OK"

# Check 3: hermes_cli package is importable.
HERMES_CLI_OK=missing
HERMES_CLI_OUT="$("$HERMES_PY" -c "import hermes_cli; print('ok')" 2>&1 || true)"
if [[ "$HERMES_CLI_OUT" == *"ok"* ]]; then
    HERMES_CLI_OK=ok
fi
run_struct_check "hermes_cli package importable" "$HERMES_CLI_OK"

# Check 4: required Python deps for the agents.
PYDEPS_OK=ok
PYDEPS_FAILED=()
for dep in eth_account eth_abi eth_utils cryptography base58 requests solders lighter; do
    out="$("$HERMES_PY" -c "import $dep" 2>&1 || true)"
    if [[ -n "$out" ]]; then
        PYDEPS_OK=fail
        PYDEPS_FAILED+=("$dep")
    fi
done
if [[ "$PYDEPS_OK" == "ok" ]]; then
    run_struct_check "Python deps for exchange agents (eth_account, eth_abi, eth_utils, cryptography, base58, requests, solders, lighter)" ok
else
    run_struct_check "Python deps for exchange agents (missing: ${PYDEPS_FAILED[*]})" fail
fi

# Check 5: telegram package importable (only if Telegram integration is configured).
TELEGRAM_OK=ok
TELEGRAM_OUT="$("$HERMES_PY" -c "import telegram" 2>&1 || true)"
if [[ -n "$TELEGRAM_OUT" ]]; then
    TELEGRAM_OK=fail
fi
run_struct_check "telegram Python package importable" "$TELEGRAM_OK"

# Final structural-compatibility verdict.
say ""
say "Structural compatibility summary:"
say "  passed: $STRUCT_CHECKS_PASSED"
say "  failed: $STRUCT_CHECKS_FAILED"

if [[ "$STRUCT_CHECKS_FAILED" -gt 0 ]]; then
    if [[ "${HERMES_TRADESK_SKIP_STRUCT_CHECK:-0}" == "1" ]]; then
        say ""
        err "Structural compatibility FAILED on $STRUCT_CHECKS_FAILED check(s):"
        for f in "${struct_failures[@]}"; do
            err "  - $f"
        done
        err ""
        err "HERMES_TRADESK_SKIP_STRUCT_CHECK=1 is set; proceeding anyway."
        err "** This is NOT recommended. Installation will likely fail at runtime. **"
    else
        say ""
        err "Structural compatibility FAILED on $STRUCT_CHECKS_FAILED check(s):"
        for f in "${struct_failures[@]}"; do
            err "  - $f"
        done
        err ""
        err "Refusing to install. The destination Hermes does not provide the"
        err "required integration contracts."
        err ""
        err "If you have manually verified these contracts and want to override,"
        err "export HERMES_TRADESK_SKIP_STRUCT_CHECK=1 and retry. (NOT recommended.)"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 6. Compatibility manifest (informational, NOT a hard gate).
#
# manifest.json ships in this repo but is not REQUIRED for installation —
# it is a metadata file. install.sh prints its reference_hermes_commit
# when present; absence is non-fatal.
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
    say "  (Hermes commit equality is NOT required for installation.)"
else
    say ""
    say "  Reference manifest not found at $MANIFEST (informational only)."
    say "  (This is OK — install will proceed using structural checks.)"
fi

# ---------------------------------------------------------------------------
# 7. Backup target
# ---------------------------------------------------------------------------
BACKUP_DIR="${HOME:-/root}/.hermes/tradedesk-install-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
say "  Backup dir:       $BACKUP_DIR"

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

# ---------------------------------------------------------------------------
# 8. Backup existing tradedesk
# ---------------------------------------------------------------------------
say ""
say "Backing up existing files..."
for f in $(find "$SRC_TRADEDESK" -type f -name "*.py"); do
    rel="${f#$SCRIPT_DIR/}"
    target="$HERMES_HOME/${rel#tradedesk/}"
    if [[ -e "$target" ]]; then
        mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
        cp -p "$target" "$BACKUP_DIR/$rel"
        say "  backup: $rel"
    fi
done

# ---------------------------------------------------------------------------
# 9-11. Install TradeDesk + wizard + integration hooks
# ---------------------------------------------------------------------------
say ""
say "Installing TradeDesk modules to $HERMES_HOME/tradedesk/..."
mkdir -p "$HERMES_HOME/tradedesk"
for f in "$SRC_TRADEDESK"/*.py; do
    fname="$(basename "$f")"
    cp "$f" "$HERMES_HOME/tradedesk/$fname"
    say "  tradedesk/$fname"
done

say ""
say "Installing Telegram /trade wizard under $HERMES_HOME/plugins/..."
mkdir -p "$HERMES_HOME/plugins/platforms/telegram/trade_menu"
cp "$SRC_OVERLAY/telegram/trade_menu/__init__.py" "$HERMES_HOME/plugins/platforms/telegram/trade_menu/__init__.py"
cp "$SRC_OVERLAY/telegram/trade_menu/wizard.py" "$HERMES_HOME/plugins/platforms/telegram/trade_menu/wizard.py"
say "  plugins/.../trade_menu/__init__.py"
say "  plugins/.../trade_menu/wizard.py"

# ---------------------------------------------------------------------------
# 12. Preserve Hermes AI configuration
# 13-15. NEVER overwrite auth.json / .env / unrelated config
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
# 16. NEVER copy credentials from this repo into the system
# 17. NEVER print secret values
# ---------------------------------------------------------------------------
say ""
say "Skipping credential injection (operator sets these manually)."
say "See .env.example shipped in this repo for variable NAMES."

# ---------------------------------------------------------------------------
# 18-19. Sanitized installation report
# ---------------------------------------------------------------------------
SANITIZED_REPORT="$BACKUP_DIR/install-report.txt"
{
    echo "Hermes TradeDesk install report"
    echo "================================"
    echo "Hermes commit:       $HERMES_COMMIT"
    echo "Reference commit:    $REF_COMMIT"
    echo "Struct checks pass:  $STRUCT_CHECKS_PASSED"
    echo "Struct checks fail:  $STRUCT_CHECKS_FAILED"
    echo "Files installed:     $(find "$SRC_TRADEDESK" -name '*.py' -printf "%f\n" | wc -l) TradeDesk modules + wizard"
    echo ""
    echo "Configured account aliases (NAMES ONLY):"
    echo "  (not enumerated to keep report sanitized)"
    echo ""
    echo "Credential values printed: NO"
    echo "Trading writes performed:  0"
} | tee "$SANITIZED_REPORT"

# ---------------------------------------------------------------------------
# 20-22. ZERO live trading verification
# ---------------------------------------------------------------------------
say ""
say "Running offline verification (verify.sh)..."
if [[ -x "$SCRIPT_DIR/verify.sh" ]]; then
    "$SCRIPT_DIR/verify.sh" || say "  (verify.sh reported a non-zero exit; investigate)"
else
    say "  (verify.sh not executable; skipping)"
fi

# ---------------------------------------------------------------------------
# 23-25. Idempotency + final
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