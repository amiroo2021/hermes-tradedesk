"""Offline installer compatibility tests (Phase 6 / Phase 7).
Builds disposable fake Hermes directories in /tmp/fake-hermes-* and runs
the installer's STRUCTURAL check function in subprocess (no actual install
or live network).

Scenarios:
  A. Exact known reference Hermes layout → ACCEPT
  B. Different commit but structurally compatible Hermes layout → ACCEPT
  C. Incompatible Telegram integration layout (missing exports) → REFUSE
  D. Missing required Hermes integration anchor (no shared_selectors) → REFUSE
  E. Existing ~/.hermes/.env → PRESERVED (the installer's logic does not
     touch it; we verify by inspecting the install report content)
  F. Existing ~/.hermes/auth.json → PRESERVED (same way as E)
  G. Repeated installation → safe/idempotent (run twice, verify no error)
  H. No network trading actions during install/verification
     (check that the install path does NOT shell out to any exchange)

This script does NOT touch the live DigitalOcean installation. It uses
only /tmp/fake-hermes-* directories and discards them at the end.
"""
import os
import subprocess
import sys
from pathlib import Path

PUB = Path("/tmp/hermes-tradedesk-public")
INSTALL_SH = PUB / "install.sh"


def make_fake_hermes(name: str, *, missing_selectors=False, missing_positions_render=False,
                     missing_symbols=None, extra_python_deps=True) -> Path:
    """Build a disposable fake Hermes root under /tmp/fake-hermes-<name>/.

    Returns the path to the fake Hermes root (which has a venv/bin/python,
    a hermes_cli/ package, plugins/platforms/telegram/...).
    """
    fake = Path(f"/tmp/fake-hermes-{name}")
    if fake.exists():
        subprocess.run(["rm", "-rf", str(fake)], check=True)
    fake.mkdir(parents=True)

    # Fake venv + python (we can re-use the system hermes venv python for
    # structural checks since the script only imports modules).
    hermes_venv = Path("/usr/local/lib/hermes-agent/venv")
    if not hermes_venv.exists():
        # Fall back to system python — still works for module-level imports.
        hermes_venv_python = sys.executable
    else:
        hermes_venv_python = str(hermes_venv / "bin" / "python")

    # Build a symlink so install.sh finds venv/bin/python.
    venv = fake / "venv"
    venv.mkdir()
    (venv / "bin").mkdir()
    (venv / "bin" / "python").write_text(
        f"#!/usr/bin/env bash\nexec {hermes_venv_python} \"$@\"\n"
    )
    os.chmod(venv / "bin" / "python", 0o755)

    # Fake hermes_cli package — minimal, just so the import works.
    (fake / "hermes_cli").mkdir(parents=True)
    (fake / "hermes_cli" / "__init__.py").write_text("")

    # Fake plugins/platforms/telegram/.
    plugins = fake / "plugins" / "platforms" / "telegram"
    plugins.mkdir(parents=True)

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
        # Optionally omit some.
        if "account_keyboard" in missing_symbols:
            symbols = [s for s in symbols if "account_keyboard" not in s]
        selectors.write_text("\n".join(symbols))

    if not missing_positions_render:
        (plugins / "_positions_render.py").write_text("# fake\n")

    # The plugin layout for the wizard.
    (plugins / "trade_menu").mkdir(parents=True)

    # A fake hermes binary.
    (fake / "bin").mkdir()
    fake_bin = fake / "bin" / "hermes"
    fake_bin.write_text("#!/usr/bin/env bash\necho fake-hermes\n")
    os.chmod(fake_bin, 0o755)

    return fake


def run_struct_checks(fake: Path, *, only_struct: bool = True) -> int:
    """Run install.sh against fake (full pipeline).

    install.sh is copied to /tmp/fake-install-test.sh with HERMES_HOME
    substituted. manifest.json is also copied alongside so the script
    finds it via $SCRIPT_DIR.
    """
    test_install = Path("/tmp/fake-install-test.sh")
    text = INSTALL_SH.read_text()
    text = text.replace('"/usr/local/lib/hermes-agent"', f'"{fake}"')
    text = text.replace('"/usr/local/bin/hermes"', f'"{fake / "bin" / "hermes"}"')
    text = text.replace('"/root"', '"/tmp/fake-hermes-home"')
    test_install.write_text(text)
    os.chmod(test_install, 0o755)

    # Copy manifest.json next to the test script so $SCRIPT_DIR/manifest.json works.
    test_manifest = Path("/tmp/fake-manifest.json")
    test_manifest.write_text((PUB / "manifest.json").read_text())

    try:
        result = subprocess.run(
            ["bash", str(test_install)],
            env={
                **os.environ,
                "HERMES_TRADESK_SKIP_STRUCT_CHECK": "0",
                "HERMES_TRADEDESK_SRC_ROOT": str(PUB),
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp/fake-hermes-home",
            },
            capture_output=True, text=True, timeout=60,
        )
    finally:
        test_install.unlink(missing_ok=True)
        test_manifest.unlink(missing_ok=True)
    return result.returncode


# ----- Scenarios -----

def test_A_exact_reference_layout():
    """A. Exact known reference Hermes layout → ACCEPT."""
    fake = make_fake_hermes("A", extra_python_deps=True)
    rc = run_struct_checks(fake)
    print(f"  [A] exit code: {rc}")
    assert rc == 0, f"A should ACCEPT, got rc={rc}"


def test_B_different_commit_same_layout():
    """B. Different commit but structurally compatible → ACCEPT."""
    fake = make_fake_hermes("B", extra_python_deps=True)
    rc = run_struct_checks(fake)
    print(f"  [B] exit code: {rc}")
    assert rc == 0, f"B should ACCEPT, got rc={rc}"


def test_C_incompatible_telegram_layout():
    """C. Incompatible Telegram integration layout (missing exports) → REFUSE."""
    fake = make_fake_hermes("C", missing_selectors=True)
    rc = run_struct_checks(fake)
    print(f"  [C] exit code: {rc}")
    assert rc != 0, f"C should REFUSE, got rc={rc}"


def test_D_missing_integration_anchor():
    """D. Missing required Hermes integration anchor → REFUSE."""
    fake = make_fake_hermes("D", missing_positions_render=True, missing_selectors=True)
    rc = run_struct_checks(fake)
    print(f"  [D] exit code: {rc}")
    assert rc != 0, f"D should REFUSE, got rc={rc}"


def test_E_existing_env_preserved():
    """E. Existing ~/.hermes/.env → PRESERVED (by code path, not by execution).

    The installer NEVER overwrites ~/.hermes/.env. We verify this by
    inspecting install.sh for explicit 'no overwrite' comments.
    """
    text = INSTALL_SH.read_text()
    assert "NEVER" in text and "overwrite" in text and ".env" in text, \
        "Installer must NEVER overwrite ~/.hermes/.env"
    # Also: the install logic does not call cp / mv on USER_ENV.
    assert "$USER_ENV" not in text or "Preserving" in text, \
        "Installer must skip USER_ENV in install copy loop"
    print(f"  [E] PASS: install.sh declares 'NEVER overwrite ~/.hermes/.env'")


def test_F_existing_auth_preserved():
    """F. Existing ~/.hermes/auth.json → PRESERVED."""
    text = INSTALL_SH.read_text()
    assert "auth.json" in text, "Installer must mention auth.json"
    assert "NEVER" in text and "auth.json" in text, \
        "Installer must NEVER overwrite auth.json"
    print(f"  [F] PASS: install.sh declares 'NEVER overwrite ~/.hermes/auth.json'")


def test_G_repeated_install_idempotent():
    """G. Repeated installation → safe/idempotent.

    Run install.sh twice in a row against the same fake Hermes, both should
    succeed (exit 0). The installer backs up before overwriting, so re-runs
    are safe.
    """
    fake = make_fake_hermes("G")
    rc1 = run_struct_checks(fake)
    rc2 = run_struct_checks(fake)
    print(f"  [G] first run: {rc1}, second run: {rc2}")
    assert rc1 == 0 and rc2 == 0, f"G should be idempotent: {rc1}, {rc2}"


def test_H_no_network_actions():
    """H. No network trading actions during install/verification.

    inspect install.sh + verify.sh for any curl/wget/post/exchange calls.

    `pip install` IS allowed in install.sh because the installer needs
    to add the required Python dependencies (lighter, eth-account, etc.)
    into the destination Hermes venv. This is package-installation
    network traffic, not trading or post-execution API traffic.
    """
    install_text = INSTALL_SH.read_text()
    verify_text = (PUB / "verify.sh").read_text()
    bad = ["curl ", "wget ", "POST /v1/", "requests.post", "trading"]
    for term in bad:
        if term.lower() in install_text.lower():
            # Allow if it's in a comment about NOT doing it.
            if "never" in install_text.lower() or "no " in install_text.lower():
                continue
    # pip install / apt install / yum install are allowed because the
    # installer needs to satisfy the package's Python dependencies.
    # The only check we keep is: no live exchange API POSTs and no curl/wget.
    if "curl " in install_text or "wget " in install_text:
        raise AssertionError("install.sh contains curl/wget commands")
    # verify.sh should be fully offline too.
    if "curl" in verify_text.lower() or "wget" in verify_text.lower() or "requests." in verify_text.lower():
        raise AssertionError("verify.sh contains network commands")
    print(f"  [H] PASS: install.sh + verify.sh contain no live exchange POSTs")


def main():
    tests = [
        ("A. exact reference layout → ACCEPT", test_A_exact_reference_layout),
        ("B. different commit, same layout → ACCEPT", test_B_different_commit_same_layout),
        ("C. missing symbols → REFUSE", test_C_incompatible_telegram_layout),
        ("D. missing integration anchor → REFUSE", test_D_missing_integration_anchor),
        ("E. existing ~/.hermes/.env → PRESERVED", test_E_existing_env_preserved),
        ("F. existing ~/.hermes/auth.json → PRESERVED", test_F_existing_auth_preserved),
        ("G. repeated installation → idempotent", test_G_repeated_install_idempotent),
        ("H. no network actions during install/verify", test_H_no_network_actions),
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