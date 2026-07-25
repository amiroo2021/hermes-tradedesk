# Hermes TradeDesk

A **portable /trade extension** for
[Hermes Agent](https://github.com/NousResearch/hermes-agent), providing
Telegram-based multi-exchange trading through `TradeDesk` and
per-exchange agents.

This package was extracted from a frozen production Hermes install
(commit `49f8f548bf7df7c9bd63765e1807ea2e9d2def36`) and repackaged as
a portable, standalone distribution that can be installed on any fresh
Hermes server.

---

## Architecture

```
Telegram /trade
        ↓
Trade Wizard (plugins/.../wizard.py)
        ↓
TradeDesk (tradedesk/tradedesk.py)
        ↓
TradeDeskRouter (tradedesk/router.py)
        ↓
Exchange Agent (one per supported exchange)
        ├── hyperliquid_agent.py
        ├── lighter_agent.py
        ├── raydium_agent.py + raydium_write.py
        ├── pacifica_agent.py + pacifica_tpsl.py
        ├── afx_agent.py
        ├── apex_agent.py
        └── rise_agent.py (EIP-712 typed-data for TP/SL)
        ↓
Exchange API / SDK
```

## Supported exchanges

| Exchange | account discovery | balance | positions | open orders |
| --------: | :----------------: | :------: | :--------: | :----------: |
| Hyperliquid | yes | yes | yes | yes |
| Lighter    | yes | yes | yes | yes |
| Raydium    | yes | yes | yes | yes |
| Pacifica   | yes | yes | yes | yes |
| AFX        | yes | yes | yes | yes |
| ApeX       | yes | yes | yes | yes |
| Rise       | yes | yes | yes | yes |

| Exchange | limit orders | market orders | ladder | cancel | grouped cancel | TP/SL |
| --------: | :-----------: | :------------: | :----: | :----: | :-------------: | :---: |
| Hyperliquid | yes | yes | yes | yes | yes | yes |
| Lighter    | yes | yes | yes | yes | yes | yes |
| Raydium    | yes | (limited) | yes | yes | yes | yes (single op via set_tp/set_sl with structured_request) |
| Pacifica   | yes | yes | yes | yes | yes | yes |
| AFX        | yes | yes | yes | yes | yes | yes |
| ApeX       | yes | yes | yes | yes | yes | yes |
| Rise       | yes | partial | (best-effort via batch_orders on Raydium shares helper) | yes | yes | yes (PlaceTpslOrder via EIP-712) |

> Capabilities above are reported ONLY from the frozen production source.
> They were not extended by this packaging extraction.

## Prerequisites

- A host running Linux (Ubuntu 24+ recommended for the new server).
- An existing, working Hermes install (your `hermes` command works).
- Telegram bot token + chat configuration handled by Hermes (not by
  this package).

## Installation

```sh
git clone https://github.com/amiroo2021/hermes-tradedesk.git
cd hermes-tradedesk
sudo ./install.sh
./verify.sh
```

The installer is conservative and self-gates:

- Detects `/usr/local/lib/hermes-agent` and `/usr/local/bin/hermes`.
- Reads the destination Hermes commit and compares it against
  `manifest.json`. Refuses to install on an unknown or mismatched Hermes
  version.
- Backs up every file it modifies into
  `~/.hermes/tradedesk-install-backups/<UTC-timestamp>/`.
- Installs TradeDesk modules and the Telegram wizard overlay.
- Performs ZERO live trading actions.

### ⚠️ Credentials

After install, you (the operator) must put your real exchange credentials
in `~/.hermes/.env` on the host machine. This package ships
`.env.example` — fill that in with your credentials.

NEVER commit `.env` or `~/.hermes/auth.json` to Git. This package's
`.gitignore` blocks them explicitly.

## Configuration

See `.env.example` for the canonical set of variable names. Account
aliases (`EXAMPLE`, `ACCOUNT1`, ...) are placeholders — replace them
with your own aliases.

```sh
# Example: rise
RISE_MYALIAS_WALLET=0x...
RISE_MYALIAS_APISIGNERPRIVATE=...

# Example: raydium
RAYDIUM_MYALIAS_ACCOUNT_ID=...
RAYDIUM_MYALIAS_API_KEY=...
RAYDIUM_MYALIAS_SECRET_KEY=...
```

After editing `~/.hermes/.env`:

```sh
systemctl --user restart hermes-gateway.service
```

## Verification

`./verify.sh` confirms the install is structurally correct. It:

- Imports each module to check the package loads.
- Confirms `.env.example` has only empty placeholder values.
- Reports `Trading writes performed: 0`.
- **Does not** place or cancel any order.

## Updating

To pull the latest compatible TradeDesk source:

```sh
cd hermes-tradedesk
git pull
sudo ./install.sh
./verify.sh
```

## Uninstall / Rollback

Backups are retained at:

```
~/.hermes/tradedesk-install-backups/<UTC-timestamp>/
```

To roll back:

```sh
sudo cp -rp ~/.hermes/tradedesk-install-backups/<timestamp>/trades_desk/* \
    /usr/local/lib/hermes-agent/trades_desk/
sudo cp -rp ~/.hermes/tradedesk-install-backups/<timestamp>/plugins/platforms/telegram/trade_menu/* \
    /usr/local/lib/hermes-agent/plugins/platforms/telegram/trade_menu/
sudo systemctl --user restart hermes-gateway.service
```

## Security Model

See SECURITY.md.

## Troubleshooting

`./install.sh` refuses to install:
- Confirm your Hermes commit is in `manifest.json` `compatible_hermes_commits`.
- If necessary, upgrade Hermes source to a compatible commit first, then
  retry.

`./verify.sh` reports failures:
- Check that `/usr/local/lib/hermes-agent/venv/bin/python` exists.
- Re-run `sudo ./install.sh`.

Open Positions shows `• None`:
- Confirm `/v1/positions` is reachable from the host.

TP/SL writes fail with `RISE_INVALID_STOP_PRICE`:
- Mark price below your stop for LONG positions, above for SHORT. The
  agent enforces this before broadcasting.

## Compatibility

This package uses **structural** compatibility checks rather than requiring
an exact Hermes commit. `install.sh` verifies the destination Hermes
provides the integration contracts our code depends on:

- `plugins/platforms/telegram/shared_selectors.py` exports
  `account_keyboard`, `account_prompt`, `exchange_keyboard`,
  `exchange_prompt`, and `lighter_account_keyboard`.
- `plugins/platforms/telegram/_positions_render.py` exists.
- The `hermes_cli` Python package is importable.
- Required Python dependencies are present: `eth_account`, `eth_abi`,
  `eth_utils`, `cryptography`, `base58`, `requests`, `solders`, `lighter`,
  and the `telegram` package.

If any contract is missing, `install.sh` refuses installation. The
`manifest.json` field `reference_hermes_commit` records the historical
commit this package was built against (`49f8f548...`); it is
**informational**, not a hard gate.

### Why structural rather than SHA-equality?

A SHA-equality check would lock the package to a single historical
Hermes commit, preventing fresh Ubuntu installations from using it
without first rebuilding Hermes from that exact commit. The structural
check allows a fresh Hermes install (Ubuntu 24, Kamatera, any standard
Hermes) to receive the /trade capability as long as it provides the
required integration contracts.

## Reference implementation

`reference_hermes_commit` in `manifest.json` records the production
commit this package was extracted from:

```
49f8f548bf7df7c9bd63765e1807ea2e9d2def36
```

The structural compatibility check makes it possible to use this
package with newer or compatible Hermes versions as long as they
provide the integration contracts above.

## Development

The package contains only frozen production code from the reference
implementation. No development-time modification is expected. To extend:

1. Bump the source reference commit in `manifest.json`.
2. Add tests in `tests/test_public_portable.py`.
3. Update `.env.example`, `README.md`, `SECURITY.md`, and this file.
4. Re-run `./verify.sh` to confirm the new layout.

## License

MIT License (compatible with upstream Hermes Agent). See `LICENSE`.
