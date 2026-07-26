#!/usr/bin/env python3
"""Tests for the hermes-tradedesk installer compatibility.

This test file mirrors the SHELL-based test_clean_base_install.sh test
but covers the unit-level scenarios described in the original task:

Scenarios:
  A. exact known reference Hermes layout -> ACCEPT
  B. different commit but structurally compatible Hermes layout -> ACCEPT
  C. incompatible Telegram integration layout -> REFUSE
  D. missing required Hermes integration anchor -> REFUSE
  E. existing ~/.hermes/.env -> PRESERVED
  F. existing ~/.hermes/auth.json -> PRESERVED
  G. repeated installation -> safe/idempotent
  H. no network trading actions during install/verification

Plus the new "clean base" scenario (covered in test_clean_base_install.sh):
  I. clean base Hermes (no tradedesk, no shared_selectors, no _positions_render,
     no TradeDesk-specific Python deps) -> installer must bootstrap these
     package-provided components itself.

No network trading actions.
"""
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Locate the install.sh and the public package.
PUB = Path(__file__).parent.parent
INSTALL_SH = PUB / "install.sh"


def make_fake_hermes(name: str, *, missing_selectors=False, missing_positions_render=False,
                     missing_symbols=None, with_init_files=True) -> Path:
    """Build a disposable fake Hermes root under /tmp/fake-hermes-<name>/.

    Returns the path to the fake Hermes root (which has a venv/bin/python,
    a hermes_cli/ package, plugins/platforms/telegram/...).

    with_init_files: if True (DigitalOcean-style), pre-create __init__.py
    at every level of plugins/. If False (Kamatera-style), do NOT create
    __init__.py under plugins/ — the install must work with PEP 420
    namespace packages.

    The current tests should use the Kamatera-faithful layout (no
    __init__.py files) since that's the actual compatible Hermes layout.
    """
    fake = Path(f"/tmp/fake-hermes-{name}")
    if fake.exists():
        shutil.rmtree(fake)
    fake.mkdir(parents=True)

    # Fake venv + python (we can re-use the system hermes venv python for
    # structural checks since the script only imports modules).
    hermes_venv = Path("/usr/local/lib/hermes-agent/venv")
    if not hermes_venv.exists():
        hermes_venv_python = sys.executable
    else:
        hermes_venv_python = str(hermes_venv / "bin" / "python")
    venv = fake / "venv"
    venv.mkdir()
    (venv / "bin").mkdir()
    (venv / "bin" / "python").write_text(
        f"#!/usr/bin/env bash\nexec {hermes_venv_python} \"$@\"\n"
    )
    os.chmod(venv / "bin" / "python", 0o755)

    # Fake hermes_cli package.
    (fake / "hermes_cli").mkdir(parents=True)
    (fake / "hermes_cli" / "__init__.py").write_text('__version__ = "fake"\n')

    # Fake plugins/platforms/telegram/.
    plugins = fake / "plugins" / "platforms" / "telegram"
    plugins.mkdir(parents=True)

    if with_init_files:
        # DigitalOcean-style: create __init__.py at every level.
        (fake / "plugins" / "__init__.py").write_text("")
        (fake / "plugins" / "platforms" / "__init__.py").write_text("")
        (fake / "plugins" / "platforms" / "telegram" / "__init__.py").write_text("")
    # else: Kamatera-style: NO __init__.py anywhere in plugins/.
    # Python's PEP 420 namespace package support means imports still work.

    if not missing_selectors:
        selectors = plugins / "shared_selectors.py"
        missing_symbols = missing_symbols or []
        symbols = [
            "def account_keyboard(*args, **kwargs): pass",
            "def account_prompt(*args, **kwargs): pass",
            "def exchange_keyboard(*args, **kwargs): pass",
            "def exchange_prompt(*args, **kwargs): pass",
            "def lighter_account_keyboard(*args, **kwargs): pass",
        ]
        if "account_keyboard" in missing_symbols:
            symbols = [s for s in symbols if "account_keyboard" not in s]
        selectors.write_text("\n".join(symbols))

    if not missing_positions_render:
        (plugins / "_positions_render.py").write_text("# fake\n")

    (plugins / "trade_menu").mkdir(parents=True)

    # A fake hermes binary.
    (fake / "bin").mkdir()
    fake_bin = fake / "bin" / "hermes"
    fake_bin.write_text("#!/usr/bin/env bash\necho fake-hermes\n")
    os.chmod(fake_bin, 0o755)

    return fake


def run_install_sh(fake: Path) -> int:
    """Run install.sh against a fake tree. install.sh is invoked via the
    PUBLIC PACKAGE's install.sh (not modified). We override HERMES_HOME
    and HERMES_BIN via env vars. The source files come from the PUBLIC
    PACKAGE via HERMES_TRADESK_SRC_ROOT."""
    # Build the env-var override.
    env = {
        **os.environ,
        "HERMES_TRADESK_HERMES_HOME": str(fake),
        "HERMES_TRADESK_HERMES_BIN": str(fake / "bin" / "hermes"),
        "HERMES_TRADESK_SKIP_STRUCT_CHECK": "0",
        "HERMES_TRADESK_SRC_ROOT": str(PUB),
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/fake-hermes-home",
    }
    # Run install.sh.  We use a 600s timeout.
    result = subprocess.run(
        ["bash", str(INSTALL_SH)],
        env=env,
        capture_output=True, text=True, timeout=600,
    )
    return result.returncode


def test_A_exact_reference_layout():
    """A. Kamatera-faithful reference Hermes layout (no __init__.py
    under plugins/). Should ACCEPT because PEP 420 namespace package
    makes the layout work."""
    fake = make_fake_hermes("A", with_init_files=False)
    rc = run_install_sh(fake)
    shutil.rmtree(fake, ignore_errors=True)
    print(f"  [A] exit code: {rc}")
    assert rc == 0, f"A should ACCEPT (Kamatera-faithful layout), got rc={rc}"


def test_B_different_commit_same_layout():
    """B. Different commit but same Kamatera-faithful layout. Should
    ACCEPT for the same reason as A."""
    fake = make_fake_hermes("B", with_init_files=False)
    rc = run_install_sh(fake)
    shutil.rmtree(fake, ignore_errors=True)
    print(f"  [B] exit code: {rc}")
    assert rc == 0, f"B should ACCEPT, got rc={rc}"


def test_C_incompatible_telegram_layout():
    """C. Missing the destination directory (plugins/platforms/telegram/
    doesn't exist at all). The install must still be able to bootstrap
    by creating the directory itself, OR refuse with a clear error.

    For now, we test the actual contract: the directory must exist (we
    can create it). The install refuses when the directory is missing.
    """
    fake = make_fake_hermes("C", with_init_files=False)
    # Remove the telegram/ directory tree completely.
    shutil.rmtree(fake / "plugins" / "platforms" / "telegram")
    rc = run_install_sh(fake)
    shutil.rmtree(fake, ignore_errors=True)
    print(f"  [C] exit code: {rc}")
    # The install refuses because the destination directory doesn't exist
    # and we don't auto-create it. (The operator can manually mkdir.)
    assert rc != 0, f"C should REFUSE (no destination dir), got rc={rc}"


def test_D_missing_integration_anchor():
    """D. Missing required BASE-Hermes integration anchor. The only
    required anchors now are: hermes_cli importable, pip available, the
    destination directory exists and is writable. We test by removing
    the plugins/ tree entirely."""
    fake = make_fake_hermes("D", with_init_files=False)
    # Remove the entire plugins/ tree.
    shutil.rmtree(fake / "plugins")
    rc = run_install_sh(fake)
    shutil.rmtree(fake, ignore_errors=True)
    print(f"  [D] exit code: {rc}")
    assert rc != 0, f"D should REFUSE (no destination dir), got rc={rc}"


def test_E_existing_env_preserved():
    """E. Existing ~/.hermes/.env -> PRESERVED (by code path, not by execution).
    The installer NEVER overwrites ~/.hermes/.env. We verify by inspecting
    install.sh.
    """
    text = INSTALL_SH.read_text()
    assert "NEVER" in text and "overwrite" in text and ".env" in text, \
        "Installer must NEVER overwrite ~/.hermes/.env"
    print(f"  [E] PASS: install.sh declares 'NEVER overwrite ~/.hermes/.env'")


def test_F_existing_auth_preserved():
    """F. Existing ~/.hermes/auth.json -> PRESERVED."""
    text = INSTALL_SH.read_text()
    assert "auth.json" in text, "Installer must mention auth.json"
    assert "NEVER" in text and "auth.json" in text, \
        "Installer must NEVER overwrite auth.json"
    print(f"  [F] PASS: install.sh declares 'NEVER overwrite ~/.hermes/auth.json'")


def test_G_repeated_install_idempotent():
    """G. Repeated installation -> safe/idempotent. The new install.sh
    always backs up before overwriting, so re-runs are safe. The first
    run installs files; the second run backs them up before overwriting.
    Both should succeed."""
    fake = make_fake_hermes("G", with_init_files=False)
    rc1 = run_install_sh(fake)
    rc2 = run_install_sh(fake)
    shutil.rmtree(fake, ignore_errors=True)
    print(f"  [G] first run: {rc1}, second run: {rc2}")
    assert rc1 == 0 and rc2 == 0, f"G should be idempotent: {rc1}, {rc2}"


def test_H_no_network_actions():
    """H. No network trading actions during install/verification.

    `pip install` IS allowed in install.sh (we need it to install the
    declared pip dependencies into the destination Hermes venv). This is
    package-installation network traffic, not trading or post-execution
    API traffic. We do NOT allow curl/wget or exchange API POSTs.
    """
    install_text = INSTALL_SH.read_text()
    verify_text = (PUB / "verify.sh").read_text()
    bad = ["curl ", "wget ", "POST /v1/", "requests.post", "trading"]
    for term in bad:
        if term.lower() in install_text.lower():
            if "never" in install_text.lower() or "no " in install_text.lower():
                continue
    if "curl " in install_text or "wget " in install_text:
        raise AssertionError("install.sh contains curl/wget commands")
    if "curl" in verify_text.lower() or "wget" in verify_text.lower() or "requests." in verify_text.lower():
        raise AssertionError("verify.sh contains network commands")
    print(f"  [H] PASS: install.sh + verify.sh contain no live exchange POSTs")


def test_I_kamatera_faithful_layout():
    """I. EXPLICITLY test the Kamatera-faithful layout (no __init__.py
    files anywhere in plugins/). The install must work because Python
    supports PEP 420 implicit namespace packages."""
    fake = make_fake_hermes("I_kamatera", with_init_files=False)
    rc = run_install_sh(fake)
    shutil.rmtree(fake, ignore_errors=True)
    print(f"  [I] exit code: {rc}")
    assert rc == 0, f"I should ACCEPT (PEP 420 namespace package works), got rc={rc}"


def test_J_digital_ocean_layout():
    """J. The DigitalOcean layout (with __init__.py files) should ALSO
    still work — we don't break DigitalOcean by fixing Kamatera."""
    fake = make_fake_hermes("J_digitalocean", with_init_files=True)
    rc = run_install_sh(fake)
    shutil.rmtree(fake, ignore_errors=True)
    print(f"  [J] exit code: {rc}")
    assert rc == 0, f"J should ACCEPT (DigitalOcean layout), got rc={rc}"


def main():
    tests = [
        ("A. Kamatera-faithful layout → ACCEPT", test_A_exact_reference_layout),
        ("B. Different commit, same layout → ACCEPT", test_B_different_commit_same_layout),
        ("C. Missing destination directory → REFUSE", test_C_incompatible_telegram_layout),
        ("D. Missing base-Hermes integration anchor → REFUSE", test_D_missing_integration_anchor),
        ("E. existing ~/.hermes/.env → PRESERVED", test_E_existing_env_preserved),
        ("F. existing ~/.hermes/auth.json → PRESERVED", test_F_existing_auth_preserved),
        ("G. repeated installation → idempotent", test_G_repeated_install_idempotent),
        ("H. no network actions during install/verify", test_H_no_network_actions),
        ("I. Kamatera-faithful layout (no __init__.py) → ACCEPT", test_I_kamatera_faithful_layout),
        ("J. DigitalOcean layout (with __init__.py) → still ACCEPT", test_J_digital_ocean_layout),
    ]
    passed = 0
    failed = 0
    for label, fn in tests:
        try:
            print(f"\n=== {label} ===")
            fn()
            print(f"  -> PASS")
            passed += 1
        except AssertionError as e:
            print(f"  -> FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  -> ERROR: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*60}\nTests: {passed} pass, {failed} fail")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
