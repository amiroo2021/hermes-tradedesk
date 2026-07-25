# Security Policy

## Credentials are NOT in this repository

This package ships only production **code**. No operator credentials are
included or required to install:

- `~/.hermes/.env` (your per-account exchange API keys, wallets, signers)
  stays on the host machine and is NEVER copied by the installer.
- `~/.hermes/auth.json` (your Hermes AI OAuth / Telegram bot token)
  stays on the host machine and is NEVER touched by the installer.

If you find `.env`, `auth.json`, or any real credential in a public
mirror of this repository, **report it confidentially**.

## Reporting a Vulnerability

Please open a private issue / security advisory with the maintainers
through the project's normal disclosure channel. Do NOT include
real credentials in any report.

## Installer Safety Guarantees

`./install.sh` is intentionally conservative:

1. Refuses to install if the destination Hermes version/commit is not
   in the package's `manifest.json` compatibility list.
2. **Always** backs up every file it modifies into
   `~/.hermes/tradedesk-install-backups/<UTC-timestamp>/` before
   replacement.
3. **Never** overwrites `~/.hermes/.env` or `~/.hermes/auth.json`.
4. **Never** copies credentials from this repo into your Hermes install.
5. **Never** prints secret values in any report or log.
6. Performs zero live trading actions (no orders, no cancellations,
   no test orders against real exchanges).
7. Idempotent: re-running replays the same steps safely after a fresh
   backup.

## Verify.sh Safety Guarantees

`./verify.sh` is read-only:

1. Imports modules to confirm the installation works.
2. Validates that `.env.example` contains only empty placeholder values.
3. Reports `Trading writes performed: 0`.
4. **Never** places a test order, even if env credentials are present.
5. **Never** prints secret values.

## Operator-Side Best Practices

To minimize blast radius on production, we recommend:

- Use a dedicated, minimum-permission API key per exchange account when
  the exchange supports key scoping.
- Restrict each key to the markets you actively trade.
- Use IP-restricted API keys where the exchange supports it.
- Rotate API keys after any operator change.
- Run `verify.sh` after every install/update to confirm structure.
- Consider testing with `price=0` for cancel sentinel before testing
  with real prices (Set TP/SL via `0` triggers the cancel flow without
  a live order).

## What this package does NOT do

- It does not contain live trading bots.
- It does not initiate any transfer, deposit, or withdrawal.
- It does not persist credentials across restarts.
- It does not phone home; verify.sh and install.sh both run entirely
  offline against the local filesystem.
