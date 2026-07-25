"""Hermes TradeDesk — TradeDesk core package.

This package contains the production TradeDesk implementation copied
verbatim from a frozen DigitalOcean Hermes release (commit 49f8f54 in
the source tree; production commit captured in manifest.json).

Modules:
    router              - exchanges <-> agent routing
    tradedesk           - main TradeDesk facade
    request_utils       - request field helpers
    account_discovery   - per-exchange env-var credential scanning
    exchange agents     - one per supported exchange
"""
