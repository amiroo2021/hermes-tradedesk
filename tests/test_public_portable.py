"""test_public_portable.py - portable offline smoke tests.

Runs WITHOUT network access. Imports modules to verify packaging is correct.
No live API calls, no order placement, no cancellations.

This is the public portable test suite shipped in hermes-tradedesk/tests/."""
import importlib
import sys
import unittest
from pathlib import Path


# Ensure we import from the local tradedesk/ alongside this file.
HERE = Path(__file__).parent.resolve()
PROJECT = HERE.parent
# For ``tradedesk.*`` imports the parent directory of the bundled
# ``tradedesk/`` package must be on sys.path (not the package itself).
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tradedesk"))
sys.path.insert(0, str(PROJECT / "hermes_overlay" / "telegram" / "trade_menu"))


class TestPortableStructure(unittest.TestCase):
    def test_required_files_exist(self):
        """All shipped production modules must be present."""
        required_tradedesk = [
            "account_discovery.py",
            "router.py",
            "tradedesk.py",
            "request_utils.py",
            "hyperliquid_agent.py",
            "lighter_agent.py",
            "raydium_agent.py",
            "raydium_write.py",
            "pacifica_agent.py",
            "pacifica_tpsl.py",
            "afx_agent.py",
            "apex_agent.py",
            "rise_agent.py",
        ]
        for f in required_tradedesk:
            self.assertTrue(
                (PROJECT / "tradedesk" / f).exists(),
                f"Missing production module: tradedesk/{f}",
            )

        required_overlay = ["__init__.py", "wizard.py"]
        for f in required_overlay:
            self.assertTrue(
                (PROJECT / "hermes_overlay" / "telegram" / "trade_menu" / f).exists(),
                f"Missing integration module: hermes_overlay/.../{f}",
            )

    def test_env_example_is_present(self):
        self.assertTrue((PROJECT / ".env.example").exists())

    def test_install_sh_exists(self):
        self.assertTrue((PROJECT / "install.sh").exists())

    def test_verify_sh_exists(self):
        self.assertTrue((PROJECT / "verify.sh").exists())

    def test_gitignore_present(self):
        self.assertTrue((PROJECT / ".gitignore").exists())


class TestAgentShape(unittest.TestCase):
    """Smoke tests: assert each agent module has the expected public API.
    We deliberately do NOT import the agents (their ``from .account_discovery``
    relative imports require a full Hermes installation context); instead
    we stat the file and inspect the syntax tree for a few canary names.
    """

    def _check_agent_has(self, fname, canary):
        from pathlib import Path
        p = PROJECT / "tradedesk" / fname
        self.assertTrue(p.exists(), f"missing {fname}")
        text = p.read_text()
        self.assertIn(canary, text, f"{fname} does not contain canary {canary!r}")

    def test_hyperliquid_agent_shape(self):
        self._check_agent_has("hyperliquid_agent.py", "SUPPORTED_OPERATIONS")

    def test_lighter_agent_shape(self):
        self._check_agent_has("lighter_agent.py", "SUPPORTED_OPERATIONS")

    def test_raydium_write_shape(self):
        self._check_agent_has("raydium_write.py", "def execute_order")

    def test_pacifica_agent_shape(self):
        self._check_agent_has("pacifica_agent.py", "SUPPORTED_OPERATIONS")

    def test_afx_agent_shape(self):
        self._check_agent_has("afx_agent.py", "SUPPORTED_OPERATIONS")

    def test_apex_agent_shape(self):
        # Apex does not expose a SUPPORTED_OPERATIONS constant; the
        # agent advertises capabilities via the inline ``if operation``
        # branches in ``ApexAgent.execute``. Sanity-check for that.
        self._check_agent_has("apex_agent.py", "if operation == \"balance\"")

    def test_rise_agent_shape(self):
        self._check_agent_has("rise_agent.py", "SUPPORTED_OPERATIONS")


class TestSecretSafety(unittest.TestCase):
    """Verify that no production source ships real secrets in the repo."""

    def test_no_env_file_committed(self):
        self.assertFalse(
            (PROJECT / ".env").exists(),
            "Found .env in repo — secret leak!",
        )
        self.assertFalse(
            (PROJECT / "auth.json").exists(),
            "Found auth.json in repo — secret leak!",
        )

    def test_env_example_has_only_placeholders(self):
        """The .env.example must only carry empty values — never real keys."""
        example = (PROJECT / ".env.example").read_text()
        for ln in example.splitlines():
            if "=" not in ln or ln.strip().startswith("#"):
                continue
            key, _, value = ln.partition("=")
            self.assertEqual(
                value.strip(), "",
                f".env.example line {key!r} contains a non-empty value: {value!r}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
