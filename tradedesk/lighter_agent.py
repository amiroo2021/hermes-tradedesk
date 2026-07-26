"""Lighter exchange agent (Phase 1: read-only authenticated balance).

This module implements the Hermes-side of the Lighter exchange
integration. Phase 1 is intentionally limited to:

    - account discovery with structured account/chain metadata
    - authenticated balance retrieval, where the chain (ARBITRUM or
      ROBINHOOD) is derived internally from LIGHTER_<account>_CHAIN
      in the operator's environment

No trading, no order placement, no TP/SL, no positions, no open-orders,
no cancel, no ladder, no leverage, no margin management, no write paths.

============================================================================
LIGHTER DEPLOYMENTS (CHAINS)
============================================================================

Lighter runs in two distinct production deployments. An account belongs
to exactly one of them and MUST be queried against the correct
deployment's API. Routing an account to the wrong deployment is
classified as a configuration error; there is no automatic fallback.

    * "ARBITRUM" — standard Lighter, connected through the Arbitrum L2.
                   Authenticated endpoints use the lighter-sdk's
                   SignerClient to mint a 10-minute bearer token via
                   CreateAuthToken, which derives the token from the API
                   private key (no transaction signing occurs).
                   Production base URL: https://mainnet.zklighter.elliot.ai
                   (from https://apidocs.lighter.xyz/docs/get-started)

    * "ROBINHOOD" — Lighter on the Robinhood chain. Same SDK
                   (``pip install lighter-sdk``), same
                   SignerClient-based auth scheme, same
                   DetailedAccounts response shape.
                   Production base URL: https://api.rh.lighter.xyz
                   (from https://apidocs.rh.lighter.xyz/docs/get-started)

Both deployments expose the same endpoint and response shape for the
authenticated account balance read:

    GET /api/v1/account?by=index&value=<account_index>
    Authorization: Bearer <token>

The agent stores an immutable per-chain configuration map
(``LIGHTER_CHAINS``) keyed by the canonical chain identifier
("ARBITRUM" / "ROBINHOOD"). The base URL is selected from that map;
it is NEVER inferred from the account name, account_index, public_key,
or from which endpoint returned HTTP 200. There is no automatic
fallback between chains — an ARBITRUM request is sent to the ARBITRUM
endpoint and a ROBINHOOD request is sent to the ROBINHOOD endpoint, period.

============================================================================
ENVIRONMENT VARIABLE FORMAT
============================================================================

Every Lighter credential block uses an explicit ``CHAIN`` variable to
declare which deployment the account belongs to::

    LIGHTER_<ACCOUNT>_CHAIN=ARBITRUM|ROBINHOOD
    LIGHTER_<ACCOUNT>_ACCOUNT_INDEX=<int>
    LIGHTER_<ACCOUNT>_APIKEY_INDEX=<int>
    LIGHTER_<ACCOUNT>_PUBLIC_KEY=<hex>
    LIGHTER_<ACCOUNT>_PRIVATE_KEY=<hex>

Examples::

    LIGHTER_EXAMPLE_CHAIN=ARBITRUM
    LIGHTER_EXAMPLE_ACCOUNT_INDEX=12345
    LIGHTER_EXAMPLE_APIKEY_INDEX=4
    LIGHTER_EXAMPLE_PUBLIC_KEY=...
    LIGHTER_EXAMPLE_PRIVATE_KEY=...

    LIGHTER_ACCOUNT1_CHAIN=ROBINHOOD
    LIGHTER_ACCOUNT1_ACCOUNT_INDEX=67890
    LIGHTER_ACCOUNT1_APIKEY_INDEX=4
    LIGHTER_ACCOUNT1_PUBLIC_KEY=...
    LIGHTER_ACCOUNT1_PRIVATE_KEY=...

The ``CHAIN`` variable is the single source of truth for the
deployment. There is no fallback or inference from the account name.

An account is configured only when **all five** of the above
variables are present. Partial blocks (e.g. missing ``CHAIN``, or
``CHAIN`` set but ``ACCOUNT_INDEX`` missing) are rejected at
discovery time. The credential variable names MUST NOT contain
``_ARB_`` or ``_RH_`` suffixes — those would route the account to
the wrong deployment.

Malformed values such as ``LIGHTER_AMIROO_ACCOUNT_INDEX==15702`` (a
leading ``=``) raise a clear configuration error. There is no silent
``lstrip('=')`` repair. The error message references the real
variable name (e.g. ``Malformed LIGHTER_AMIROO_ACCOUNT_INDEX:
value contains a leading '=' (got '=15702')``).

Phase 1 uses ``ACCOUNT_INDEX`` and ``APIKEY_INDEX`` for the
authenticated read path. ``PUBLIC_KEY`` is read for validation and
``PRIVATE_KEY`` is loaded by Phase 1 to mint the SignerClient
auth-token; it is never used for any write operation in this phase.

============================================================================
REQUEST FLOW
============================================================================

The wizard passes only the account identifier — no chain field::

    { "version": 1, "operation": "balance",
    "exchange": "lighter", "account": "example" }

LighterAgent reads ``LIGHTER_<account>_CHAIN`` internally and uses
that as the single source of truth for the deployment. There is no
possibility of an inconsistent (account, chain) pair being passed
through the wizard, because the wizard never carries a chain.

TradeDesk remains exchange-agnostic; it does not know about chains
and does not need to know. The request shape is unchanged for every
existing exchange.
"""
from __future__ import annotations

from decimal import InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Tuple

from .account_discovery import combined_casefold_env

# ``SignerClient`` is a sync wrapper around the lighter-signer native
# binary that mints short-lived bearer tokens for authenticated
# requests. We import it here so the auth-token path runs once per
# process; the HTTP layer itself is done via ``requests`` (see
# ``LighterHttpClient.account``).
from lighter import SignerClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Immutable per-chain configuration map.
# ---------------------------------------------------------------------------

LIGHTER_CHAINS: dict[str, dict[str, str]] = {
    "ARBITRUM": {
        "label": "Arbitrum",
        # ARB / standard Lighter production base URL.
        # Source: https://apidocs.lighter.xyz/docs/get-started
        "base_url": "https://mainnet.zklighter.elliot.ai",
    },
    "ROBINHOOD": {
        "label": "Robinhood",
        # Robinhood Lighter production base URL.
        # Source: https://apidocs.rh.lighter.xyz/docs/get-started
        "base_url": "https://api.rh.lighter.xyz",
    },
}
"""Immutable per-chain configuration.

Frozen at module import. Phase 1 supports exactly the two chains
that Lighter officially documents. New chains must be added here
explicitly and must be sourced from the corresponding official
Lighter documentation, never inferred from an HTTP probe or any
other heuristic.
"""


# ---------------------------------------------------------------------------
# Chain normalization
# ---------------------------------------------------------------------------

_CANONICAL_CHAINS: Tuple[str, ...] = tuple(sorted(LIGHTER_CHAINS.keys()))


def _normalize_chain(value: Any) -> Optional[str]:
    """Normalize a chain value to its canonical uppercase form.

    Accepts "ARBITRUM", "Arbitrum", "arbitrum", or "  Arbitrum  " (with
    surrounding whitespace). Returns the canonical uppercase form or
    ``None`` if the value is not a string, is empty, or does not match
    a known chain.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    if not candidate:
        return None
    if candidate in LIGHTER_CHAINS:
        return candidate
    return None


def _get_chain_config(chain: Any) -> Tuple[str, str]:
    """Look up the immutable (label, base_url) tuple for a chain.

    Returns ``(label, base_url)`` on success. Raises ``ValueError`` for
    any input that is not a known chain identifier. The function never
    returns a default and never falls back to one chain when the caller
    asked for another.
    """
    normalized = _normalize_chain(chain)
    if normalized is None:
        raise ValueError(f"Unknown Lighter chain: {chain!r}")
    cfg = LIGHTER_CHAINS[normalized]
    return cfg["label"], cfg["base_url"]


# ---------------------------------------------------------------------------
# Phase 1 surface
# ---------------------------------------------------------------------------

SUPPORTED_OPERATIONS: set[str] = {"balance", "positions", "open_orders", "order", "batch_orders", "cancel_orders", "set_tp", "set_sl"}


# ---------------------------------------------------------------------------
# Account discovery (structured)
# ---------------------------------------------------------------------------

_ACCOUNT_KEY_RE = re.compile(
    r"^lighter_(?P<account>[a-z0-9]+)_(?P<field>[a-z_]+)$", re.IGNORECASE,
)
_CHAIN_FIELDS: Tuple[str, ...] = ("CHAIN",)
_STANDARD_FIELDS: Tuple[str, ...] = (
    "ACCOUNT_INDEX", "APIKEY_INDEX", "PUBLIC_KEY", "PRIVATE_KEY",
)
_REQUIRED_FIELDS: Tuple[str, ...] = _STANDARD_FIELDS + _CHAIN_FIELDS  # all five


def _list_lighter_keys() -> list[tuple[str, str]]:
    """Enumerate every ``LIGHTER_<account>_<field>`` tuple present in
    the case-insensitive union of process env and Hermes ``.env``.

    The variable name MUST NOT contain ``_ARB_`` or ``_RH_`` tokens
    (those would route the account to the wrong deployment). The
    account and field are lowercased before returning.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for actual_key, _value, _source in combined_casefold_env().values():
        m = _ACCOUNT_KEY_RE.match(actual_key)
        if not m:
            continue
        account = m.group("account").lower()
        field = m.group("field").upper()
        if field not in _REQUIRED_FIELDS:
            continue
        key = (account, field)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


@dataclass(frozen=True)
class LighterAccount:
    """Structured account metadata for one (account, chain) pair.

    An account is considered configured only when ALL five required
    Phase 1 fields are present for the same ``account`` token. The
    ``chain`` field is the single source of truth for which Lighter
    deployment the account belongs to. Mixed blocks (the same
    account configured for two different chains) are explicitly
    rejected at discovery time so the agent never silently routes to
    the wrong deployment.
    """
    account: str
    chain: str

    def label(self) -> str:
        return f"{self.account} — {LIGHTER_CHAINS[self.chain]['label']}"


def _read_env_value(account: str, field: str) -> str:
    """Read a LIGHTER_<account>_<field> value from process env + Hermes .env.

    Returns the value exactly as it appears in the source, with no
    whitespace stripping and no character-level mutation. Raises
    ``ValueError`` if the key is missing, the value is empty, or the
    key contains ``_ARB_`` / ``_RH_`` tokens (which would route the
    account to the wrong deployment).
    """
    if not account or not isinstance(account, str):
        raise ValueError(f"Invalid Lighter account: {account!r}")
    if not field or not isinstance(field, str):
        raise ValueError(f"Invalid Lighter field: {field!r}")

    # Defensive: ensure the env var has no _ARB_ or _RH_ suffix in
    # ``field`` (the user has explicitly told us these are obsolete).
    if field in {"ARB_ACCOUNT_INDEX", "RH_ACCOUNT_INDEX",
                 "ARB_APIKEY_INDEX", "RH_APIKEY_INDEX",
                 "ARB_PUBLIC_KEY", "RH_PUBLIC_KEY",
                 "ARB_PRIVATE_KEY", "RH_PRIVATE_KEY"}:
        raise ValueError(
            f"Obsolete LIGHTER variable form: LIGHTER_{account.upper()}_{field}. "
            f"Use the unsuffixed form LIGHTER_{account.upper()}_<FIELD> "
            f"with a separate LIGHTER_{account.upper()}_CHAIN variable."
        )

    actual_key = f"lighter_{account.lower()}_{field.lower()}"
    env_map = combined_casefold_env()
    entry = env_map.get(actual_key.lower())
    if entry is None:
        raise ValueError(
            f"Missing LIGHTER_{account.upper()}_{field} in process env and Hermes .env"
        )
    _, value, _source = entry
    if not value or not value.strip():
        raise ValueError(
            f"Empty value for LIGHTER_{account.upper()}_{field}"
        )
    return value


def _validate_integer_field(name: str, raw: str) -> int:
    """Strict integer parser. Rejects leading/trailing whitespace,
    leading '=', non-decimal characters, and sign-only values."""
    if raw != raw.strip():
        raise ValueError(
            f"Invalid LIGHTER_{name.upper()}: value contains whitespace"
        )
    if raw.startswith("="):
        raise ValueError(
            f"Malformed LIGHTER_{name.upper()}: value contains a leading '=' "
            f"(got {raw!r})"
        )
    if raw.startswith("+") or raw.startswith("-"):
        raise ValueError(
            f"Invalid LIGHTER_{name.upper()}: expected a non-negative integer "
            f"(got {raw!r})"
        )
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid LIGHTER_{name.upper()}: {raw!r} is not a valid integer"
        ) from exc


def _validate_hex_field(name: str, raw: str, min_chars: int) -> str:
    """Strict hex parser. Rejects leading '=', leading '0x' is allowed
    but optional, requires at least ``min_chars`` hex characters."""
    if raw != raw.strip():
        raise ValueError(
            f"Invalid LIGHTER_{name.upper()}: value contains whitespace"
        )
    if raw.startswith("="):
        raise ValueError(
            f"Malformed LIGHTER_{name.upper()}: value contains a leading '=' "
            f"(got {raw!r})"
        )
    candidate = raw[2:] if raw.lower().startswith("0x") else raw
    if len(candidate) < min_chars:
        raise ValueError(
            f"Invalid LIGHTER_{name.upper()}: hex value too short "
            f"(got len={len(candidate)}, min={min_chars})"
        )
    try:
        int(candidate, 16)
    except ValueError as exc:
        raise ValueError(
            f"Invalid LIGHTER_{name.upper()}: {raw!r} is not valid hex"
        ) from exc
    return raw


def discover_lighter_accounts() -> list[LighterAccount]:
    """Discover (account, chain) pairs with complete Phase 1 credentials.

    Algorithm:
      1. Enumerate every ``LIGHTER_<account>_<field>`` tuple across
         process env and Hermes ``.env``.
      2. Filter to valid fields (CHAIN, ACCOUNT_INDEX, APIKEY_INDEX,
         PUBLIC_KEY, PRIVATE_KEY).
      3. Group by ``account``. For each group, require all five
         fields to be present.
      4. For each group, parse and normalize the CHAIN value. If
         CHAIN is missing, the group is rejected. If CHAIN is not
         one of the canonical chain identifiers, the group is
         rejected.
      5. The returned list contains one ``LighterAccount(account,
         chain)`` per valid group.
    """
    grouped: dict[str, set[str]] = {}
    for account, field in _list_lighter_keys():
        grouped.setdefault(account, set()).add(field)

    out: list[LighterAccount] = []
    for account, fields in sorted(grouped.items()):
        missing = set(_REQUIRED_FIELDS) - fields
        if missing:
            logger.warning(
                "Incomplete Lighter credentials for account=%s: missing=%s",
                account, sorted(missing),
            )
            continue
        # Parse + normalize chain.
        try:
            chain_raw = _read_env_value(account, "CHAIN")
        except ValueError as exc:
            logger.warning("Lighter chain resolution failed for %s: %s",
                           account, exc)
            continue
        chain = _normalize_chain(chain_raw)
        if chain is None:
            logger.warning(
                "Unsupported Lighter chain for account=%s: %r (must be one of %s)",
                account, chain_raw, list(_CANONICAL_CHAINS),
            )
            continue
        # Validate the rest of the standard fields. If any is malformed,
        # reject the whole account. We don't return partial data.
        try:
            _validate_integer_field(
                f"{account}_ACCOUNT_INDEX",
                _read_env_value(account, "ACCOUNT_INDEX"),
            )
            _validate_integer_field(
                f"{account}_APIKEY_INDEX",
                _read_env_value(account, "APIKEY_INDEX"),
            )
            _validate_hex_field(
                f"{account}_PUBLIC_KEY",
                _read_env_value(account, "PUBLIC_KEY"),
                min_chars=64,
            )
            _validate_hex_field(
                f"{account}_PRIVATE_KEY",
                _read_env_value(account, "PRIVATE_KEY"),
                min_chars=64,
            )
        except ValueError as exc:
            logger.warning(
                "Lighter credential validation failed for account=%s: %s",
                account, exc,
            )
            continue
        out.append(LighterAccount(account=account, chain=chain))
    return out


def _resolve_account_credentials(account: str) -> dict[str, Any]:
    """Resolve one account's Phase 1 credentials.

    Returns a dict with keys:

        - ``chain`` (str, canonical uppercase form)
        - ``account_index`` (int)
        - ``apikey_index`` (int)
        - ``public_key`` (str, raw — ``0x`` prefix preserved if present)
        - ``private_key`` (str, raw)

    The chain is the SINGLE SOURCE OF TRUTH: it is read from
    ``LIGHTER_<account>_CHAIN`` in the operator's environment. The
    caller cannot override it (there is no chain argument).

    On any missing or malformed field, raises ``ValueError`` with a
    clear configuration-error message referencing the real variable
    name. No silent repair.
    """
    if not account or not isinstance(account, str):
        raise ValueError(f"Invalid Lighter account: {account!r}")

    # Read the configured chain from .env / process env.
    try:
        chain_raw = _read_env_value(account, "CHAIN")
    except ValueError as exc:
        raise ValueError(
            f"Lighter account {account!r} is not properly configured: "
            f"{exc}"
        ) from exc
    canonical_chain = _normalize_chain(chain_raw)
    if canonical_chain is None:
        raise ValueError(
            f"LIGHTER_{account.upper()}_CHAIN is not one of the supported "
            f"chains (got {chain_raw!r}; expected one of {list(_CANONICAL_CHAINS)})"
        )

    # Read and validate the rest of the credential block.
    field_name = f"{account}_ACCOUNT_INDEX"
    account_index = _validate_integer_field(
        field_name, _read_env_value(account, "ACCOUNT_INDEX")
    )
    field_name = f"{account}_APIKEY_INDEX"
    apikey_index = _validate_integer_field(
        field_name, _read_env_value(account, "APIKEY_INDEX")
    )
    field_name = f"{account}_PUBLIC_KEY"
    public_key = _validate_hex_field(
        field_name, _read_env_value(account, "PUBLIC_KEY"), min_chars=64,
    )
    field_name = f"{account}_PRIVATE_KEY"
    private_key = _validate_hex_field(
        field_name, _read_env_value(account, "PRIVATE_KEY"), min_chars=64,
    )

    return {
        "chain": canonical_chain,
        "account_index": account_index,
        "apikey_index": apikey_index,
        "public_key": public_key,
        "private_key": private_key,
    }


# ---------------------------------------------------------------------------
# LighterHTTPError (Hermes-style structured error, no secrets)
# ---------------------------------------------------------------------------

class LighterHTTPError(RuntimeError):
    """Mirrors ``PacificaHTTPError``. ``message`` and ``diagnostics`` are
    sanitized: they never include private keys, full auth tokens, or
    sensitive headers.
    """

    def __init__(self, message: str, *, status: Optional[int] = None,
                 body: Any = None, diagnostics: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.diagnostics = diagnostics or {}


# ---------------------------------------------------------------------------
# HTTP client interface
# ---------------------------------------------------------------------------

class LighterHttpClient:
    """Thin wrapper over the Lighter SDK's authenticated GET path.

    ``base_url`` is the chain-specific production URL (selected by
    the agent from ``LIGHTER_CHAINS``). The client never infers the
    base URL from any other field. The SDK's ``SignerClient`` is used
    only to mint the bearer auth-token via
    ``create_auth_token_with_expiry`` — no signing is performed.
    """

    def __init__(self, *, base_url: str, account_index: int, api_key_index: int,
                 api_private_key: str, public_key: str) -> None:
        self._base_url = base_url
        self._account_index = account_index
        self._api_key_index = api_key_index
        self._api_private_key = api_private_key
        self._public_key = public_key
        self._signer = None
        self._api = None

    @property
    def base_url(self) -> str:
        return self._base_url
    def account(self, account_index: int) -> dict:
        """Authenticated ``GET /api/v1/account?by=index&value=<account_index>``.

        Synchronous from start to end. We deliberately bypass the
        lighter-sdk's async HTTP wrapper (``AccountApi.account``,
        ``ApiClient.call_api``, aiohttp) because TradeDesk is a
        synchronous caller and we are typically invoked from inside
        the Telegram / PTB asyncio loop, where spawning a fresh event
        loop with ``asyncio.run(...)`` is illegal.

        Authentication (mints the 10-minute bearer token) is still
        performed by the official lighter-sdk's ``SignerClient`` —
        that call is synchronous and remains the source of truth.

        The authenticated HTTP GET itself is a small, well-defined
        shape: a single ``GET`` against ``{base_url}/api/v1/account``
        with two query parameters (``by=index``, ``value=<idx>``) and
        the canonical ``?auth=<token>`` query parameter carrying the
        auth-token issued by ``create_auth_token_with_expiry``. We
        issue that with the ``requests`` package (already in the venv)
        rather than the SDK's async aiohttp client.

        The ``?auth=<token>`` format is documented at
        https://apidocs.lighter.xyz/docs/api-keys (Lighter SDK auth
        tokens are sent as ``?auth=...`` query parameters, not as
        ``Authorization: Bearer`` headers, contrary to what the SDK's
        auth helper class suggests). The ``Authorization: Bearer``
        form yields HTTP 401 from the ARBITRUM deployment; the
        ``?auth=...`` form returns 200.

        Returns the deserialized JSON dict. Raises
        ``LighterHTTPError`` on transport or HTTP errors. The error
        path strips private keys and auth tokens; it never echoes the
        token value, the private key, or the auth query parameter.
        """
        # 1. Construct the sync SignerClient and mint the auth token
        #    (this is the SDK call we keep).
        if self._signer is None:
            self._signer = SignerClient(
                url=self._base_url,
                account_index=self._account_index,
                api_private_keys={self._api_key_index: self._api_private_key},
            )
        auth_token, err = self._signer.create_auth_token_with_expiry(
            api_key_index=self._api_key_index
        )
        if err or not auth_token:
            # Never echo the token or the private key.
            raise LighterHTTPError(
                "Lighter authentication failed",
                status=None,
                diagnostics={
                    "account_index": self._account_index,
                    "api_key_index": self._api_key_index,
                    "public_key": self._public_key,
                },
            )

        # 2. Issue the authenticated GET directly. Two query params
        #    plus the canonical ``?auth=<token>`` query parameter.
        #    responses with non-2xx become LighterHTTPError.
        try:
            import requests  # local import to keep top-of-module deps minimal
            resp = requests.get(
                f"{self._base_url}/api/v1/account",
                params={
                    "by": "index",
                    "value": str(account_index),
                    "auth": auth_token,
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            raise LighterHTTPError(
                f"Lighter HTTP transport failed: {type(exc).__name__}: {exc}",
                status=None,
                diagnostics={
                    "account_index": account_index,
                    "base_url": self._base_url,
                },
            ) from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            # Sanitized: only status + truncated body length.
            raise LighterHTTPError(
                f"Lighter HTTP {resp.status_code}",
                status=resp.status_code,
                body={"truncated": True, "text_head": (resp.text or "")[:512]},
                diagnostics={
                    "account_index": account_index,
                    "base_url": self._base_url,
                },
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise LighterHTTPError(
                f"Lighter returned malformed JSON: {type(exc).__name__}: {exc}",
                status=resp.status_code,
                body={"truncated": True, "text_head": (resp.text or "")[:512]},
                diagnostics={
                    "account_index": account_index,
                    "base_url": self._base_url,
                },
            ) from exc

    def order_book_details(self) -> dict:
        """Public (no auth) ``GET /api/v1/orderBookDetails``.

        Returns the canonical server-side catalog of markets. Each
        entry in ``order_book_details[]`` (perp) and
        ``spot_order_book_details[]`` (spot) carries both ``market_id``
        (int) and ``symbol`` (str). This is the authoritative source
        for ``market_id -> symbol`` resolution and is the ONLY place
        Hermes will ever consult for symbol metadata. We never
        hardcode or fabricate this mapping.

        Public endpoint (the Lighter SDK's ``OrderApi.order_book_details``
        has empty ``_auth_settings`` and no ``authorization``
        parameter), so this call does NOT need a bearer token. We
        still go through ``requests.get(...)`` (sync) for the same
        reason as ``account()``: Hermes' calling context is
        synchronous.

        Returns the deserialized JSON dict. Raises
        ``LighterHTTPError`` on transport / HTTP / JSON errors. The
        error path strips any token / private-key fields (defensive —
        they are not sent by this method).
        """
        try:
            import requests  # local import to keep top-of-module deps minimal
            resp = requests.get(
                f"{self._base_url}/api/v1/orderBookDetails",
                headers={"Accept": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            raise LighterHTTPError(
                f"Lighter public metadata fetch failed: "
                f"{type(exc).__name__}: {exc}",
                status=None,
                diagnostics={"base_url": self._base_url},
            ) from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            raise LighterHTTPError(
                f"Lighter public metadata HTTP {resp.status_code}",
                status=resp.status_code,
                body={"truncated": True, "text_head": (resp.text or "")[:512]},
                diagnostics={"base_url": self._base_url},
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise LighterHTTPError(
                f"Lighter public metadata returned malformed JSON: "
                f"{type(exc).__name__}: {exc}",
                status=resp.status_code,
                body={"truncated": True, "text_head": (resp.text or "")[:512]},
                diagnostics={"base_url": self._base_url},
            ) from exc

    def account_active_orders(
        self,
        account_index: int,
        *,
        market_id: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        """Authenticated ``GET /api/v1/accountActiveOrders``.

        Synchronous from start to end. Same auth + same transport as
        :meth:`account`. The auth token is sent as ``?auth=<token>``
        (Phase 1 architecture; see ``account()`` for the full
        rationale). Cursor pagination is supported by passing
        ``cursor`` to fetch a specific page; pass ``None`` for the
        first page. Per the user's directive, callers must drive
        pagination externally (follow ``next_cursor`` until absent
        or empty) — this method only fetches a single page.

        Returns the deserialized JSON dict with shape::

            {
                "code": int,
                "message": Optional[str],
                "next_cursor": Optional[str],  # empty/absent => end of pagination
                "orders": [Order, ...]
            }

        Raises ``LighterHTTPError`` on transport / HTTP / JSON errors.
        """
        # 1. Mint the auth token via SignerClient (sync, frozen Phase 1).
        if self._signer is None:
            self._signer = SignerClient(
                url=self._base_url,
                account_index=self._account_index,
                api_private_keys={self._api_key_index: self._api_private_key},
            )
        auth_token, err = self._signer.create_auth_token_with_expiry(
            api_key_index=self._api_key_index
        )
        if err or not auth_token:
            raise LighterHTTPError(
                "Lighter authentication failed",
                status=None,
                diagnostics={
                    "account_index": self._account_index,
                    "api_key_index": self._api_key_index,
                    "public_key": self._public_key,
                },
            )

        # 2. Build query params. The server accepts ``cursor`` as a
        # query parameter named ``cursor`` (consistent with how
        # Lighter's ``position_funding``, ``trades``, ``liquidations``
        # and ``accountTxs`` endpoints handle pagination; verified
        # against the lighter-sdk source). We omit the parameter
        # entirely when no cursor is supplied so the first page is
        # requested cleanly.
        params: dict[str, str] = {
            "account_index": str(account_index),
            "auth": auth_token,
        }
        if market_id is not None:
            params["market_id"] = str(market_id)
        if cursor:
            params["cursor"] = cursor

        # 3. Issue the authenticated GET.
        try:
            import requests
            resp = requests.get(
                f"{self._base_url}/api/v1/accountActiveOrders",
                params=params,
                headers={"Accept": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            raise LighterHTTPError(
                f"Lighter active-orders fetch failed: "
                f"{type(exc).__name__}: {exc}",
                status=None,
                diagnostics={
                    "account_index": account_index,
                    "base_url": self._base_url,
                },
            ) from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            raise LighterHTTPError(
                f"Lighter active-orders HTTP {resp.status_code}",
                status=resp.status_code,
                body={"truncated": True, "text_head": (resp.text or "")[:512]},
                diagnostics={
                    "account_index": account_index,
                    "base_url": self._base_url,
                    "had_cursor": bool(cursor),
                },
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise LighterHTTPError(
                f"Lighter active-orders returned malformed JSON: "
                f"{type(exc).__name__}: {exc}",
                status=resp.status_code,
                body={"truncated": True, "text_head": (resp.text or "")[:512]},
                diagnostics={
                    "account_index": account_index,
                    "base_url": self._base_url,
                },
            ) from exc

    def account_tx_by_hash(self, tx_hash: str) -> dict:
        """Public (no auth) ``GET /api/v1/tx?by=hash&value=<tx_hash>``.

        Returns the deserialized JSON dict representing the
        transaction (or an error envelope if not found). This is the
        SDK-documented authoritative transaction-lookup endpoint
        (``lighter/models/enriched_tx.py``).

        Returned shape on success::

            {
              "code": 200,
              "hash": str,
              "type": int,
              "info": str (JSON-encoded),
              "event_info": str (JSON-encoded),
              "status": int,
              "transaction_index": int,
              "l1_address": str,
              "account_index": int,
              "nonce": int,
              "expire_at": int,
              "block_height": int,
              "queued_at": int,
              "executed_at": int,
              "sequence_index": int,
              "parent_hash": str,
              "api_key_index": int,
              "transaction_time": int,
              "committed_at": int,
              "verified_at": int,
              ... (any number of additional_properties)
            }

        Returned shape on not-found::

            {"code": 21500, "message": "transaction not found"}

        Raises ``LighterHTTPError`` on transport failure.

        The caller is responsible for interpreting the response
        semantics (``code``, ``executed_at``, ``block_height``, etc.).
        This method performs NO semantic interpretation — it only
        fetches and returns the raw response.
        """
        if not isinstance(tx_hash, str) or not tx_hash.strip():
            raise LighterHTTPError(
                "Lighter account_tx_by_hash called with empty tx_hash",
                status=None,
                diagnostics={"tx_hash": repr(tx_hash)},
            )
        tx_hash_clean = tx_hash.strip()
        # Some Lighter endpoints accept the hash with or without a
        # leading 0x. We strip it; the server treats them
        # equivalently.
        if tx_hash_clean.startswith("0x"):
            tx_hash_clean = tx_hash_clean[2:]
        try:
            import requests
            resp = requests.get(
                f"{self._base_url}/api/v1/tx",
                params={"by": "hash", "value": tx_hash_clean},
                headers={"Accept": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            raise LighterHTTPError(
                f"Lighter tx-by-hash fetch failed: "
                f"{type(exc).__name__}: {exc}",
                status=None,
                body={"truncated": True, "text_head": "[REDACTED]"},
                diagnostics={"base_url": self._base_url},
            ) from exc
        if resp.status_code != 200:
            # Lighter returns 400 with {code: 21500, message: "..."}
            # when the tx is not in the database. The caller
            # distinguishes by inspecting the body's ``code``.
            try:
                body = resp.json()
            except ValueError:
                body = {"truncated": True, "text_head": (resp.text or "")[:512]}
            if resp.status_code == 400 and isinstance(body, Mapping):
                # Treat "transaction not found" as a soft negative —
                # return the body to the caller; do NOT raise. The
                # verification loop interprets this as
                # "continue polling".
                return body
            raise LighterHTTPError(
                f"Lighter tx-by-hash HTTP {resp.status_code}",
                status=resp.status_code,
                body=body if isinstance(body, Mapping) else {
                    "truncated": True, "text_head": (resp.text or "")[:512],
                },
                diagnostics={"base_url": self._base_url},
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise LighterHTTPError(
                f"Lighter tx-by-hash returned malformed JSON: "
                f"{type(exc).__name__}: {exc}",
                status=resp.status_code,
                body={"truncated": True, "text_head": (resp.text or "")[:512]},
                diagnostics={"base_url": self._base_url},
            ) from exc

    def next_nonce(self, account_index: int, api_key_index: int) -> int:
        """Public (no auth) ``GET /api/v1/nextNonce``.

        Returns the server-authoritative nonce for the next transaction
        on ``(account_index, api_key_index)``. This is a read-only
        endpoint and does not require a bearer token. We call it via
        ``requests.get(...)`` so the call is sync and does not depend
        on the SDK's async NonceManager.

        Returns the integer ``nonce`` value. Raises ``LighterHTTPError``
        on transport / HTTP / JSON errors.
        """
        try:
            import requests
            resp = requests.get(
                f"{self._base_url}/api/v1/nextNonce",
                params={
                    "account_index": str(account_index),
                    "api_key_index": str(api_key_index),
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            raise LighterHTTPError(
                f"Lighter nextNonce fetch failed: {type(exc).__name__}: {exc}",
                status=None,
                diagnostics={
                    "account_index": account_index,
                    "api_key_index": api_key_index,
                    "base_url": self._base_url,
                },
            ) from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            sanitized_body = _sanitize_http_body(resp)
            raise LighterHTTPError(
                f"Lighter nextNonce HTTP {resp.status_code}",
                status=resp.status_code,
                body=sanitized_body,
                diagnostics={
                    "account_index": account_index,
                    "api_key_index": api_key_index,
                    "base_url": self._base_url,
                },
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            sanitized_body = _sanitize_http_body(resp)
            raise LighterHTTPError(
                f"Lighter nextNonce returned malformed JSON: {type(exc).__name__}: {exc}",
                status=resp.status_code,
                body=sanitized_body,
                diagnostics={
                    "account_index": account_index,
                    "api_key_index": api_key_index,
                    "base_url": self._base_url,
                },
            ) from exc

        # Response shape: {"code": 200, "nonce": <int>}.
        if not isinstance(payload, Mapping):
            raise LighterHTTPError(
                f"Lighter nextNonce returned non-mapping: {type(payload).__name__}",
                status=resp.status_code,
                body=_sanitize_http_body(resp),
                diagnostics={
                    "account_index": account_index,
                    "api_key_index": api_key_index,
                },
            )
        try:
            nonce = int(payload.get("nonce", 0))
        except (TypeError, ValueError) as exc:
            raise LighterHTTPError(
                f"Lighter nextNonce returned malformed nonce: {type(exc).__name__}",
                status=resp.status_code,
                body=_sanitize_http_body(resp),
                diagnostics={
                    "account_index": account_index,
                    "api_key_index": api_key_index,
                },
            ) from exc
        return nonce

    def place_order(
        self,
        *,
        account_index: int,
        api_key_index: int,
        market_index: int,
        client_order_index: int,
        wire_price: int,
        wire_base_amount: int,
        is_ask: int,
        order_type: int,
        time_in_force: int,
        reduce_only: bool = False,
        trigger_price: int = 0,
        order_expiry: int = -1,
        api_private_key: str = "",
    ) -> Tuple[dict, dict]:
        """Authenticated ``POST /api/v1/sendTx`` for one limit order.

        Phase 3A authorizes LIMIT + GTT only. The caller is responsible
        for converting the requested decimal price and quantity to
        their integer wire values via ``_exact_scale_to_wire``.

        Sequence:

        1. Mint the Lighter SDK's native bearer signer (sync C call).
        2. Sign the order via ``SignerClient.sign_create_order(...)``
           using the verified Python signature (this bypasses the SDK's
           async NonceManager, which we avoid because TradeDesk is
           synchronous).
        3. POST ``tx_type`` and the ephemeral ``tx_info`` to
           ``/api/v1/sendTx`` via ``requests.post(...)``.
        4. Discard the local ``tx_info`` reference immediately.

        Returns a SANITIZED summary dict: never the raw signed payload,
        never the signature, never the raw nonce. The signature is
        represented as ``"[REDACTED]"``.

        Raises ``LighterHTTPError`` on transport / HTTP / signing errors.
        All error paths pass through ``_sanitize_http_body`` or the
        sanitizer before surfacing.
        """
        # Step 1: SignerClient. The SDK requires a Dict[int, str] for
        # ``api_private_keys``. The Phase 1 client only stored one
        # private key per LighterHttpClient; we accept it via the
        # ``api_private_key`` parameter and pass it as a single-entry
        # dict under the resolved ``api_key_index``.
        if self._signer is None:
            self._signer = SignerClient(
                url=self._base_url,
                account_index=self._account_index,
                api_private_keys={int(api_key_index): api_private_key},
            )

        # Step 2: Synchronous signing. ``sign_create_order`` is NOT
        # decorated with @process_api_key_and_nonce (unlike the async
        # create_order). It calls self.signer.SignCreateOrder(...)
        # directly, passing our ``nonce`` straight through to the C
        # binary. We therefore MUST fetch a real, server-authoritative
        # nonce OUTSIDE this call and pass it as the ``nonce`` kwarg
        # here. ``api_key_index`` is also passed directly.
        # ``skip_nonce=0`` means the nonce is included in the signed
        # payload.
        nonce = self.next_nonce(self._account_index, int(api_key_index))
        signed = self._signer.sign_create_order(
            market_index,
            client_order_index,
            wire_base_amount,
            wire_price,
            is_ask,
            order_type,
            time_in_force,
            reduce_only,
            trigger_price,
            order_expiry,
            integrator_account_index=0,
            integrator_taker_fee=0,
            integrator_maker_fee=0,
            self_trade_behavior_mode=0,
            self_trade_equality_mode=0,
            skip_nonce=0,
            nonce=nonce,
            api_key_index=api_key_index,
        )

        # The SDK's returned tuple is (tx_type, tx_info, tx_hash_pre, err).
        tx_type, tx_info, _tx_hash_pre, sign_err = signed
        if sign_err or not tx_info:
            raise LighterHTTPError(
                f"Lighter signing failed: {sign_err!r}",
                status=None,
                body={"truncated": True, "text_head": REDACT_TOKEN},
                diagnostics={
                    "market_index": market_index,
                    "client_order_index": client_order_index,
                },
            )

        # Step 3: POST the ephemeral tx_info.
        response = None
        try:
            try:
                import requests
                response = requests.post(
                    f"{self._base_url}/api/v1/sendTx",
                    data={"tx_type": str(tx_type), "tx_info": tx_info},
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
            except Exception as exc:
                raise LighterHTTPError(
                    f"Lighter sendTx transport failed: "
                    f"{type(exc).__name__}: {exc}",
                    status=None,
                    body={"truncated": True, "text_head": REDACT_TOKEN},
                    diagnostics={
                        "market_index": market_index,
                        "client_order_index": client_order_index,
                        "base_url": self._base_url,
                    },
                ) from exc

            if response.status_code < 200 or response.status_code >= 300:
                raise LighterHTTPError(
                    f"Lighter sendTx HTTP {response.status_code}",
                    status=response.status_code,
                    body=_sanitize_http_body(response),
                    diagnostics={
                        "market_index": market_index,
                        "client_order_index": client_order_index,
                        "base_url": self._base_url,
                    },
                )

            try:
                resp_send_tx = response.json()
            except ValueError as exc:
                raise LighterHTTPError(
                    f"Lighter sendTx returned malformed JSON: "
                    f"{type(exc).__name__}: {exc}",
                    status=response.status_code,
                    body=_sanitize_http_body(response),
                    diagnostics={
                        "market_index": market_index,
                        "client_order_index": client_order_index,
                    },
                ) from exc

            if not isinstance(resp_send_tx, Mapping):
                raise LighterHTTPError(
                    f"Lighter sendTx returned non-mapping: "
                    f"{type(resp_send_tx).__name__}",
                    status=response.status_code,
                    body=_sanitize_http_body(response),
                    diagnostics={
                        "market_index": market_index,
                        "client_order_index": client_order_index,
                    },
                )

            return _sanitize_sensitive_data(resp_send_tx), dict(resp_send_tx)
        finally:
            # Step 4: Discard the local tx_info reference. We rely on
            # Python's garbage collector for memory; we do not claim
            # cryptographic memory erasure.
            tx_info = None

    def create_tp_order(
        self,
        *,
        market_index: int,
        client_order_index: int,
        base_amount: int,
        trigger_price: int,
        price: int,
        is_ask: int,
        reduce_only: bool = False,
        order_expiry: int = -1,
        api_private_key: str = "",
    ) -> Tuple[dict, dict]:
        """Authenticated ``POST /api/v1/sendTx`` for one Take-Profit
        conditional order.

        Phase 7 (Position Manager activation, commit 2e0e839) wired
        the LighterAgent dispatch table for ``set_tp`` but this
        transport wrapper was not yet implemented. This method
        closes the gap so the Position Manager can actually submit
        a TP order to the Lighter matching engine.

        Sequence (mirrors ``place_order`` exactly):
        1. Mint the Lighter SDK's native bearer signer (sync C call).
        2. Sign the order via ``SignerClient.sign_create_order(...)``
           using the verified Python signature (this bypasses the
           SDK's async NonceManager, which we avoid because TradeDesk
           is synchronous). The order is signed as ``ORDER_TYPE_TAKE_PROFIT``
           (= 4) with ``trigger_price`` set to the activation price
           and ``price`` set to the limit execution price. Per Lighter
           wire semantics, a TP order with ``trigger_price == price``
           converts to a market-on-trigger (the canonical Telegram UX
           for "set TP, execute at that price").
        3. POST ``tx_type`` and the ephemeral ``tx_info`` to
           ``/api/v1/sendTx`` via ``requests.post(...)``.
        4. Discard the local ``tx_info`` reference immediately.

        Returns a SANITIZED summary dict: never the raw signed payload,
        never the signature, never the raw nonce. The signature is
        represented as ``"[REDACTED]"``.

        Raises ``LighterHTTPError`` on transport / HTTP / signing errors.
        All error paths pass through ``_sanitize_http_body`` or the
        sanitizer before surfacing.

        IMPORTANT: TP/SL are signed via the SAME ``sign_create_order``
        primitive as limit orders — the difference is only the
        ``order_type`` field (TAKE_PROFIT = 4 vs STOP_LOSS = 2 vs
        LIMIT = 0) and the ``trigger_price`` field. There is NO
        separate ``sign_create_tp_order`` method in the Lighter SDK.

        Contract: this wrapper passes
        ``SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL`` (= 0)
        to match the SDK's own ``create_tp_order`` helper. The Lighter
        wire validator (``lighter-go/types/txtypes/create_order.go``)
        rejects GTT for ``ORDER_TYPE_TAKE_PROFIT`` (= 4) with the error
        string 'OrderTimeInForce is not valid'. LIMIT-trigger variants
        (``ORDER_TYPE_TAKE_PROFIT_LIMIT`` = 5) are outside this
        wrapper's scope and would require GTT.
        """
        return self._sign_and_post_create_order(
            market_index=market_index,
            client_order_index=client_order_index,
            base_amount=base_amount,
            trigger_price=trigger_price,
            price=price,
            is_ask=is_ask,
            order_type=SignerClient.ORDER_TYPE_TAKE_PROFIT,
            reduce_only=reduce_only,
            order_expiry=order_expiry,
            api_private_key=api_private_key,
            # SDK-proven contract: market-on-trigger TP requires IOC.
            # Passing GTT here causes the Lighter wire validator to
            # reject the tx with 'OrderTimeInForce is not valid' BEFORE
            # any HTTP POST is issued.
            time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
        )

    def create_sl_order(
        self,
        *,
        market_index: int,
        client_order_index: int,
        base_amount: int,
        trigger_price: int,
        price: int,
        is_ask: int,
        reduce_only: bool = False,
        order_expiry: int = -1,
        api_private_key: str = "",
    ) -> Tuple[dict, dict]:
        """Authenticated ``POST /api/v1/sendTx`` for one Stop-Loss
        conditional order.

        Phase 7 (Position Manager activation, commit 2e0e839) wired
        the LighterAgent dispatch table for ``set_sl`` but this
        transport wrapper was not yet implemented. This method
        closes the gap.

        Same wire transaction as ``create_tp_order`` and
        ``place_order``: the difference is the ``order_type`` field
        (``ORDER_TYPE_STOP_LOSS`` = 2). All sanitization, nonce-lock
        contract, and error handling are identical to ``place_order``.

        Contract: this wrapper passes
        ``SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL`` (= 0)
        to match the SDK's own ``create_sl_order`` helper. The Lighter
        wire validator rejects GTT for ``ORDER_TYPE_STOP_LOSS`` (= 2)
        with the error string 'OrderTimeInForce is not valid'.
        LIMIT-trigger variants (``ORDER_TYPE_STOP_LOSS_LIMIT`` = 3)
        are outside this wrapper's scope and would require GTT.
        """
        return self._sign_and_post_create_order(
            market_index=market_index,
            client_order_index=client_order_index,
            base_amount=base_amount,
            trigger_price=trigger_price,
            price=price,
            is_ask=is_ask,
            order_type=SignerClient.ORDER_TYPE_STOP_LOSS,
            reduce_only=reduce_only,
            order_expiry=order_expiry,
            api_private_key=api_private_key,
            # SDK-proven contract: market-on-trigger SL requires IOC.
            time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
        )

    def _sign_and_post_create_order(
        self,
        *,
        market_index: int,
        client_order_index: int,
        base_amount: int,
        trigger_price: int,
        price: int,
        is_ask: int,
        order_type: int,
        reduce_only: bool,
        order_expiry: int,
        api_private_key: str,
        time_in_force: int = SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
    ) -> Tuple[dict, dict]:
        """Shared inner primitive for sign + POST of any CreateOrder
        tx (LIMIT, TAKE_PROFIT, STOP_LOSS, MARKET, TAKE_PROFIT_LIMIT,
        STOP_LOSS_LIMIT, TWAP).

        All OrderPlace paths converge here because the Lighter SDK
        signs every order with the same ``sign_create_order``
        primitive. Per the lighter-go reference (types/txtypes/
        create_order.go Validate()), the wire-level time_in_force
        value MUST be selected per the order_type family:

          - LIMIT (type=0)                      → GTT (this default).
          - MARKET (type=1)                     → IOC required.
          - TAKE_PROFIT (type=4, market-on-trig) → IOC required.
          - STOP_LOSS (type=2, market-on-trig)   → IOC required.
          - TAKE_PROFIT_LIMIT (type=5, limit-trig)   → GTT (SDK default).
          - STOP_LOSS_LIMIT (type=3, limit-trig)     → GTT (SDK default).
          - TWAP (type=6)                        → GTT required.

        The time_in_force parameter is explicit (NOT derived from
        order_type inside this helper) so that:
          - the standard LIMIT path keeps its existing GTT default
            byte-for-byte equivalent in behavior;
          - the market-on-trigger TP / SL wrappers can pass IOC
            exactly as the SDK's own ``create_tp_order`` /
            ``create_sl_order`` helpers do;
          - a future TP_LIMIT / SL_LIMIT caller can pass GTT without
            forcing the TP/SL dispatchers to learn a new code path.

        The validation that catches a mismatched TIF (e.g. GTT for
        type=4) lives in the C binding ``SignCreateOrder`` and
        produces the canonical error string
        'OrderTimeInForce is not valid'. The wrapper surfaces this
        verbatim via ``LighterHTTPError`` with a redacted body.

        The nonce acquisition, synchronous signing, and POST contract
        match ``place_order`` exactly. No retries. No fallback.
        """
        # Step 1: SignerClient (same as place_order).
        api_key_index = self._api_key_index
        # Phase 7: the LighterAgent._send_tpsl_order_locked captures
        # api_private_key from the resolved credentials and passes it
        # through. We accept it via this parameter and pass it as a
        # single-entry dict under the resolved api_key_index. When
        # api_private_key is empty (a defensive test-only case), we
        # fall back to the value passed to __init__.
        effective_private_key = api_private_key or self._api_private_key
        if self._signer is None:
            self._signer = SignerClient(
                url=self._base_url,
                account_index=self._account_index,
                api_private_keys={
                    int(api_key_index): effective_private_key
                },
            )

        # Step 2: Synchronous signing. We always fetch a real
        # server-authoritative nonce OUTSIDE this call and pass it
        # as the ``nonce`` kwarg. ``skip_nonce=0`` means the nonce
        # is included in the signed payload.
        #
        # The 7th positional argument to ``SignerClient.sign_create_order``
        # is ``time_in_force``. The value is supplied by the caller
        # (LIMIT callers default to GTT; MARKET and the market-on-
        # trigger TP / SL wrappers pass IOC). We do NOT derive TIF
        # from order_type inside this helper — that would silently
        # change the byte-equivalent behavior of the standard LIMIT
        # path.
        nonce = self.next_nonce(self._account_index, int(api_key_index))
        signed = self._signer.sign_create_order(
            market_index,
            client_order_index,
            base_amount,
            price,
            is_ask,
            order_type,
            time_in_force,
            reduce_only,
            trigger_price,
            order_expiry,
            integrator_account_index=0,
            integrator_taker_fee=0,
            integrator_maker_fee=0,
            self_trade_behavior_mode=0,
            self_trade_equality_mode=0,
            skip_nonce=0,
            nonce=nonce,
            api_key_index=api_key_index,
        )

        # The SDK's returned tuple is (tx_type, tx_info, tx_hash_pre, err).
        tx_type, tx_info, _tx_hash_pre, sign_err = signed
        if sign_err or not tx_info:
            raise LighterHTTPError(
                f"Lighter signing failed: {sign_err!r}",
                status=None,
                body={"truncated": True, "text_head": REDACT_TOKEN},
                diagnostics={
                    "market_index": market_index,
                    "client_order_index": client_order_index,
                    "order_type": order_type,
                },
            )

        # Step 3: POST the ephemeral tx_info.
        response = None
        try:
            try:
                import requests
                response = requests.post(
                    f"{self._base_url}/api/v1/sendTx",
                    data={"tx_type": str(tx_type), "tx_info": tx_info},
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
            except Exception as exc:
                raise LighterHTTPError(
                    f"Lighter sendTx transport failed: "
                    f"{type(exc).__name__}: {exc}",
                    status=None,
                    body={"truncated": True, "text_head": REDACT_TOKEN},
                    diagnostics={
                        "market_index": market_index,
                        "client_order_index": client_order_index,
                        "base_url": self._base_url,
                    },
                ) from exc

            if response.status_code < 200 or response.status_code >= 300:
                raise LighterHTTPError(
                    f"Lighter sendTx HTTP {response.status_code}",
                    status=response.status_code,
                    body=_sanitize_http_body(response),
                    diagnostics={
                        "market_index": market_index,
                        "client_order_index": client_order_index,
                        "base_url": self._base_url,
                    },
                )

            try:
                resp_send_tx = response.json()
            except ValueError as exc:
                raise LighterHTTPError(
                    f"Lighter sendTx returned malformed JSON: "
                    f"{type(exc).__name__}: {exc}",
                    status=response.status_code,
                    body=_sanitize_http_body(response),
                    diagnostics={
                        "market_index": market_index,
                        "client_order_index": client_order_index,
                    },
                ) from exc

            if not isinstance(resp_send_tx, Mapping):
                raise LighterHTTPError(
                    f"Lighter sendTx returned non-mapping: "
                    f"{type(resp_send_tx).__name__}",
                    status=response.status_code,
                    body=_sanitize_http_body(response),
                    diagnostics={
                        "market_index": market_index,
                        "client_order_index": client_order_index,
                    },
                )

            return _sanitize_sensitive_data(resp_send_tx), dict(resp_send_tx)
        finally:
            # Step 4: Discard the local tx_info reference.
            tx_info = None

    def cancel_order(
        self,
        *,
        account_index: int,
        api_key_index: int,
        market_index: int,
        order_index: int,
        api_private_key: str = "",
    ) -> tuple:
        """Authenticated ``POST /api/v1/sendTx`` for one cancel-order
        transaction (Phase 3B).

        Phase 3B authorizes cancelling a single existing order by its
        server-assigned ``order_index``. The same nonce-lock contract
        as ``place_order`` applies: we fetch the authoritative nonce
        from the public ``/api/v1/nextNonce`` endpoint, sign synchronously
        via the SDK's native C binary, and POST the ephemeral ``tx_info``
        to ``/api/v1/sendTx``. The signed payload is discarded in a
        ``finally`` block immediately after the POST returns or raises.

        Returns the (sanitized, raw) pair. The raw dict is preserved
        so callers can correlate with the order being cancelled. The
        signed payload material (Sig, Nonce, AccountIndex, ...) is
        never retained.
        """
        # Step 1: Lazy SignerClient init. We accept api_private_key as a
        # single string and pass it as a one-entry dict, exactly the
        # same shape ``place_order`` uses (Phase 3A).
        if self._signer is None:
            self._signer = SignerClient(
                url=self._base_url,
                account_index=self._account_index,
                api_private_keys={
                    int(api_key_index): api_private_key,
                },
            )

        # Step 2: Fetch authoritative nonce for the
        # (account_index, api_key_index) pair. This nonce is for THIS
        # transaction slot; Lighter's matching engine atomically bumps
        # the nonce on successful acceptance.
        nonce = self.next_nonce(
            self._account_index, int(api_key_index)
        )

        # Step 3: Synchronous native signing for the CancelOrder
        # transaction. ``sign_cancel_order`` is NOT decorated with
        # @process_api_key_and_nonce, so the nonce we just fetched is
        # forwarded directly into the C binary's SignCancelOrder call.
        signed = self._signer.sign_cancel_order(
            market_index,
            order_index,
            skip_nonce=0,
            nonce=nonce,
            api_key_index=int(api_key_index),
        )
        # Result tuple: (tx_type, tx_info, tx_hash_pre, None_or_err)
        tx_type, tx_info, _tx_hash_pre, sign_err = signed
        if sign_err or not tx_info:
            raise LighterHTTPError(
                f"Lighter signing failed (cancel): {sign_err!r}",
                status=None,
                body={"truncated": True, "text_head": REDACT_TOKEN},
                diagnostics={
                    "market_index": market_index,
                    "order_index": order_index,
                },
            )

        # Step 4: POST the ephemeral tx_info. Same shape as
        # ``place_order`` (form-encoded body, ``tx_type`` + ``tx_info``).
        response = None
        try:
            try:
                import requests
                response = requests.post(
                    f"{self._base_url}/api/v1/sendTx",
                    data={
                        "tx_type": str(tx_type),
                        "tx_info": tx_info,
                    },
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
            except Exception as exc:
                raise LighterHTTPError(
                    f"Lighter sendTx transport failed (cancel): "
                    f"{type(exc).__name__}: {exc}",
                    status=None,
                    body={"truncated": True, "text_head": REDACT_TOKEN},
                    diagnostics={
                        "market_index": market_index,
                        "order_index": order_index,
                        "base_url": self._base_url,
                    },
                ) from exc

            if response.status_code < 200 or response.status_code >= 300:
                sanitized_body = _sanitize_http_body(response)
                raise LighterHTTPError(
                    f"Lighter sendTx HTTP {response.status_code} (cancel)",
                    status=response.status_code,
                    body=sanitized_body,
                    diagnostics={
                        "market_index": market_index,
                        "order_index": order_index,
                        "base_url": self._base_url,
                    },
                )

            try:
                resp_send_tx = response.json()
            except ValueError as exc:
                raise LighterHTTPError(
                    f"Lighter sendTx returned malformed JSON (cancel): "
                    f"{type(exc).__name__}: {exc}",
                    status=response.status_code,
                    body=_sanitize_http_body(response),
                    diagnostics={
                        "market_index": market_index,
                        "order_index": order_index,
                    },
                ) from exc

            if not isinstance(resp_send_tx, Mapping):
                raise LighterHTTPError(
                    f"Lighter sendTx returned non-mapping (cancel): "
                    f"{type(resp_send_tx).__name__}",
                    status=response.status_code,
                    body=_sanitize_http_body(response),
                    diagnostics={
                        "market_index": market_index,
                        "order_index": order_index,
                    },
                )

            return (
                _sanitize_sensitive_data(resp_send_tx),
                dict(resp_send_tx),
            )
        finally:
            # Step 5: Discard the local tx_info reference. We rely on
            # Python's garbage collector for memory; we do not claim
            # cryptographic memory erasure.
            tx_info = None


# ---------------------------------------------------------------------------
# Execution result helper
# ---------------------------------------------------------------------------

def _execution_result(request: Mapping[str, Any], *, success: bool,
                      error: Optional[str] = None, exchange_response: Any = None,
                      balance: Any = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "success": bool(success),
        "exchange": "lighter",
        "operation": str(request.get("operation") or ""),
        "parent_operation": str(request.get("parent_operation")
                                or request.get("operation") or ""),
        "account": request.get("account") or "",
        "structured_request": dict(request),
    }
    if "chain" in extra:
        out["chain"] = extra["chain"]
    if exchange_response is not None:
        out["exchange_response"] = exchange_response
    if balance is not None:
        out["balance"] = balance
    if error is not None:
        out["error"] = str(error)
    for k, v in extra.items():
        if k not in out:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Balance normalization
# ---------------------------------------------------------------------------

def _to_hermes_balance(account: Mapping[str, Any]) -> dict[str, Any]:
    """Project a single Lighter ``accounts[]`` entry into the Hermes
    balance shape.

    The same shape is produced regardless of chain (ARBITRUM or
    ROBINHOOD) because both deployments return the same
    ``DetailedAccounts`` schema.

    Synthetic ``marginSummary`` and ``withdrawable`` blocks are
    populated on the agent's ``exchange_response`` (not here) so the
    existing ``_format_balance_message`` renderer can find them.
    """
    total_value = str(account.get("total_asset_value") or "0")
    available_balance = str(account.get("available_balance") or "0")
    margin_used = str(account.get("cross_initial_margin_requirement") or "0")
    return {
        "balance": total_value,
        "available_to_withdraw": available_balance,
        "account_equity": str(account.get("collateral") or total_value),
        "total_margin_used": margin_used,
        "marginSummary": {
            "accountValue": total_value,
            "totalMarginUsed": margin_used,
            "totalNtlPos": str(account.get("cross_asset_value") or "0"),
        },
        "l1_address": str(account.get("l1_address") or ""),
        "asset_index": int(account.get("index") or 0),
        "pending_order_count": int(account.get("pending_order_count") or 0),
        "total_order_count": int(account.get("total_order_count") or 0),
        "positions_count": len(account.get("positions") or []),
        "assets": list(account.get("assets") or []),
    }


# ---------------------------------------------------------------------------
# Position normalization (Phase 2A)
# ---------------------------------------------------------------------------
#
# Lighter's ``/api/v1/account`` response includes a ``positions`` array
# inside ``accounts[0]``. Each position has the shape defined by
# ``ligher.models.account_position.AccountPosition``:
#
#   market_id: int
#   symbol: str
#   initial_margin_fraction: str   (e.g. "2.00"  ->  leverage 1/2.00 = 50x)
#   open_order_count: int
#   pending_order_count: int
#   position_tied_order_count: int
#   sign: int                       (1=long, -1=short, 0=flat)
#   position: str                   (decimal string, signed magnitude;
#                                    Lighter does NOT include a separate
#                                    sign in the magnitude string)
#   avg_entry_price: str
#   position_value: str
#   unrealized_pnl: str
#   realized_pnl: str
#   liquidation_price: str
#   total_funding_paid_out: Optional[str]
#   margin_mode: int                (0=cross, 1=isolated)
#   allocated_margin: str
#   total_discount: str
#
# The Hermes-standard position schema is documented in Phase 2A
# implementation report docs/plans/2026-07-19-001-lighter-phase2a-positions-freeze.md.
# Notes on the gap between Lighter's payload and the user's schema:
#
#   * mark_price: NOT provided by /api/v1/account. Returned as
#     ``None``; surface ``positions_meta.mark_price_supported=False``
#     in diagnostics so callers don't mistake the absence for a bug.
#   * leverage: Lighter exposes ``initial_margin_fraction`` (the
#     inverse). We compute ``leverage = 1 / imf`` (e.g. "2.00" -> "50").
#   * size: raw.position may carry a sign in the magnitude (``-1.76363``
#     on Lighter's source). We strip the sign and let ``side`` carry
#     the sign instead, mirroring how other exchanges surface the
#     net quantity.
#   * margin_used: raw.allocated_margin (zero in cross-margin accounts
#     because the SDK doesn't separate per-position margin there).

def _hermes_normalize_lighter_position(
    raw: Mapping[str, Any], *, chain: str, account: str,
) -> dict[str, Any] | None:
    """Normalize one Lighter position dict into the Hermes-standard
    shape. Returns ``None`` for zero-size / flat positions so the
    caller can drop them from the active list.

    The full raw dict is preserved under ``raw`` for diagnostics.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        sign = int(raw.get("sign") or 0)
    except (TypeError, ValueError):
        sign = 0
    magnitude_str = str(raw.get("position") or "0")
    try:
        magnitude = Decimal(str(magnitude_str).lstrip("-") or "0")
    except Exception:
        magnitude = Decimal("0")
    # Zero-size positions are filtered. Lighter surfaces them on
    # every market the account has ever touched; they are not
    # "active positions" in any Hermes sense.
    if magnitude <= 0 or sign == 0:
        return None

    # Leverage: inverse of initial_margin_fraction.
    try:
        imf = Decimal(str(raw.get("initial_margin_fraction") or "0"))
    except Exception:
        imf = Decimal("0")
    leverage_str = "1"
    if imf > 0:
        leverage_str = str((Decimal("1") / imf).quantize(Decimal("0.01")))

    side_str = "long" if sign > 0 else "short"

    # Margin mode mapping.
    try:
        mm_int = int(raw.get("margin_mode") or 0)
    except (TypeError, ValueError):
        mm_int = 0
    margin_mode_str = "isolated" if mm_int == 1 else "cross"

    # Numeric helpers: surface as decimal strings (Hermes convention).
    def _s(name: str) -> str:
        return str(raw.get(name) or "0")

    return {
        "exchange": "lighter",
        "chain": chain,
        "account": account,
        "market_id": int(raw.get("market_id") or 0),
        "symbol": str(raw.get("symbol") or ""),
        "side": side_str,
        "size": str(magnitude),
        "entry_price": _s("avg_entry_price"),
        "mark_price": None,
        "unrealized_pnl": _s("unrealized_pnl"),
        "realized_pnl": _s("realized_pnl"),
        "position_value": _s("position_value"),
        "leverage": leverage_str,
        "liquidation_price": _s("liquidation_price"),
        "margin_mode": margin_mode_str,
        "margin_used": _s("allocated_margin"),
        "open_order_count": int(raw.get("open_order_count") or 0),
        "pending_order_count": int(raw.get("pending_order_count") or 0),
        "total_funding_paid_out": str(raw.get("total_funding_paid_out") or "0"),
        "take_profit": None,
        "stop_loss": None,
        "raw": dict(raw),
    }


def _hermes_normalize_lighter_positions(
    raw_positions: Iterable[Mapping[str, Any]], *, chain: str, account: str,
) -> tuple[list[dict[str, Any]], int]:
    """Normalize an iterable of Lighter position dicts.

    Returns ``(normalized_active_list, active_count)``. Zero-size /
    flat positions are skipped from the active list. ``active_count``
    is the number of positions in that list (NOT the total raw
    payload, which is ``len(raw_positions)``).
    """
    out: list[dict[str, Any]] = []
    for raw in raw_positions:
        norm = _hermes_normalize_lighter_position(
            raw, chain=chain, account=account,
        )
        if norm is not None:
            out.append(norm)
    return out, len(out)


# ---------------------------------------------------------------------------
# Open-order normalization (Phase 2B)
# ---------------------------------------------------------------------------
#
# The ``/api/v1/accountActiveOrders`` response embeds each order under
# ``orders[]``. The Lighter SDK's ``lighter.models.order.Order`` model
# defines the wire schema. We project each entry into the Hermes-standard
# open-order shape (mirrors AFX + Hyperliquid).
#
# Notable gaps:
#   * The ``Order`` model does NOT carry a ``symbol`` field. We resolve
#     ``market_index`` -> ``symbol`` via the public
#     ``/api/v1/orderBookDetails`` endpoint (cached, see
#     ``_LighterMarketSymbolCache``). If the cache is missing or stale
#     and a refresh fails, ``symbol`` falls back to ``None``. Hermes
#     NEVER fabricates exchange metadata.
#   * The ``raw.side`` field is marked ``TODO: remove`` in the SDK
#     source; it is unreliable. We use ``raw.is_ask`` as the
#     authoritative side indicator.
#
# The Hermes-standard open-order shape surfaced via ``_execution_result``:
#
#   order_id               str  (raw.order_id)
#   client_order_id        Optional[str]
#   order_index            str  (raw.order_index; int -> str)
#   client_order_index     str  (raw.client_order_index; int -> str)
#   market_index           str  (raw.market_index; int -> str)
#   symbol                 Optional[str]   (from authoritative map; None if missing)
#   side                   'buy'|'sell'
#   order_type             str (lowercased; raw.type)
#   time_in_force          str (lowercased; raw.time_in_force)
#   price                  str (Decimal)
#   trigger_price          Optional[str] (raw.trigger_price if non-zero else None)
#   initial_base_amount    str (Decimal)
#   remaining_base_amount  str (Decimal)
#   filled_base_amount     str (Decimal)
#   filled_quote_amount    str (Decimal)
#   reduce_only            bool
#   is_post_only           bool  (synthesized: time_in_force == 'post-only')
#   trigger_status         str
#   status                 str (one of 22 enum values)
#   is_active              bool  (synthesized: status in {open, in-progress, pending})
#   order_expiry           Optional[int] (raw.order_expiry; -1 -> None)
#   created_at             int
#   updated_at             int
#   block_height           int
#   raw                    dict (verbatim)


def _hermes_normalize_lighter_open_order(
    raw: Mapping[str, Any], *,
    chain: str, account: str, symbol_map: Mapping[int, str],
) -> dict[str, Any]:
    """Project one Lighter ``Order`` dict into the Hermes-standard
    open-order shape. ``symbol_map`` is the authoritative
    ``market_id -> symbol`` mapping from the public
    ``/api/v1/orderBookDetails`` endpoint. If a market_index is
    missing from the map, ``symbol`` is set to ``None`` — Hermes
    never fabricates exchange metadata.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(f"open order is not a mapping: {type(raw).__name__}")

    def _s(name: str) -> str:
        return str(raw.get(name) or "0")

    market_index_int = int(raw.get("market_index") or 0)
    # Resolve symbol ONLY from the authoritative map. Missing entries
    # become None (never fabricated).
    symbol = symbol_map.get(market_index_int)

    is_ask_raw = raw.get("is_ask")
    is_ask = bool(is_ask_raw) if isinstance(is_ask_raw, (bool, int)) else False
    side = "sell" if is_ask else "buy"

    tif_raw = str(raw.get("time_in_force") or "").strip().lower()
    type_raw = str(raw.get("type") or "").strip().lower()

    trigger_price_raw = str(raw.get("trigger_price") or "0")
    trigger_price: Optional[str]
    try:
        if Decimal(trigger_price_raw) == 0:
            trigger_price = None
        else:
            trigger_price = trigger_price_raw
    except Exception:
        trigger_price = None

    status_raw = str(raw.get("status") or "").strip().lower()
    is_active = status_raw in {"open", "in-progress", "pending"}

    try:
        order_expiry = int(raw.get("order_expiry") or 0)
    except (TypeError, ValueError):
        order_expiry = 0
    if order_expiry < 0:
        order_expiry = None  # SKIP_NONCE_OFF

    return {
        "exchange": "lighter",
        "chain": chain,
        "account": account,
        "order_id": str(raw.get("order_id") or ""),
        "client_order_id": str(raw.get("client_order_id") or "")
                           or None,
        "order_index": str(int(raw.get("order_index") or 0)),
        "client_order_index": str(int(raw.get("client_order_index") or 0)),
        "market_index": str(market_index_int),
        "symbol": symbol,
        "side": side,
        "order_type": type_raw,
        "time_in_force": tif_raw,
        "price": _s("price"),
        "trigger_price": trigger_price,
        "initial_base_amount": _s("initial_base_amount"),
        "remaining_base_amount": _s("remaining_base_amount"),
        "filled_base_amount": _s("filled_base_amount"),
        "filled_quote_amount": _s("filled_quote_amount"),
        "reduce_only": bool(raw.get("reduce_only") or False),
        "is_post_only": tif_raw == "post-only",
        "trigger_status": str(raw.get("trigger_status") or ""),
        "status": status_raw,
        "is_active": is_active,
        "order_expiry": order_expiry,
        "created_at": int(raw.get("created_at") or 0),
        "updated_at": int(raw.get("updated_at") or 0),
        "block_height": int(raw.get("block_height") or 0),
        "raw": dict(raw),
    }


def _hermes_normalize_lighter_open_orders(
    raw_orders: Iterable[Mapping[str, Any]], *,
    chain: str, account: str, symbol_map: Mapping[int, str],
) -> list[dict[str, Any]]:
    """Normalize an iterable of Lighter ``Order`` dicts.

    Returns the normalized list. Non-mapping entries are silently
    dropped (defensive). The caller is expected to drive the
    pagination loop and supply the aggregated ``raw_orders``.
    """
    out: list[dict[str, Any]] = []
    for raw in raw_orders:
        if not isinstance(raw, Mapping):
            continue
        out.append(_hermes_normalize_lighter_open_order(
            raw, chain=chain, account=account, symbol_map=symbol_map,
        ))
    return out


# ---------------------------------------------------------------------------
# Position TP/SL read-back enrichment (Lighter-specific)
# ---------------------------------------------------------------------------

LIGHTER_TPSL_ACTIVE_STATUSES: frozenset[str] = frozenset({"pending"})
LIGHTER_TPSL_TRIGGER_STATUSES: frozenset[str] = frozenset({
    "mark-price",
    "ready",
})
_LIGHTER_TP_TYPES: frozenset[str] = frozenset({
    "take-profit",
    "take-profit-limit",
})
_LIGHTER_SL_TYPES: frozenset[str] = frozenset({
    "stop-loss",
    "stop-loss-limit",
})


def _normalize_lighter_enum(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    return re.sub(r"-+", "-", raw)


def _lighter_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _lighter_trigger_price_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dec = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite() or dec == 0:
        return None
    return raw


def _lighter_int_for_rank(value: Any) -> int:
    try:
        return int(str(value or "0"))
    except (TypeError, ValueError):
        return 0


def _lighter_tpsl_rank(order: Mapping[str, Any]) -> tuple[int, int, int, int, int, int, str]:
    return (
        _lighter_int_for_rank(order.get("updated_at")),
        _lighter_int_for_rank(order.get("created_at")),
        _lighter_int_for_rank(order.get("block_height")),
        _lighter_int_for_rank(order.get("order_index")),
        _lighter_int_for_rank(order.get("order_id")),
        _lighter_int_for_rank(order.get("client_order_index")),
        str(order.get("order_id") or ""),
    )


def _lighter_order_side(order: Mapping[str, Any]) -> Optional[str]:
    if "is_ask" in order:
        raw = order.get("is_ask")
        if isinstance(raw, bool):
            return "sell" if raw else "buy"
        if isinstance(raw, int):
            return "sell" if bool(raw) else "buy"
    side = str(order.get("side") or "").strip().lower()
    if side in {"sell", "ask"}:
        return "sell"
    if side in {"buy", "bid"}:
        return "buy"
    return None


def _classify_active_lighter_position_tpsl(
    order: Mapping[str, Any],
    positions_by_market: Mapping[int, Mapping[str, Any]],
) -> Optional[tuple[tuple[int, str, str], str, tuple[int, int, int, int, int, int, str]]]:
    if not isinstance(order, Mapping):
        return None

    type_raw = order.get("order_type") if order.get("order_type") is not None else order.get("type")
    order_type = _normalize_lighter_enum(type_raw)
    if order_type in _LIGHTER_TP_TYPES:
        leg = "take_profit"
    elif order_type in _LIGHTER_SL_TYPES:
        leg = "stop_loss"
    else:
        return None

    status = _normalize_lighter_enum(order.get("status"))
    if status not in LIGHTER_TPSL_ACTIVE_STATUSES:
        return None

    if not _lighter_truthy(order.get("reduce_only")):
        return None

    trigger_price = _lighter_trigger_price_string(order.get("trigger_price"))
    if trigger_price is None:
        return None

    trigger_status = _normalize_lighter_enum(order.get("trigger_status"))
    if trigger_status not in LIGHTER_TPSL_TRIGGER_STATUSES:
        return None

    try:
        market_index = int(order.get("market_index"))
    except (TypeError, ValueError):
        return None
    position = positions_by_market.get(market_index)
    if position is None:
        return None

    order_symbol = str(order.get("symbol") or "").strip().upper()
    position_symbol = str(position.get("symbol") or "").strip().upper()
    if order_symbol and position_symbol and order_symbol != position_symbol:
        return None

    position_side = str(position.get("side") or "").strip().lower()
    required_side = "sell" if position_side == "long" else "buy" if position_side == "short" else None
    if required_side is None or _lighter_order_side(order) != required_side:
        return None

    remaining = order.get("remaining_base_amount")
    if remaining not in (None, ""):
        try:
            rem_dec = Decimal(str(remaining))
        except (InvalidOperation, ValueError):
            return None
        if not rem_dec.is_finite() or rem_dec <= 0:
            return None

    return ((market_index, position_side, leg), trigger_price, _lighter_tpsl_rank(order))


def _apply_lighter_tpsl_enrichment(
    positions: list[dict[str, Any]],
    orders: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    positions_by_market: dict[int, dict[str, Any]] = {}
    for pos in positions:
        pos["take_profit"] = None
        pos["stop_loss"] = None
        try:
            positions_by_market[int(pos.get("market_id"))] = pos
        except (TypeError, ValueError):
            continue

    selected: dict[tuple[int, str, str], tuple[tuple[int, int, int, int, int, int, str], str]] = {}
    inspected = 0
    eligible = 0
    for order in orders:
        inspected += 1
        classified = _classify_active_lighter_position_tpsl(order, positions_by_market)
        if classified is None:
            continue
        key, trigger_price, rank = classified
        eligible += 1
        current = selected.get(key)
        if current is None or rank > current[0]:
            selected[key] = (rank, trigger_price)

    matched_positions: set[tuple[int, str]] = set()
    for (market_id, position_side, leg), (_rank, trigger_price) in selected.items():
        pos = positions_by_market.get(market_id)
        if pos is None:
            continue
        pos[leg] = trigger_price
        matched_positions.add((market_id, position_side))

    return {
        "orders_inspected": inspected,
        "eligible_trigger_count": eligible,
        "matched_position_count": len(matched_positions),
        "selected_trigger_count": len(selected),
    }



# We need ``time`` for the cache timestamps. Use a module-level alias
# so that tests can monkeypatch ``time_module`` if they ever need to.
import time as time_module


# ---------------------------------------------------------------------------
# Market-metadata cache (Phase 2B)
# ---------------------------------------------------------------------------
#
# The only authoritative source for ``market_id -> symbol`` resolution
# is the public Lighter endpoint ``/api/v1/orderBookDetails``. We
# cache the catalog in memory keyed by chain (``base_url``), and
# refresh only when:
#   * no map exists for the chain,
#   * a requested market_id is missing from the cached map, or
#   * the cache exceeds ``MARKET_MAP_TTL_SECONDS`` (default 600s).
#
# Correctness rules:
#   * The cache is the source of truth only as long as it is fresh.
#   * If a refresh fails AND a cached map exists, continue using the
#     cached authoritative map.
#   * If no cached map exists AND a refresh fails, the per-order
#     normalizer leaves ``symbol=None``. We never invent metadata.
#   * The cache is per-agent, in-memory only. It is NOT persisted
#     across gateway restarts. The very first open-orders call after
#     a restart triggers a metadata fetch.

# ``time`` is referenced inside ``_LighterMarketSymbolCache`` for
# freshness timestamps. We import it at module scope so the class can
# refer to it via ``time_module`` (the alias makes tests easier to
# monkeypatch).
import time as time_module

MARKET_MAP_TTL_SECONDS = 600  # 10 minutes
# Hard cap on pages consumed during pagination. ~5,000 active orders
# is far above any realistic operator; an unbounded loop would mask
# server bugs.
ORDER_PAGINATION_MAX_PAGES = 50


class _LighterMarketSymbolCache:
    """Per-agent in-memory cache of the public market catalog.

    The cache holds ``market_id -> symbol`` mappings for a single
    chain (``base_url``). It is keyed by ``base_url`` because two
    chains (ARBITRUM and ROBINHOOD) live in the same agent but
    maintain independent market catalogs.

    The cache is intentionally simple: a tuple of ``(fetched_at,
    mapping)``. A stale cache is treated as a missing cache and
    triggers a refresh.
    """

    __slots__ = ("_by_chain",)

    def __init__(self) -> None:
        self._by_chain: dict[str, tuple[float, dict[int, str]]] = {}

    def get(self, *, base_url: str, market_id: int) -> Optional[str]:
        """Return the cached symbol for ``market_id`` on ``base_url``,
        or ``None`` if missing / not present in the cache.

        This call does NOT trigger a refresh. Use :meth:`needs_refresh`
        + :meth:`replace` to update the cache.
        """
        entry = self._by_chain.get(base_url)
        if entry is None:
            return None
        _fetched_at, mapping = entry
        return mapping.get(int(market_id))

    def has_fresh(self, *, base_url: str) -> bool:
        """True if a fresh cached map exists for ``base_url``."""
        entry = self._by_chain.get(base_url)
        if entry is None:
            return False
        fetched_at, _mapping = entry
        return (time_module.time() - fetched_at) < MARKET_MAP_TTL_SECONDS

    def needs_refresh_for(self, *, base_url: str, market_id: int) -> bool:
        """True if the cache should be refreshed to attempt a hit
        on ``market_id``. Returns True if:
          - no map exists for ``base_url``,
          - the existing map is stale, OR
          - the existing map is fresh but does not contain ``market_id``
            (which we treat as a strong hint that the catalog may
            have grown since the last fetch).
        """
        entry = self._by_chain.get(base_url)
        if entry is None:
            return True
        fetched_at, mapping = entry
        if (time_module.time() - fetched_at) >= MARKET_MAP_TTL_SECONDS:
            return True
        return int(market_id) not in mapping

    def replace(self, *, base_url: str, mapping: Mapping[int, str]) -> None:
        """Atomically replace the cache for ``base_url``. ``mapping``
        should be a freshly-fetched authoritative catalog. The
        timestamp is captured here so that "freshness" is anchored
        to the moment of cache replacement, not to the moment of
        fetch start.
        """
        # Defensive: coerce keys to int.
        normalized = {int(k): str(v) for k, v in mapping.items()}
        self._by_chain[base_url] = (time_module.time(), normalized)

    def current_size(self, *, base_url: str) -> int:
        entry = self._by_chain.get(base_url)
        if entry is None:
            return 0
        return len(entry[1])


# ---------------------------------------------------------------------------
# Phase 3A: Recursive sanitizer, signed-payload redaction, and signed-64-bit
# constants for place_order.
#
# The sanitizer removes authenticated transaction material and credentials
# from any structured or unstructured payload before it is logged, returned,
# stored, or surfaced in an exception. We do NOT rely on substring search
# alone for structured payloads; we recursively walk mappings/lists/tuples
# and apply a normalized-key match against an explicit exact-match set plus
# a small suffix set. For strings, we apply explicit patterns for the
# specific values we expect (bearer tokens, auth query parameters,
# label-prefixed signature fields). We never redact arbitrary long hex
# strings by length alone because Lighter's server-issued ``tx_hash`` is
# itself a long hex string and must remain visible for correlation.
# ---------------------------------------------------------------------------

REDACT_TOKEN = "[REDACTED]"

# Exact normalized sensitive key fragments (after lowercasing and removing
# non-alphanumerics). The sanitizer redacts a key whose normalized form is
# in this set, replacing its value with ``REDACT_TOKEN``.
_EXACT_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "sig",
    "signature",
    "txinfo",                # covers tx_info, txinfo, Tx-Info
    "nonce",
    "privatekey",            # covers private_key, privatekey
    "apiprivatekey",         # covers api_private_key, api-private-key
    "authorization",          # covers Authorization, AUTHORIZATION
    "auth",                   # covers auth, AUTH
    "token",
    "bearer",
    "secret",
})

# Suffix match (normalized). A key whose normalized form ENDS WITH one of
# these suffixes is also redacted. ``endswith`` is used (not ``contains``)
# to avoid matching unrelated words like ``author``.
_SENSITIVE_SUFFIXES: tuple[str, ...] = (
    "token",
    "secret",
    "signature",
    "privatekey",
    "nonce",
)

# Substring match (normalized). Reserved for very narrow cases where the
# full normalized form might otherwise be missed (e.g. ``signedtxinfo``).
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "txinfo",
)

# Bearer tokens in an Authorization header or similar (with whitespace).
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}")

# ``key=value`` patterns in plain text (Lighter error bodies and SDK
# logs often serialize credentials this way). We match a small set of
# well-known sensitive keys and redact the value while preserving the
# field name. We deliberately do NOT match arbitrary keys.
# The lookbehind ``(?<![A-Za-z0-9_])`` ensures we only match keys that
# are NOT immediately preceded by an identifier character — i.e. the
# key is at a word boundary. This avoids matching ``fSig=...`` as
# ``Sig=...``.
_PLAINTEXT_KV_RE = re.compile(
    r'(?i)(?<![A-Za-z0-9_])(["\']?(?:sig|signature|nonce|auth|bearer|token|access_token|api_key|private_key|api_private_key|secret)["\']?\s*[=:]\s*)'
    r'([^\s,;"\']+)'
)
# Preserves the parameter name (and its leading ``?`` or ``&``) so the
# redacted output still parses as a URL.
_AUTH_QUERY_RE = re.compile(
    r"(?i)([?&](?:auth|token|access_token|api_key)=)[^&#\s]+"
)
# Label-prefixed signature field in JSON-like text. We redact only when
# the substring ``"Sig":`` or ``"Signature":`` (or case variants) appears
# before a quoted 0x-hex signature. This protects Lighter's ``tx_hash``
# from being redacted.
_LABELED_SIG_IN_TEXT_RE = re.compile(
    r'(?i)("(?:sig|signature)"\s*:\s*")(0x[0-9a-fA-F]{64,}|'
    r'\\"0x[0-9a-fA-F]{64,}\\")',
)

# Bare ``nonce=<digits>`` patterns in plain text (Lighter error bodies
# and SDK logs sometimes serialize nonces this way). We redact the value
# while preserving the field name.
_NONCE_ASSIGNMENT_RE = re.compile(r'(?i)(["\']?nonce["\']?\s*[=:]\s*)\d+')

# Diagnostic limit for plain-text body sanitization. We sanitize first,
# then truncate, so sensitive fields straddling the boundary are fully
# redacted.
DIAGNOSTIC_LIMIT_BYTES = 512

# Integer sentinel limits for signed-64-bit math. We use these in
# place-order wire conversion.
INT64_MAX = (1 << 63) - 1        # 9_223_372_036_854_775_807
INT64_MIN = -(1 << 63)           # -9_223_372_036_854_775_808

# Bounded verification schedule for GET-only post-read confirmation.
VERIFICATION_MIN_SLEEP_MS = 500
VERIFICATION_MAX_SLEEP_MS = 30_000
VERIFICATION_MAX_READS = 6
VERIFICATION_MAX_WALL_TIME_S = 180

# Lighter group-cancellation chunk size.
#
# Per the operator's directive: Lighter group-cancellation is capped
# at 20 orders per chunk. This limit is documented in the operator's
# production defect report ("Lighter cancellation is limited to 20
# per request"); the underlying cause is Lighter's signing pipeline,
# which signs each cancel as an independent transaction. We do NOT
# increase this constant without additional documented evidence.
#
# When a group-cancel request resolves to MORE than this many matched
# orders, the Lighter adapter transparently splits the work into
# sequential chunks of at most LIGHTER_CANCEL_CHUNK_SIZE orders. Each
# chunk is fully submitted before the next chunk begins; if any chunk
# fails, the remaining chunks are NOT attempted (stop-on-first-failure).
LIGHTER_CANCEL_CHUNK_SIZE = 20


def _normalize_key(key: Any) -> str:
    """Lowercase + strip non-alphanumerics.

    Examples:
        'Sig'         -> 'sig'
        'API-Key'     -> 'apikey'
        'private_key' -> 'privatekey'
        'Tx-Info'     -> 'txinfo'
    """
    if not isinstance(key, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _is_sensitive_key(key: Any) -> bool:
    """True if the key matches the exact, suffix, or substring sets."""
    norm = _normalize_key(key)
    if not norm:
        return False
    if norm in _EXACT_SENSITIVE_KEYS:
        return True
    if any(norm.endswith(suf) for suf in _SENSITIVE_SUFFIXES):
        return True
    if any(sub in norm for sub in _SENSITIVE_SUBSTRINGS):
        return True
    return False


def _redact_text_string(text: str) -> str:
    """Apply string-level redactions to a single string value.

    Patterns handled:

    - ``Bearer <token>``     -> ``Bearer [REDACTED]``
    - URL query params: ``?auth=``, ``&auth=``, ``?token=``,
      ``&token=``, ``?access_token=``, ``&access_token=``,
      ``?api_key=``, ``&api_key=``  -> value redacted
    - Labeled JSON-like fields ``"Sig": "0x..."`` or
      ``"Signature": "0x..."`` -> value redacted (preserves ``tx_hash``)
    - Bare ``nonce=<digits>`` patterns in text -> value redacted
      (Lighter error bodies and logs use this serialization)

    We deliberately do NOT redact arbitrary long hex strings by
    length alone so that ``tx_hash`` (also a long hex string)
    remains visible for correlation in diagnostic output.
    """
    s = _BEARER_RE.sub("Bearer [REDACTED]", text)
    s = _AUTH_QUERY_RE.sub(r"\1" + REDACT_TOKEN, s)
    s = _LABELED_SIG_IN_TEXT_RE.sub(r"\1" + REDACT_TOKEN, s)
    s = _NONCE_ASSIGNMENT_RE.sub(r"\1" + REDACT_TOKEN, s)
    s = _PLAINTEXT_KV_RE.sub(r"\1" + REDACT_TOKEN, s)
    return s


def _sanitize_value(obj: Any) -> Any:
    """Recursively sanitize a JSON-like value.

    - Mappings: each (key, value) is recursively sanitized. If the key is
      sensitive (per ``_is_sensitive_key``), the value is replaced with
      ``REDACT_TOKEN`` rather than recursively sanitized.
    - Lists/tuples: each element is recursively sanitized.
    - Strings: ``_redact_text_string`` is applied.
    - Other primitives: returned unchanged.
    """
    if isinstance(obj, Mapping):
        return {
            k: (REDACT_TOKEN if _is_sensitive_key(k) else _sanitize_value(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_sanitize_value(v) for v in obj]
    if isinstance(obj, str):
        return _redact_text_string(obj)
    return obj


def _sanitize_sensitive_data(obj: Any) -> Any:
    """Exception-safe wrapper around ``_sanitize_value``.

    If the sanitizer itself raises (e.g. unexpected types, custom
    objects with broken ``__iter__``), this returns a bounded fallback
    that does NOT include the original raw object.
    """
    try:
        return _sanitize_value(obj)
    except Exception:
        return {
            "sanitization_error": True,
            "detail": REDACT_TOKEN,
        }


def _truncate_text(text: str, limit_bytes: int = DIAGNOSTIC_LIMIT_BYTES) -> str:
    """Truncate a UTF-8 string to ``limit_bytes`` bytes without splitting
    a UTF-8 codepoint.

    We always sanitize BEFORE truncating so a sensitive field straddling
    the truncation boundary is fully redacted first.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    truncated = encoded[:limit_bytes].decode("utf-8", errors="replace")
    return truncated + f" [TRUNCATED to {limit_bytes} bytes]"


def _sanitize_http_body(
    response: "Any",
    limit_bytes: int = DIAGNOSTIC_LIMIT_BYTES,
) -> dict:
    """Build a sanitized diagnostic body from a ``requests.Response``.

    1. Try parsing JSON.
       a. Success -> recursively sanitize the parsed object.
       b. Failure -> sanitize the complete text, then truncate.
    2. Return a sanitized dict. NEVER include the raw, unsanitized text.
    """
    text = (getattr(response, "text", "") or "")
    try:
        parsed = response.json()
    except Exception:
        sanitized_text = _redact_text_string(text)
        return {"_text_head": _truncate_text(sanitized_text, limit_bytes)}
    return _sanitize_sensitive_data(parsed)


# -----------------------------------------------------------------------------
# Result-classification helper (Phase 6 result-classification refinement).
# -----------------------------------------------------------------------------
#
# When a single child of a batch_orders submission fails with a Lighter
# HTTP error, the inner envelope's ``exchange_response`` field already
# carries the SANITIZED diagnostic payload produced by ``place_order``
# at the HTTP-client layer:
#
#   exchange_response = {
#     "diagnostics": {
#       "market_index": ...,
#       "client_order_index": ...,
#       "base_url": ...,
#     },
#     "body": <already-sanitized JSON-or-text bounded by DIAGNOSTIC_LIMIT_BYTES>,
#   }
#
# The batch dispatcher must preserve these fields so that:
#
#   - the failed per-child result records the structured diagnostic
#     (``exchange_response``) for downstream rendering, AND
#   - the batch envelope's top-level ``stopped_diagnostic`` carries a
#     canonical structured summary so the operator can see WHY the
#     submission failed (without needing to re-run anything).
#
# This helper is the SINGLE place that builds the canonical structured
# diagnostic from an inner envelope. The dispatcher's failure-handling
# branch calls this once and propagates the result. Body size is
# bounded by ``DIAGNOSTIC_LIMIT_BYTES`` upstream.
#
# NEVER include: private keys, API secrets, auth tokens, signatures,
# raw tx_info, or raw nonce credentials. The ``body`` field is already
# sanitized by ``_sanitize_http_body`` at the HTTP-client layer.
_BODY_DIAGNOSTIC_KEYS = (
    # Keys we lift out of a JSON body if present (already sanitized
    # by the HTTP-client layer). The values MUST be strings; we do
    # not preserve nested objects because they may contain
    # sensitive material from the upstream exchange.
    "code",
    "message",
    "error",
    "err",
    "reason",
    "detail",
)


def _extract_body_exchange_reason(body: Any) -> tuple[Any, Any]:
    """Return ``(exchange_code, exchange_message)`` from a sanitized body.

    ``body`` is either:
      - a dict (already-sanitized JSON; keys may be ``code`` /
        ``message`` / ``error`` / etc.), or
      - a dict ``{"_text_head": "..."}`` (sanitized plain-text), or
      - ``None`` or any other non-dict shape (we treat it as empty).

    Returns ``(code, message)`` where each is a string or ``None``.
    """
    if not isinstance(body, dict):
        return (None, None)
    code: Any = None
    message: Any = None
    # Prefer explicit keys in canonical order.
    for k in _BODY_DIAGNOSTIC_KEYS:
        v = body.get(k)
        if v is None:
            continue
        # Accept string or int codes/values. We do NOT accept nested
        # objects or lists because they may contain sensitive
        # material from the upstream exchange. We keep the original
        # Python type so the operator can compare ``exchange_code``
        # directly against the numeric HTTP status when both are ints.
        if isinstance(v, (str, int)):
            if k in ("code", "error", "err"):
                if code is None:
                    code = v
            else:
                if message is None:
                    message = v
    if message is None:
        # Plain-text body: fall back to the truncated text head.
        text_head = body.get("_text_head")
        if isinstance(text_head, str) and text_head.strip():
            message = text_head.strip()
    return (code, message)


def _build_placement_diagnostic_from_inner(
    inner: Mapping[str, Any],
    *,
    endpoint: str,
) -> dict[str, Any]:
    """Build a canonical structured placement-diagnostic from an inner
    batch-child envelope. Returns a NEW dict each call.

    Output shape::

        {
          "http_status": 400,
          "endpoint": "/api/v1/sendTx",
          "exchange_code": "...",
          "exchange_message": "...",
          "response_body": <sanitized bounded body>,
          "market_index": 1,
          "client_order_index": "...",
        }

    All fields are optional. Fields whose source is absent are omitted
    from the dict (no nulls). Sensitive fields are NEVER present:
    ``_sanitize_http_body`` and ``_sanitize_sensitive_data`` are
    applied at the HTTP-client layer, BEFORE this helper sees the
    body.
    """
    out: dict[str, Any] = {"endpoint": endpoint}
    err_response = inner.get("exchange_response") if isinstance(
        inner, Mapping
    ) else None
    diagnostics: dict[str, Any] = {}
    body: Any = None
    http_status: Any = None
    if isinstance(err_response, Mapping):
        raw_diag = err_response.get("diagnostics")
        if isinstance(raw_diag, Mapping):
            diagnostics = dict(raw_diag)
        body = err_response.get("body")
        # ``_place_order_locked`` puts the HTTP status code under
        # ``exchange_response["status"]`` on the LighterHTTPError path.
        # That is the canonical source.
        status_from_resp = err_response.get("status")
        if isinstance(status_from_resp, int):
            http_status = status_from_resp
    # http_status fallback: also try the inner envelope directly.
    if http_status is None and isinstance(inner, Mapping):
        hs = inner.get("http_status")
        if isinstance(hs, int):
            http_status = hs
    if http_status is None and diagnostics.get("status") is not None:
        http_status = diagnostics.get("status")
    if http_status is not None:
        out["http_status"] = int(http_status)
    # exchange code/message from body
    code, message = _extract_body_exchange_reason(body)
    if code is not None:
        out["exchange_code"] = code
    if message is not None:
        out["exchange_message"] = message
    if body is not None:
        out["response_body"] = body
    # market_index / client_order_index from diagnostics or inner
    mi = diagnostics.get("market_index")
    if mi is None and isinstance(inner, Mapping):
        mi = inner.get("market_id")
    if mi is not None:
        out["market_index"] = mi
    coi = diagnostics.get("client_order_index")
    if coi is None and isinstance(inner, Mapping):
        coi = inner.get("client_order_index")
    if coi is not None:
        out["client_order_index"] = str(coi)
    return out
# ---------------------------------------------------------------------------
# Phase 3A: Decimal helpers for exact-scale validation (signed-64-bit range).
# ---------------------------------------------------------------------------


def _price_increment(price_decimals: int) -> Decimal:
    return Decimal(1).scaleb(-int(price_decimals))


def _size_increment(size_decimals: int) -> Decimal:
    return Decimal(1).scaleb(-int(size_decimals))


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("increment must be > 0")
    scaled = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
    return scaled * increment


def _ceil_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("increment must be > 0")
    scaled = (value / increment).to_integral_value(rounding=ROUND_CEILING)
    return scaled * increment


# -----------------------------------------------------------------------------
# Lighter-specific ladder price-band guard — REMOVED.
#
# The 5% defensive symmetric band preflight (added in commit 60d4478) has
# been removed because it was based on an incorrect assumption about
# Lighter's server-side rule. Empirically:
#
#   - Manual SELL limit orders at prices up to +365.8% above mark are
#     accepted by Lighter.
#   - The server-side rule is asymmetric or BUY-side only and is NOT
#     documented or exposed via the public orderBookDetails metadata.
#   - A constant-based client-side guard rejected legitimate SELL ladders.
#
# The exchange is now the authoritative validator for allowable price
# distance. Server-side rejections (HTTP 400 with code 21734 "limit
# order price is too far from the mark price") are surfaced through
# the existing diagnostic propagation pipeline introduced at d1a81cd.
# -----------------------------------------------------------------------------

def _exact_scale_to_wire(
    decimal_str: str, *, decimals: int, field_name: str,
    allow_zero: bool = False,
) -> int:
    """Convert a decimal-string field to its integer wire value with
    exact scaling and signed-64-bit range checks.

    ``allow_zero`` permits the value 0 (e.g. for ``trigger_price=0``
    to indicate "no trigger"). Without this, 0 is rejected by the
    ``value > 0`` check, which is correct for most fields but wrong
    for sentinel-zero fields.
    """
    if not isinstance(decimal_str, str):
        raise ValueError(
            f"{field_name}: must be a string, got {type(decimal_str).__name__}"
        )
    try:
        value = Decimal(decimal_str)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"{field_name}: malformed decimal {decimal_str!r}: {exc}"
        ) from exc
    if not value.is_finite():
        raise ValueError(f"{field_name}: not finite ({value})")
    if allow_zero:
        if value < 0:
            raise ValueError(f"{field_name}: must be >= 0 (got {value})")
    else:
        if value <= 0:
            raise ValueError(f"{field_name}: must be > 0 (got {value})")
    scaled = value * (Decimal(10) ** int(decimals))
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"{field_name}: {decimal_str!r} does not fit within "
            f"{decimals} decimal places (scaled to {scaled})"
        )
    wire = int(scaled)
    if wire < INT64_MIN or wire > INT64_MAX:
        raise ValueError(
            f"{field_name}: wire value {wire} overflows signed 64-bit"
        )
    return wire


def _scale_from_wire(wire_int: int, decimals: int) -> str:
    d = Decimal(int(wire_int)) / (Decimal(10) ** int(decimals))
    s = f"{d:.{int(decimals)}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# SDK's accepted protocol range for ``client_order_index``.
# Lighter's native signing routine rejects values greater than this.
# See live verification at commit 2e9bd00: the 63-bit generator
# produced ``3,434,719,424,021,049,306`` which failed signing with
# ``ClientOrderIndex should not be larger than 281474976710655``.
_CLIENT_ORDER_INDEX_MAX = (1 << 48) - 1  # 281_474_976_710_655


def _generate_client_order_index() -> int:
    """Generate a unique positive integer in [1, 2^48 - 1] for one
    ``place_order`` attempt.

    Range: ``[1, 2**48 - 1]`` (= ``[1, 281_474_976_710_655]``).

    Uses ``secrets.randbits(48)`` (cryptographically strong) and
    discards the single zero outcome (probability 2**-48). The
    upper bound matches Lighter's native SDK constraint; values
    outside this range are rejected by the signing routine.

    Although the prior code allowed ``[1, 2**63 - 1]``, this proved
    unworkable against Lighter's protocol. The narrower 48-bit
    range still provides 281 trillion distinct client_order_index
    values — sufficient for practical use and safe for the
    post-read verification via ``client_order_index``.
    """
    while True:
        candidate = secrets.randbits(48)
        if candidate != 0:
            return candidate


# ---------------------------------------------------------------------------
# Response-classification helpers (Phase 3A bugfix-2: live-verification
# follow-up).
#
# When Lighter returns code=200 with a non-empty ``message`` field,
# the message may be:
#   - empty / None          → success path
#   - a known non-blocking advisory (e.g. "didn't use volume quota")
#   - an unknown string we cannot confidently classify
#
# Conservative policy (preserved): unknown messages continue to be
# treated as ambiguous until explicitly supported. We only relax
# classification for messages we can positively identify as advisory,
# via a documented live verification (see the Phase 3A freeze doc).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Response-classification helper (Phase 6 final-cleanup follow-up).
# ---------------------------------------------------------------------------
#
# Lighter's `/api/v1/sendTx` response message is a JSON-encoded shape
# that carries an informational ratelimit advisory alongside a normal
# successful submission. We classify the message into one of:
#
#   "empty"     → no message; success path.
#   "advisory"  → a known non-blocking advisory. The order was in fact
#                 processed by the server.
#   "unknown"   → anything else. Treated conservatively as ambiguous.
#
# Existing advisory fragments (verified at commit c6770c7):
#     "didn't use volume quota"
#     "didnt use volume quota"
#     "did not use volume quota"
#
# New advisory observed during the live ladder verification at commit
# 60d4478: a "<integer> volume quota remained" advisory that the
# exchange uses when the order is accepted but a future rate-limit
# warning is attached. The integer is the volume_quota_remaining
# field that the server echoes in the same response. Variants:
#
#     "14999999 volume quota remained"
#     "1 volume quota remained"
#     "14999999 volume quota remaining"   (slightly different verb form)
#     "volume quota remained: 14999999"   (colon-separated)
#
# We classify these via a dedicated narrow helper. The check is
# case-insensitive, requires a positive integer on at least one side
# of the literal "volume quota remained" / "volume quota remaining",
# and rejects any input that contains ONLY the words "quota" or
# "remained" without the surrounding literal (per the operator's
# directive to keep the pattern narrow).
#
# IMPORTANT: the success decision is NOT made solely by this helper.
# The existing flow in `_place_order_locked` requires:
#   - HTTP status code == 200,
#   - non-empty tx_hash,
#   - message classified as "empty" or "advisory".
# This helper ONLY contributes the "advisory" classification; it does
# not weaken the positive-evidence requirements.
import re

# Narrow patterns for the "volume quota remained / remaining" advisory.
# Each pattern REQUIRES a positive integer on at least one side of
# the literal "volume quota remained" (or "volume quota remaining").
# Case-insensitive. We deliberately do NOT include the bare words
# "quota" or "remained" as standalone fragments.
_VOLUME_QUOTA_REMAINED_RE = re.compile(
    r"""
    (?P<n1>\d{1,18})\s*        # optional leading positive integer
    \bvolume\s+quota\s+
    (?:remained|remaining)\b    # verb form: remained or remaining
    |
    \bvolume\s+quota\s+
    (?:remained|remaining)\b    # verb form
    \s*[:,]?\s*                # optional separator
    (?P<n2>\d{1,18})           # trailing positive integer
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _is_known_volume_quota_advisory(message: Any) -> bool:
    """Return True iff ``message`` matches a known volume-quota-remaining
    advisory pattern AND the matched substring is bounded by an
    integer on at least one side.

    The check is intentionally narrow: it requires a positive integer
    adjacent to the literal phrase "volume quota remained" (or
    "volume quota remaining"). Bare occurrences of "quota" or
    "remained" without an integer do NOT match.

    Returns False for empty messages and for messages that are not
    strings. Side effects: none.
    """
    if not isinstance(message, str):
        return False
    s = message.strip()
    if not s:
        return False
    m = _VOLUME_QUOTA_REMAINED_RE.search(s)
    if m is None:
        return False
    n1 = (m.group("n1") or "").strip()
    n2 = (m.group("n2") or "").strip()
    # Require at least one positive integer adjacent to the literal.
    # We use the Lighter server's documented format which always
    # echoes a positive integer in `volume_quota_remaining`.
    return bool(n1 or n2)
# Substring fragments (case-insensitive) that designate a known
# non-blocking advisory. The matched message string is the rendered
# JSON shape returned by Lighter, e.g. ``{"ratelimit":"didn't use
# volume quota"}``.
_KNOWN_ADVISORY_FRAGMENTS: tuple[str, ...] = (
    # Verified at commit c6770c7's live submission: a rate-limit note
    # that does NOT block order creation. The order was confirmed
    # via accountActiveOrders with block_height=294730828.
    "didn't use volume quota",
    "didnt use volume quota",
    "did not use volume quota",
)


def _classify_lighter_sendtx_message(message: Any) -> str:
    """Classify the ``message`` field of a ``POST /api/v1/sendTx``
    response into one of:

      ``"empty"``     → no message; success path.
      ``"advisory"``  → a known non-blocking advisory. The order
                        was in fact processed by the server
                        (see the Phase 3A freeze doc for the live
                        verification that proved this).
      ``"unknown"``   → anything else. We treat this conservatively
                        as ambiguous and refuse to relax
                        classification until the message is
                        positively identified.
    """
    if message is None:
        return "empty"
    s = str(message).strip()
    if not s:
        return "empty"
    s_lower = s.lower()
    # Narrow volume-quota-remaining advisory pattern (regex-based).
    if _is_known_volume_quota_advisory(s):
        return "advisory"
    # Existing substring fragments.
    for frag in _KNOWN_ADVISORY_FRAGMENTS:
        if frag in s_lower:
            return "advisory"
    return "unknown"


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class LighterAgent:
    """Hermes-side Lighter exchange agent. Phase 1: read-only authenticated balance.

    The agent receives the account identifier only; the chain is
    derived internally from ``LIGHTER_<account>_CHAIN`` in the
    operator's environment. There is no chain argument in the request
    shape, no chain field in the wizard, and no possibility of an
    (account, chain) mismatch at the request layer.
    """

    SUPPORTED_OPERATIONS = SUPPORTED_OPERATIONS

    def __init__(self, *, http_client: Any = None) -> None:
        # Per-chain HTTP client cache: chain -> LighterHttpClient
        self._http_clients: dict[str, Any] = {}
        # Tests may inject a single http_client that handles all chains.
        # In production, http_client is None and we construct real
        # per-chain clients lazily.
        self._injected_http_client = http_client
        # Phase 2B: per-chain market-symbol cache for ``market_id ->
        # symbol`` resolution. Populated lazily from the public
        # ``/api/v1/orderBookDetails`` endpoint. TTL-bounded and
        # invalidated when a missing market_id is requested.
        self._market_symbol_cache: _LighterMarketSymbolCache = (
            _LighterMarketSymbolCache()
        )
        # Phase 3A: per-(chain, account, api_key) synchronous lock for
        # the nonce/sign/POST/sendTx sequence. Lazily created. An
        # additional guard protects against concurrent dict mutation.
        self._nonce_locks: dict[tuple[str, int, int], threading.Lock] = {}
        self._nonce_locks_guard = threading.Lock()
        # Per-trade-call invariant: each _place_order invocation issues
        # EXACTLY ONE POST /api/v1/sendTx. The single-trade cap is
        # enforced *inside* each dispatcher method via counters on the
        # HTTP client (the HTTP client method increments a post-count
        # per call and refuses more than one POST per call). The
        # dispatcher itself may be called any number of times by the
        # wizard across many trades.
        # NOTE: Phase 3A historically also tracked a per-agent-lifetime
        # call cap (``_place_order_call_count = 0; <=_max = 1``),
        # which Lighter alone added. That cap was suited only to the
        # single-shot operator-invoked live verification scenario and
        # is incompatible with the canonical wizard path, where a
        # single agent instance handles many trades over time. It has
        # been removed to restore canonical behavior matched by all
        # other frozen exchanges.

    # -- account discovery -----------------------------------------------

    def list_accounts(self) -> dict:
        """Return the discovered (account, chain) tuples with credentials.

        The wizard uses this to render the Lighter account-selection menu
        with each pair labeled as ``"<account> — <chain label>"``.
        """
        accounts = discover_lighter_accounts()
        return {
            "success": True,
            "exchange": "lighter",
            "accounts": [
                {"account": a.account, "chain": a.chain, "label": a.label()}
                for a in accounts
            ],
        }

    # -- main dispatch ---------------------------------------------------

    def execute(self, request: Mapping[str, Any]) -> dict:
        if not isinstance(request, Mapping):
            return _execution_result(
                request,
                success=False,
                error="StructuredTradeRequest must be a mapping",
            )
        operation = str(request.get("operation") or "")
        if operation == "balance":
            return self._balance(request)
        if operation == "positions":
            return self._positions(request)
        if operation == "open_orders":
            return self._open_orders(request)
        # Lighter Position Manager dispatch:
        # set_tp / set_sl — Take-profit / stop-loss standalone orders
        # built from a normalized TradeDesk passthrough. The wizard
        # sends a single logical request with fields {operation,
        # symbol, side, price}; we sign + POST a Lighter TP/SL order
        # using the same Phase 3A nonce-lock / sanitize pipeline.
        if operation == "set_tp":
            return self._set_tp(request)
        if operation == "set_sl":
            return self._set_sl(request)
        # Canonical TradeDesk contract: "order" is the internal
        # operation emitted by normalize() when the user submits
        # place_order. The internal helper is still _place_order
        # (renamed implementation detail, not a contract).
        if operation == "order":
            return self._place_order(request)
        # Canonical TradeDesk contract: "cancel_orders" (plural).
        if operation == "cancel_orders":
            return self._cancel_order(request)
        if operation == "batch_orders":
            return self._batch_orders(request)
        return _execution_result(
            request,
            success=False,
            error=f"Unsupported Lighter operation: {operation}",
        )

    # -- shared authenticated read (Phase 2A: single source of truth) ---
    #
    # This is the only path that issues an authenticated HTTP GET against
    # the Lighter ``/api/v1/account`` endpoint. Both ``_balance`` and
    # ``_positions`` call it. Auth (SignerClient + bearer mint),
    # credentials (``_resolve_account_credentials``), chain routing
    # (``_get_chain_config``), HTTP transport (``LighterHttpClient.account``
    # — sync ``requests.get`` with ``?auth=...``), and structured
    # error wrapping (``_execution_result``) all flow through this
    # single helper. There is intentionally no second HTTP method.

    def _fetch_account_entry(self, request: Mapping[str, Any]) -> dict:
        """Perform one authenticated /api/v1/account read and return
        ``{"raw": <server payload>, "target": <account entry>}``.

        Auth, credentials, chain, transport, and error handling all
        inherit from the frozen Phase 1 path. On any failure the
        returned dict contains an ``"_execution": <execution result>``
        key carrying the already-formed structured error, so callers
        can ``return result["_execution"]`` for early exit without
        reimplementing the error shape.
        """
        account = str(request.get("account") or "").strip().lower()
        if not account:
            return {
                "_execution": _execution_result(
                    request,
                    success=False,
                    error="missing account name",
                )
            }
        # Credentials (chain derived from LIGHTER_<account>_CHAIN).
        try:
            creds = _resolve_account_credentials(account)
        except ValueError as exc:
            return {
                "_execution": _execution_result(
                    request,
                    success=False,
                    error=str(exc),
                    account=account,
                )
            }
        chain = creds["chain"]
        try:
            label, base_url = _get_chain_config(chain)
        except ValueError as exc:
            return {
                "_execution": _execution_result(
                    request,
                    success=False,
                    error=str(exc),
                    account=account,
                    chain=chain,
                )
            }
        try:
            client = self._http_client_for_chain(chain, base_url, creds)
            raw = client.account(creds["account_index"])
        except LighterHTTPError as exc:
            return {
                "_execution": _execution_result(
                    request,
                    success=False,
                    error=str(exc),
                    account=account,
                    chain=chain,
                    exchange_response=exc.diagnostics,
                )
            }
        except Exception as exc:
            return {
                "_execution": _execution_result(
                    request,
                    success=False,
                    error=f"Lighter authenticated read failed: "
                          f"{type(exc).__name__}: {exc}",
                    account=account,
                    chain=chain,
                )
            }

        # Pick the account entry that matches the requested account_index.
        accounts = raw.get("accounts") if isinstance(raw, Mapping) else None
        if not isinstance(accounts, list) or not accounts:
            return {
                "_execution": _execution_result(
                    request,
                    success=False,
                    error=(
                        f"Lighter account {account} is not found on "
                        f"configured chain {label}"
                    ),
                    account=account,
                    chain=chain,
                    exchange_response=raw,
                )
            }

        target = None
        target_index = creds["account_index"]
        for entry in accounts:
            if not isinstance(entry, Mapping):
                continue
            if (int(entry.get("account_index") or 0) == target_index
                    or int(entry.get("index") or 0) == target_index):
                target = entry
                break
        if target is None and isinstance(accounts[0], Mapping):
            target = accounts[0]

        return {
            "_raw": raw if isinstance(raw, Mapping) else {},
            "target": target if isinstance(target, Mapping) else {},
            "account": account,
            "chain": chain,
            "label": label,
        }

    # -- balance ----------------------------------------------------------

    def _balance(self, request: Mapping[str, Any]) -> dict:
        loaded = self._fetch_account_entry(request)
        if "_execution" in loaded:
            return loaded["_execution"]
        target = loaded["target"]
        account = loaded["account"]
        chain = loaded["chain"]
        label = loaded["label"]
        raw = loaded["_raw"]

        balance = _to_hermes_balance(target)

        # Synthesize a top-level marginSummary / withdrawable so the
        # existing _format_balance_message renderer can read them.
        exchange_response = dict(raw)
        account_entry = target
        exchange_response["marginSummary"] = {
            "accountValue": str(account_entry.get("total_asset_value") or "0"),
            "totalMarginUsed": str(account_entry.get("cross_initial_margin_requirement") or "0"),
            "totalNtlPos": str(account_entry.get("cross_asset_value") or "0"),
        }
        exchange_response["withdrawable"] = str(account_entry.get("available_balance") or "0")
        # Wrap each normalized Lighter position (Rise-style field
        # names) into Hyperliquid-style envelopes
        # ({"position": {coin, szi, entryPx, unrealizedPnl,
        # liquidationPx}}) so the exchange-agnostic
        # _format_balance_message renderer can display them without
        # any exchange-specific branches.
        #
        # The raw positions from the Lighter API use
        # wire-format keys (``sign``, ``position``, ``avg_entry_price``,
        # ``unrealized_pnl``, etc.) which the renderer can't read
        # directly. We normalize them via the existing
        # ``_hermes_normalize_lighter_positions`` helper so the wrap
        # operates on the canonical Hermes-standard keys.
        raw_positions = list(account_entry.get("positions") or [])
        normalized_positions, _active_count = _hermes_normalize_lighter_positions(
            raw_positions, chain=chain, account=account,
        )
        wrapped_positions: list = []
        for pos in normalized_positions:
            if not isinstance(pos, Mapping):
                continue
            try:
                size = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0
            if size <= 0:
                continue
            side = str(pos.get("side") or "").lower()
            # Hyperliquid convention: szi is signed (+ long, - short).
            szi = size if side != "short" else -size
            symbol = str(pos.get("symbol") or "").upper()
            wrapped_positions.append({
                "position": {
                    "coin": symbol,
                    "szi": str(szi),
                    "entryPx": pos.get("entry_price"),
                    "unrealizedPnl": pos.get("unrealized_pnl"),
                    "liquidationPx": pos.get("liquidation_price"),
                }
            })
        exchange_response["positions"] = wrapped_positions
        exchange_response["assetPositions"] = wrapped_positions

        return _execution_result(
            request,
            success=True,
            account=account,
            chain=chain,
            exchange_response=exchange_response,
            balance=balance,
            positions=wrapped_positions,
        )

    # -- positions (Phase 2A: read-only) -------------------------------

    def _positions(self, request: Mapping[str, Any]) -> dict:
        loaded = self._fetch_account_entry(request)
        if "_execution" in loaded:
            return loaded["_execution"]
        target = loaded["target"]
        account = loaded["account"]
        chain = loaded["chain"]
        raw = loaded["_raw"]

        raw_positions = list(target.get("positions") or [])
        normalized, active_count = _hermes_normalize_lighter_positions(
            raw_positions, chain=chain, account=account,
        )
        enrichment: dict[str, Any] = {
            "success": True,
            "source": "accountActiveOrders",
            "pages_fetched": 0,
            "total_active_orders_inspected": 0,
            "eligible_trigger_count": 0,
            "matched_position_count": 0,
            "pagination_complete": True,
            "accepted_statuses": sorted(LIGHTER_TPSL_ACTIVE_STATUSES),
            "accepted_trigger_statuses": sorted(LIGHTER_TPSL_TRIGGER_STATUSES),
        }
        if normalized:
            try:
                creds = _resolve_account_credentials(account)
                _label, base_url = _get_chain_config(chain)
                client = self._http_client_for_chain(chain, base_url, creds)
                fetched = self._fetch_lighter_active_tpsl_orders(
                    client=client, account_index=creds["account_index"]
                )
                enrichment.update(fetched["diagnostic"])
                stats = _apply_lighter_tpsl_enrichment(
                    normalized, fetched.get("orders") or []
                )
                enrichment["total_active_orders_inspected"] = stats["orders_inspected"]
                enrichment["eligible_trigger_count"] = stats["eligible_trigger_count"]
                enrichment["matched_position_count"] = stats["matched_position_count"]
                enrichment["selected_trigger_count"] = stats["selected_trigger_count"]
            except Exception as exc:
                for pos in normalized:
                    pos["take_profit"] = None
                    pos["stop_loss"] = None
                enrichment.update({
                    "success": False,
                    "errors": [{
                        "error_type": type(exc).__name__,
                        "error": str(_sanitize_sensitive_data(str(exc))),
                    }],
                })

        # Phase 1 surfaces the raw positions array under
        # ``exchange_response.positions``. We preserve that contract
        # so the existing balance renderer keeps working, and we add
        # ``positions`` at the top level carrying the normalized list.
        exchange_response = dict(raw)
        exchange_response["positions"] = raw_positions

        result = _execution_result(
            request,
            success=True,
            account=account,
            chain=chain,
            exchange_response=exchange_response,
        )
        # Preserve the canonical Phase 1 fallback fields (_balance also
        # synthesizes them, harmless duplication) and attach the new
        # normalized positions list at the top level.
        account_entry = target
        exchange_response["marginSummary"] = {
            "accountValue": str(account_entry.get("total_asset_value") or "0"),
            "totalMarginUsed": str(account_entry.get("cross_initial_margin_requirement") or "0"),
            "totalNtlPos": str(account_entry.get("cross_asset_value") or "0"),
        }
        exchange_response["withdrawable"] = str(account_entry.get("available_balance") or "0")
        result["positions"] = normalized
        result["positions_active_count"] = active_count
        result["positions_total_count"] = len(raw_positions)
        result["tpsl_enrichment"] = enrichment
        return result

    # -- open_orders (Phase 2B: read-only, paginated) -----------------

    def _open_orders(self, request: Mapping[str, Any]) -> dict:
        """Authenticated read-only ``open_orders`` dispatcher.

        Reuses Phase 1/2A's credential-resolution / chain-routing
        helpers. Adds two new things:

          1. A paginated cursor loop over
             ``/api/v1/accountActiveOrders`` (follows ``next_cursor``
             until absent or empty; aggregate; then normalize).
          2. A market-symbol cache populated lazily from the public
             ``/api/v1/orderBookDetails`` endpoint. ``symbol`` is
             attached only when the authoritative map has a match.
             We never fabricate exchange metadata.
        """
        account = str(request.get("account") or "").strip().lower()
        if not account:
            return _execution_result(
                request, success=False, error="missing account name",
            )
        # Optional market_id filter on the request (string or int).
        market_id_filter = self._market_id_from_request(request)

        # Resolve credentials (chain derived from LIGHTER_<account>_CHAIN).
        try:
            creds = _resolve_account_credentials(account)
        except ValueError as exc:
            return _execution_result(
                request, success=False, error=str(exc), account=account,
            )
        chain = creds["chain"]
        try:
            label, base_url = _get_chain_config(chain)
        except ValueError as exc:
            return _execution_result(
                request, success=False, error=str(exc),
                account=account, chain=chain,
            )

        # Build a per-client-call symbol map. We may need to refresh
        # the cache if a market_id is missing from the cached map.
        # Collect market_ids we will need to resolve AFTER the
        # pagination loop, then refresh the cache once if any are
        # missing (single refresh for the whole request).
        market_ids_to_resolve: set[int] = set()
        symbol_map = self._effective_symbol_map(base_url)

        # Pagination loop: fetch every page of active orders.
        try:
            client = self._http_client_for_chain(chain, base_url, creds)
            loaded = self._fetch_all_active_orders_raw(
                client=client,
                account_index=creds["account_index"],
                market_id=market_id_filter,
            )
        except LighterHTTPError as exc:
            return _execution_result(
                request, success=False, error=str(exc),
                account=account, chain=chain,
                exchange_response=exc.diagnostics,
            )
        except Exception as exc:
            return _execution_result(
                request, success=False,
                error=(
                    f"Lighter open-orders fetch failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                account=account, chain=chain,
            )

        raw_orders = loaded["orders"]

        # Collect the market_ids we need to resolve. If any are
        # missing from the cached map, refresh the cache once and
        # retry the lookup. We deliberately do NOT refresh on every
        # call — only when a missing market_id is observed, or when
        # the cache is stale. This is the per-call optimization.
        for raw in raw_orders:
            if isinstance(raw, Mapping):
                mi = int(raw.get("market_index") or 0)
                if mi:
                    market_ids_to_resolve.add(mi)

        # Determine whether we need a refresh.
        needs_refresh = False
        for mid in market_ids_to_resolve:
            if self._market_symbol_cache.needs_refresh_for(
                base_url=base_url, market_id=mid,
            ):
                needs_refresh = True
                break
        if needs_refresh:
            refresh_outcome = self._refresh_market_symbol_map(base_url)
            if refresh_outcome.get("ok"):
                symbol_map = self._effective_symbol_map(base_url)

        # Aggregate raw orders (already aggregated by the pagination loop).
        normalized = _hermes_normalize_lighter_open_orders(
            raw_orders,
            chain=chain, account=account, symbol_map=symbol_map,
        )

        # Carry the LAST raw page on the result for diagnostics
        # (paginated responses are too bulky to keep every page).
        # Also keep a compact summary of the pagination metadata.
        exchange_response = dict(loaded["final_raw"]) if isinstance(
            loaded.get("final_raw"), Mapping,
        ) else {}
        exchange_response["_pagination"] = {
            "page_count": loaded["page_count"],
            "raw_order_count": loaded["raw_order_count"],
            "final_next_cursor": loaded["final_next_cursor"],
            "metadata_cache_refreshed": bool(needs_refresh),
            "metadata_cache_size": self._market_symbol_cache.current_size(
                base_url=base_url,
            ),
        }

        return _execution_result(
            request,
            success=True,
            account=account, chain=chain,
            exchange_response=exchange_response,
            orders=normalized,
            open_order_count=len(normalized),
            raw_order_count=loaded["raw_order_count"],
            page_count=loaded["page_count"],
            final_next_cursor=loaded["final_next_cursor"],
        )

    # -- TP/SL enrichment active-order traversal -----------------------

    def _fetch_lighter_active_tpsl_orders(
        self, *, client: Any, account_index: int,
    ) -> dict[str, Any]:
        """Fetch every active-order page once for Lighter position TP/SL.

        This is separate from the normal ``open_orders`` operation so TP/SL
        position enrichment cannot alter open-orders normalization, menus,
        counts, or cancellation behavior.
        """
        orders: list[Mapping[str, Any]] = []
        seen_cursors: set[str] = set()
        cursor: Optional[str] = None
        pages_fetched = 0
        final_next_cursor: Optional[str] = None
        warnings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        pagination_complete = True

        while pages_fetched < ORDER_PAGINATION_MAX_PAGES:
            try:
                page = client.account_active_orders(
                    account_index, market_id=None, cursor=cursor,
                )
            except Exception as exc:
                return {
                    "orders": orders,
                    "diagnostic": {
                        "success": False,
                        "source": "accountActiveOrders",
                        "pages_fetched": pages_fetched,
                        "final_next_cursor": final_next_cursor,
                        "pagination_complete": False,
                        "errors": [{
                            "error_type": type(exc).__name__,
                            "error": str(_sanitize_sensitive_data(str(exc))),
                        }],
                    },
                }

            pages_fetched += 1
            if not isinstance(page, Mapping):
                return {
                    "orders": orders,
                    "diagnostic": {
                        "success": False,
                        "source": "accountActiveOrders",
                        "pages_fetched": pages_fetched,
                        "final_next_cursor": final_next_cursor,
                        "pagination_complete": False,
                        "errors": [{
                            "error_type": type(page).__name__,
                            "error": "malformed accountActiveOrders envelope: non-mapping page",
                        }],
                    },
                }
            page_orders = page.get("orders")
            if page_orders is None:
                page_orders = []
            if not isinstance(page_orders, list):
                return {
                    "orders": orders,
                    "diagnostic": {
                        "success": False,
                        "source": "accountActiveOrders",
                        "pages_fetched": pages_fetched,
                        "final_next_cursor": final_next_cursor,
                        "pagination_complete": False,
                        "errors": [{
                            "error_type": type(page_orders).__name__,
                            "error": "malformed accountActiveOrders envelope: orders is not a list",
                        }],
                    },
                }
            for order in page_orders:
                if isinstance(order, Mapping):
                    orders.append(dict(order))

            nxt = page.get("next_cursor")
            final_next_cursor = str(nxt) if nxt is not None else None
            if not nxt or not str(nxt).strip():
                break
            next_cursor = str(nxt)
            if next_cursor in seen_cursors:
                pagination_complete = False
                warnings.append({
                    "type": "duplicate_cursor",
                    "message": f"duplicate cursor encountered: {next_cursor}",
                })
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            pagination_complete = False
            warnings.append({
                "type": "safety_cap",
                "message": (
                    "accountActiveOrders pagination safety cap reached "
                    f"({ORDER_PAGINATION_MAX_PAGES} pages)"
                ),
            })

        success = pagination_complete and not errors and not warnings
        diagnostic: dict[str, Any] = {
            "success": success,
            "source": "accountActiveOrders",
            "pages_fetched": pages_fetched,
            "final_next_cursor": final_next_cursor,
            "pagination_complete": pagination_complete,
            "pagination_cap_pages": ORDER_PAGINATION_MAX_PAGES,
        }
        if warnings:
            diagnostic["warnings"] = warnings
        if errors:
            diagnostic["errors"] = errors
        return {"orders": orders, "diagnostic": diagnostic}

    # -- pagination loop (Phase 2B) -----------------------------------

    def _fetch_all_active_orders_raw(
        self, *,
        client: Any,
        account_index: int,
        market_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Drive cursor pagination over ``/api/v1/accountActiveOrders``.

        Issue the first request with no cursor, then iterate while
        the response carries a non-empty ``next_cursor`` value. Each
        page's ``orders[]`` is appended to the running aggregate. The
        loop is hard-capped at ``ORDER_PAGINATION_MAX_PAGES`` to
        guard against server bugs.

        Returns::

            {
              "orders": [<order>, ...],     # aggregated raw orders
              "raw_order_count": <int>,      # len(orders)
              "pages": [<page>, ...],        # raw page payloads (for diagnostics)
              "page_count": <int>,           # pages consumed
              "final_raw": <dict or None>,   # the LAST raw page (for exchange_response)
              "final_next_cursor": <str or None>,
            }
        """
        aggregated: list[Mapping[str, Any]] = []
        pages: list[Mapping[str, Any]] = []
        cursor: Optional[str] = None
        page_count = 0
        final_next_cursor: Optional[str] = None

        while page_count < ORDER_PAGINATION_MAX_PAGES:
            page = client.account_active_orders(
                account_index, market_id=market_id, cursor=cursor,
            )
            page_count += 1
            pages.append(page)
            page_orders = list(page.get("orders") or [])
            aggregated.extend(page_orders)
            nxt = page.get("next_cursor")
            final_next_cursor = nxt
            # Terminate if no cursor or empty/whitespace cursor.
            if not nxt or not str(nxt).strip():
                break
            cursor = str(nxt)
        else:
            # Loop exited via the cap, not via terminal cursor. Surface
            # this so the operator can see if the cap is too low. We
            # do NOT raise — we return what we have. The freeze record
            # documents the cap as 50.
            pass

        return {
            "orders": aggregated,
            "raw_order_count": len(aggregated),
            "pages": pages,
            "page_count": page_count,
            "final_raw": pages[-1] if pages else None,
            "final_next_cursor": final_next_cursor,
        }

    # -- market-symbol map (Phase 2B) ----------------------------------

    def _effective_symbol_map(self, base_url: str) -> dict[int, str]:
        """Return the cached map for ``base_url`` (possibly empty)."""
        entry = self._market_symbol_cache._by_chain.get(base_url)
        if entry is None:
            return {}
        return entry[1]

    def _refresh_market_symbol_map(self, base_url: str) -> dict[str, Any]:
        """Issue a single public ``GET /api/v1/orderBookDetails`` and
        atomically replace the cached map for ``base_url``.

        Returns ``{"ok": True, "size": N}`` on success and
        ``{"ok": False, "error": <str>}`` on failure. Failure does NOT
        propagate — the caller decides whether to use the existing
        cache or fall back to ``symbol=None``.

        Honors the test seam: if ``_injected_http_client`` is set, we
        route through it instead of constructing a new
        ``LighterHttpClient``. This is the only path where the
        public metadata fetch uses a *different* HTTP transport from
        the authenticated orders fetch — and we keep them aligned
        via the same seam so tests can intercept both.
        """
        try:
            client = self._injected_http_client
            if client is None:
                client = LighterHttpClient(
                    base_url=base_url,
                    account_index=0,
                    api_key_index=0,
                    api_private_key="",
                    public_key="",
                )
            payload = client.order_book_details()
        except Exception as exc:
            return {"ok": False, "error": (
                f"{type(exc).__name__}: {exc}"
            )}

        if not isinstance(payload, Mapping):
            return {"ok": False, "error": (
                f"orderBookDetails returned non-mapping: "
                f"{type(payload).__name__}"
            )}

        mapping: dict[int, str] = {}
        for entry in list(payload.get("order_book_details") or []) + \
                     list(payload.get("spot_order_book_details") or []):
            if not isinstance(entry, Mapping):
                continue
            try:
                mid = int(entry.get("market_id") or 0)
                sym = str(entry.get("symbol") or "").strip()
            except (TypeError, ValueError):
                continue
            if mid > 0 and sym:
                mapping[mid] = sym

        self._market_symbol_cache.replace(base_url=base_url, mapping=mapping)
        return {"ok": True, "size": len(mapping)}

    # -- helpers -------------------------------------------------------

    def _market_id_from_request(
        self, request: Mapping[str, Any],
    ) -> Optional[int]:
        """Optional ``market_id`` filter for open-orders.

        Accepts either ``market_id`` (int) or ``market_index`` (int)
        in the request. None means "all markets" (default for Hermes
        wizards that do not specify a market).
        """
        for key in ("market_id", "market_index"):
            v = request.get(key)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None

    def _find_market_entry_for_symbol(
        self,
        obd_payload: Mapping[str, Any],
        symbol: str,
    ) -> Optional[Mapping[str, Any]]:
        """Look up the order-book entry for ``symbol``.

        Searches both perp (``order_book_details``) and spot
        (``spot_order_book_details``) buckets. The match is
        case-insensitive on the symbol. Returns the first matching
        entry, or None if not found.
        """
        if not isinstance(obd_payload, Mapping):
            return None
        target = str(symbol or "").strip().upper()
        if not target:
            return None
        for bucket in (
            obd_payload.get("order_book_details") or [],
            obd_payload.get("spot_order_book_details") or [],
        ):
            if not isinstance(bucket, list):
                continue
            for entry in bucket:
                if not isinstance(entry, Mapping):
                    continue
                entry_symbol = str(entry.get("symbol") or "").strip().upper()
                if entry_symbol == target:
                    return entry
        return None

    # -- set_tp / set_sl (Position Manager: TP/SL standalone orders) ---
    #
    # Both operations are routed here by ``execute()`` when the wizard
    # dispatches ``operation="set_tp"`` or ``operation="set_sl"`` with
    # the standard TradeDesk shape:
    #
    #     {
    #       "operation": "set_tp" | "set_sl",
    #       "exchange": "lighter",
    #       "account": "<account>",
    #       "symbol": "BTC",
    #       "side": "long" | "short",
    #       "price": <trigger_price>,
    #     }
    #
    # Per Lighter's semantics, a TP/SL order is a STOP_MARKET or
    # TAKE_PROFIT_MARKET order tied to a trigger_price. We use the
    # SDK's sign_create_tp_order / sign_create_sl_order helpers, which
    # internally produce a CreateOrder transaction with the correct
    # order_type (TAKE_PROFIT or STOP_LOSS) and trigger_price field.
    #
    # price == 0 is the documented removal sentinel per the wizard's
    # contract (e.g. the user typed "0" to clear an existing TP). For
    # removal we still POST a TP/SL order with trigger_price=0, which
    # is a no-op equivalent on the Lighter side; the canonical contract
    # is that the exchange is the authoritative remover.
    #
    # Same execution guarantees as Phase 3A:
    #   - per-(chain, account, api_key) nonce lock (held while POSTing)
    #   - one logical TradeDesk request → one Lighter POST
    #   - bounded verification (180s wall, 6 reads)
    #   - stop-on-failure (no automatic retry)
    #   - normalized execution envelope (success / error / ambiguous)

    def _prepare_tpsl_order(
        self, request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Prepare a TP/SL standalone order for Lighter.

        Pure preparation. Performs NO POST and acquires no lock.
        Returns the same shape as ``_prepare_place_order`` (the inner
        locked primitive can then run the SDK call).
        """
        # The wizard / TradeDesk normalize may place user-facing fields
        # either at the top level (direct call) or under
        # ``structured_request``. Read either, in that priority order.
        def _field(name: str) -> Any:
            for source in (
                request,
                request.get("structured_request") if isinstance(request.get("structured_request"), Mapping) else None,
            ):
                if source is not None and source.get(name) is not None:
                    return source.get(name)
            return None

        account = str(_field("account") or "").strip().lower()
        if not account:
            return {"success": False, "error": "missing account name"}

        symbol = str(_field("symbol") or "").strip().upper()
        if not symbol:
            return {"success": False, "error": "missing symbol", "account": account}

        side_raw = str(_field("side") or "").strip().lower()
        if side_raw not in {"long", "short", "buy", "sell"}:
            return {
                "success": False,
                "error": f"invalid side {side_raw!r}; must be 'long', 'short', 'buy', or 'sell'",
                "account": account, "symbol": symbol,
            }
        # TP/SL direction semantics:
        #
        # The wizard / TradeDesk input ``side`` is the user's CURRENT
        # POSITION side ("long" or "short"). For a reduce-only TP/SL,
        # the wire ``IsAsk`` must be the CLOSING direction for that
        # position — NOT the opening direction.
        #
        # Authoritative SDK evidence: lighter-python/examples/
        # create_position_tied_sl_tp.py — explicitly states that for
        # a SHORT position the SL/TP orders use IsAsk=0 (BUY); by
        # symmetry, for a LONG position the SL/TP orders use IsAsk=1
        # (SELL). The matching engine rejects reduce-only orders
        # whose IsAsk would increase the position, with code=21738
        # "invalid reduce only direction".
        #
        # Mapping:
        #   position LONG  -> TP/SL SELL  (IsAsk=1)
        #   position SHORT -> TP/SL BUY   (IsAsk=0)
        #
        # We keep the user-facing ``side`` field for diagnostics
        # (rendering + audit) carrying the original position side.
        if side_raw in {"long", "buy"}:
            # User is LONG. The reduce-only TP/SL must SELL to close.
            side = "long"
            is_ask = True
        else:
            # User is SHORT. The reduce-only TP/SL must BUY to close.
            side = "short"
            is_ask = False

        # Trigger price. 0 is the documented removal sentinel.
        price_raw = _field("price")
        if price_raw is None:
            return {
                "success": False,
                "error": "missing trigger price",
                "account": account, "symbol": symbol, "side": side,
            }
        try:
            trigger_price_decimal = Decimal(str(price_raw))
        except (InvalidOperation, ValueError) as exc:
            return {
                "success": False,
                "error": (
                    f"invalid trigger price {price_raw!r}: {exc}"
                ),
                "account": account, "symbol": symbol, "side": side,
            }
        if trigger_price_decimal < 0:
            return {
                "success": False,
                "error": (
                    f"trigger price must be >= 0 "
                    f"(got {trigger_price_decimal})"
                ),
                "account": account, "symbol": symbol, "side": side,
            }
        if not trigger_price_decimal.is_finite():
            return {
                "success": False,
                "error": (
                    f"trigger price not finite ({trigger_price_decimal})"
                ),
                "account": account, "symbol": symbol, "side": side,
            }
        # Decimal-string canonical form for the response envelope.
        trigger_price_str = format(trigger_price_decimal.normalize(), "f")

        # Resolve credentials, chain, and HTTP client.
        try:
            creds = _resolve_account_credentials(account)
        except ValueError as exc:
            return {
                "success": False, "error": str(exc),
                "account": account, "symbol": symbol, "side": side,
            }
        chain = creds["chain"]
        try:
            label, base_url = _get_chain_config(chain)
        except ValueError as exc:
            return {
                "success": False, "error": str(exc),
                "account": account, "chain": chain,
                "symbol": symbol, "side": side,
            }
        client = self._http_client_for_chain(chain, base_url, creds)

        # Resolve the market_id from the symbol by reading
        # ``order_book_details``. We refuse to guess.
        try:
            obd_payload = client.order_book_details()
        except LighterHTTPError as exc:
            return {
                "success": False, "error": str(exc),
                "account": account, "chain": chain,
                "symbol": symbol, "side": side,
                "exchange_response": exc.diagnostics,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": (
                    f"market resolution failed for {symbol}: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "account": account, "chain": chain,
                "symbol": symbol, "side": side,
            }
        market_entry = self._find_market_entry_for_symbol(
            obd_payload, symbol,
        )
        if market_entry is None:
            return {
                "success": False,
                "error": (
                    f"unknown symbol {symbol!r} on chain {chain}; "
                    f"market not found in orderBookDetails"
                ),
                "account": account, "chain": chain,
                "symbol": symbol, "side": side,
            }
        market_id_int = int(market_entry.get("market_id") or 0)
        if market_id_int <= 0:
            return {
                "success": False,
                "error": (
                    f"invalid market_id for {symbol} on {chain}"
                ),
                "account": account, "chain": chain,
                "symbol": symbol, "side": side,
            }
        price_decimals = int(market_entry.get("price_decimals") or 0)

        # Scale the trigger price to integer wire value.
        try:
            wire_trigger_price = _exact_scale_to_wire(
                trigger_price_str, decimals=price_decimals,
                field_name="price", allow_zero=True,
            )
        except ValueError as exc:
            return {
                "success": False, "error": str(exc),
                "account": account, "chain": chain,
                "symbol": symbol, "side": side,
            }

        return {
            "success": True,
            "account": account, "chain": chain,
            "client": client, "creds": creds,
            "market_id_int": market_id_int,
            "symbol": symbol, "side": side, "is_ask": is_ask,
            "trigger_price_str": trigger_price_str,
            "wire_trigger_price": wire_trigger_price,
            "price_decimals": price_decimals,
        }

    def _set_tp(self, request: Mapping[str, Any]) -> dict:
        """Take-profit standalone order path.

        Wraps the Phase 3A nonce-locked signing primitive by
        preparing the Lighter TP tx_info via the SDK's
        ``sign_create_tp_order`` helper, then POSTing it through the
        shared ``LighterHttpClient.send_tx_batch`` pipeline.
        """
        return self._send_tpsl_order_locked(request, kind="tp")

    def _set_sl(self, request: Mapping[str, Any]) -> dict:
        """Stop-loss standalone order path.

        Wraps the Phase 3A nonce-locked signing primitive by
        preparing the Lighter SL tx_info via the SDK's
        ``sign_create_sl_order`` helper, then POSTing it through the
        shared ``LighterHttpClient.send_tx_batch`` pipeline.
        """
        return self._send_tpsl_order_locked(request, kind="sl")

    # -- Transaction-based verification (Phase 38) ---------------------
    #
    # The investigation established that ``/api/v1/tx?by=hash`` is the
    # authoritative SDK-documented lookup endpoint. ``sendTx``
    # returning HTTP 200 + tx_hash is necessary but NOT sufficient —
    # the matching engine may reject or drop the transaction at edge
    # without producing a resting order. We poll the public
    # authoritative endpoint until the transaction appears or the
    # bounded wall-time is reached.
    #
    # Verification status values surfaced through the envelope (the
    # renderer branches on these, not on transport success alone):
    #
    #   transport_only            : sendTx OK but we have not confirmed
    #                               the tx landed yet (verification
    #                               loop exhausted or was not reached).
    #   confirmed_transaction     : tx appears in /api/v1/tx with
    #                               executed_at > 0 (documented as the
    #                               server-side execution timestamp).
    #   confirmed_resting         : tx confirmed AND the resting order
    #                               appears in accountActiveOrders
    #                               (the authoritative resting-side
    #                               proof; carries server-assigned
    #                               order_index).
    #   confirmed_rejected        : tx appears in /api/v1/tx with
    #                               executed_at == 0 AND block_height
    #                               == 0. Documented "rejected"
    #                               state.
    #   confirmed_sequenced       : tx appears in /api/v1/tx with
    #                               block_height > 0 BUT
    #                               executed_at == 0. UNDOCUMENTED
    #                               state — the Lighter docs do not
    #                               define whether this corresponds
    #                               to a resting order, a queued tx,
    #                               or a discarded tx. We surface a
    #                               neutral ⏳ message; the operator
    #                               decides next steps. NEVER
    #                               classified as "queued",
    #                               "accepted", or "confirmed".
    #   unconfirmed_at_exchange   : the bounded loop exhausted retries
    #                               and the tx never appeared in
    #                               /api/v1/tx; the envelope preserves
    #                               the diagnostic.
    #   verification_timeout      : the wall-time bound elapsed before
    #                               any conclusion could be drawn.
    #   verification_error        : a transport error occurred while
    #                               polling (e.g. CloudFront 403) OR
    #                               the observable fields were in an
    #                               impossible combination
    #                               (executed_at > 0 but
    #                               block_height == 0).
    #
    # Observable fields used (all are documented in
    # ``lighter/models/enriched_tx.py`` and confirmed by live probes):
    #
    #   - executed_at       (server-side execution timestamp;
    #                        0 means "not yet / never executed")
    #   - block_height      (non-zero means the tx was included in a
    #                        block)
    #   - committed_at      (post-execution verification timestamp)
    #   - verified_at       (additional verification timestamp)
    #   - hash              (matches our submitted tx_hash)
    #
    # We do NOT branch on the undocumented numeric ``status`` field.
    #
    # PHASE-1 (event_info) source: the Lighter OpenAPI spec lists
    # ``event_info`` as ``type: string, example: "{}"`` — the schema
    # is NOT documented. Empirically, on a successful tx the value
    # is ``'{"a":..,"k":..,"l":..,"x":..,"ae":""}'`` (with ``ae``
    # empty). On a rejected tx, ``ae`` is a JSON string of the form
    # ``'{"code":<int>,"message":"<str>"}'``. We treat the parseable
    # ``ae`` rejection as the highest-precedence signal because it
    # is the matching engine's direct outcome for that tx.

    @staticmethod
    def _parse_event_info_outcome(
        event_info: Any,
    ) -> dict:
        """Parse the exchange-outcome from ``event_info``.

        Lighter's ``event_info`` is an opaque JSON string whose
        schema is not documented in the OpenAPI spec. Empirically,
        successful txs carry ``event_info.ae = ""`` and rejected
        txs carry ``event_info.ae = '{"code":<int>,"message":"<str>"}'``.

        Returns a dict:

          {
            "parseable":  bool,
            "is_rejection": bool,
            "code":       int | None,
            "message":    str | None,
            "raw":        Any  (the parsed ae JSON, if parseable),
          }

        This helper is deliberately defensive: if the JSON cannot
        be parsed, or if the ``ae`` field is absent / null / empty,
        we report ``is_rejection=False`` so the caller falls through
        to the documented state machine. We do NOT infer success
        from the absence of a parseable rejection.
        """
        outcome = {
            "parseable": False,
            "is_rejection": False,
            "code": None,
            "message": None,
            "raw": None,
        }
        if not event_info:
            return outcome
        if not isinstance(event_info, str):
            # Some upstream callers may pass non-string types; we
            # do not infer — just mark unparseable.
            return outcome
        try:
            ei_obj = json.loads(event_info)
        except Exception:
            return outcome
        if not isinstance(ei_obj, Mapping):
            return outcome
        ae = ei_obj.get("ae")
        if ae is None:
            return outcome
        if not isinstance(ae, str):
            outcome["parseable"] = True
            outcome["raw"] = ae
            return outcome
        outcome["parseable"] = True  # event_info itself parsed
        if ae.strip() == "":
            # Empty ae ⇒ no applied-event error ⇒ exchange did not
            # emit a per-tx rejection. parseable=True because the
            # outer event_info was a valid JSON document.
            return outcome
        try:
            ae_obj = json.loads(ae)
        except Exception:
            outcome["raw"] = ae
            return outcome
        if not isinstance(ae_obj, Mapping):
            outcome["raw"] = ae
            return outcome
        code = ae_obj.get("code")
        message = ae_obj.get("message")
        outcome["raw"] = ae_obj
        # A rejection is defined as a non-zero error code AND a
        # non-empty message. A code of 0 with an empty message is
        # treated as no-error.
        if isinstance(code, int) and code != 0:
            outcome["is_rejection"] = True
            outcome["code"] = code
            outcome["message"] = (
                f"Lighter matching-engine rejection: code={code}, "
                f"message={message!r}"
            )
        elif isinstance(code, str) and code and code != "0":
            outcome["is_rejection"] = True
            outcome["code"] = code
            outcome["message"] = (
                f"Lighter matching-engine rejection: code={code!r}, "
                f"message={message!r}"
            )
        return outcome

    def _verify_tpsl_transaction_landed(
        self,
        *,
        client: Any,
        tx_hash: str,
        client_order_index: str,
        account_index: int,
        api_key_index: int,
    ) -> dict:
        """Bounded poll of ``GET /api/v1/tx?by=hash``.

        Returns a verification diagnostic dict (never raises) with
        fields the envelope-builder can interpret::

            {
              "verification_status": str,
              "verification_attempts": int,
              "verification_wall_time_s": float,
              "final_sleep_ms": int,
              "tx_lookup_diagnostic":  {  # populated on first hit
                  "hash": str | None,
                  "executed_at": int,
                  "block_height": int,
                  "committed_at": int,
                  "verified_at": int,
                  "raw": dict,            # the raw EnrichedTx body
              } or None,
              "transport_only": bool,      # True iff no conclusion reached
              "error": str or None,
            }
        """
        import time as _time

        # The verification bounds for TP/SL are TIGHTER than the
        # generic LIMIT-order bounds (180s/6 reads). PredictedExpireTime
        # (lighter-go DefaultExpireTime) is 9 minutes, so 60s/8 reads
        # comfortably confirms both fast and slow paths without
        # waiting past expiry.
        #
        # We honor the module-level VERIFICATION_* constants so
        # tests can tighten them. The pre-existing verifier
        # (``_run_bounded_post_read``) uses the same constants.
        min_sleep_ms = max(int(VERIFICATION_MIN_SLEEP_MS), 1)
        max_sleep_ms = max(int(VERIFICATION_MAX_SLEEP_MS), min_sleep_ms)
        max_wall_time_s = float(VERIFICATION_MAX_WALL_TIME_S) or 60.0
        max_reads = int(VERIFICATION_MAX_READS) or 8

        deadline = _time.monotonic() + max_wall_time_s
        sleep_ms = min_sleep_ms
        reads_performed = 0
        first_hit: Optional[dict] = None
        error_message: Optional[str] = None
        wall_time_start = _time.monotonic()

        for attempt in range(max_reads):
            if _time.monotonic() >= deadline:
                break
            try:
                payload = client.account_tx_by_hash(tx_hash)
            except LighterHTTPError as exc:
                error_message = (
                    f"Lighter /api/v1/tx lookup failed: "
                    f"{type(exc).__name__}: status={getattr(exc, 'status', None)}"
                )
                break
            except Exception as exc:
                error_message = (
                    f"Lighter /api/v1/tx lookup error: "
                    f"{type(exc).__name__}: {exc}"
                )
                break
            reads_performed = attempt + 1

            # ``account_tx_by_hash`` returns the deserialized JSON
            # body. code=200 + hash==our_hash means the tx exists.
            # code=21500 means "transaction not found".
            if isinstance(payload, Mapping):
                code = payload.get("code")
                hash_match = payload.get("hash") == tx_hash
                if code == 200 and hash_match:
                    first_hit = {
                        "hash": payload.get("hash"),
                        "executed_at": int(
                            payload.get("executed_at") or 0
                        ),
                        "block_height": int(
                            payload.get("block_height") or 0
                        ),
                        "committed_at": int(
                            payload.get("committed_at") or 0
                        ),
                        "verified_at": int(
                            payload.get("verified_at") or 0
                        ),
                        "event_info": payload.get("event_info") or "",
                        "raw": dict(payload),
                    }
                    break
                if code == 200 and not hash_match:
                    # Server returned a DIFFERENT hash with code=200.
                    # That should not happen with /api/v1/tx; treat as
                    # network error and continue.
                    pass
                elif code not in (None, 200):
                    # Error response (e.g., 21500 transaction not found).
                    # Continue polling (the matching engine may queue
                    # and apply shortly). Bounded by max_reads.
                    pass

            remaining = max(int(deadline - _time.monotonic()), 0)
            if remaining <= 0:
                break
            sleep_ms = min(sleep_ms * 2, max_sleep_ms, remaining * 1000)
            if sleep_ms <= 0:
                break
            _time.sleep(sleep_ms / 1000.0)

        wall_time_s = _time.monotonic() - wall_time_start

        # Decide verification_status based on what we observed.
        if error_message is not None:
            return {
                "verification_status": "verification_error",
                "verification_attempts": reads_performed,
                "verification_wall_time_s": wall_time_s,
                "final_sleep_ms": sleep_ms,
                "tx_lookup_diagnostic": None,
                "transport_only": True,
                "error": error_message,
            }

        if first_hit is None:
            # Polled, never found the tx.
            if reads_performed >= max_reads:
                return {
                    "verification_status": "unconfirmed_at_exchange",
                    "verification_attempts": reads_performed,
                    "verification_wall_time_s": wall_time_s,
                    "final_sleep_ms": sleep_ms,
                    "tx_lookup_diagnostic": None,
                    "transport_only": True,
                    "error": (
                        f"Lighter /api/v1/tx did not find tx_hash "
                        f"after {reads_performed} reads in {wall_time_s:.2f}s"
                    ),
                }
            return {
                "verification_status": "verification_timeout",
                "verification_attempts": reads_performed,
                "verification_wall_time_s": wall_time_s,
                "final_sleep_ms": sleep_ms,
                "tx_lookup_diagnostic": None,
                "transport_only": True,
                "error": (
                    f"Lighter /api/v1/tx lookup wall-time exceeded after "
                    f"{reads_performed} reads in {wall_time_s:.2f}s"
                ),
            }

        # Decide the normalized verification_status using the
        # DOCUMENTED observable fields. The exchange does not publish
        # a documented lifecycle for every combination, so we limit
        # our mapping to the documented branches:
        #
        # PHASE 1 — Exchange-outcome parsing (HIGHEST PRECEDENCE).
        #
        # Lighter's ``event_info`` field is an opaque JSON string
        # whose schema is NOT documented in the OpenAPI spec
        # (lighter-python/openapi.json describes it as ``type: string,
        # example: "{}"``). Empirically across tx types 8, 13, 20, 31
        # (sampled from sequence_indexes 1..126 on the public
        # /api/v1/tx endpoint), the per-tx ``applied-event error``
        # payload is exposed under ``event_info.ae``:
        #
        #   - Successful txs: event_info.ae = ""  (empty string)
        #   - Rejected txs : event_info.ae = '{"code":<int>,"message":"<str>"}'
        #
        # The exchange-direct outcome lives in this field and is
        # authoritative — it is the per-tx response emitted by the
        # matching engine. We treat it as the highest-precedence
        # signal: if event_info carries a parseable rejection, we
        # classify confirmed_rejected EVEN IF executed_at > 0 and
        # block_height > 0 (live observation: 2026-07-20 15:23 UTC,
        # robin BTC long TP — server set executed_at=1784561032536,
        # block_height=4478707, AND embedded
        # event_info.ae='{"code":21738,"message":"invalid reduce
        # only direction"}').
        #
        # If event_info is absent, unparseable, or does not contain
        # a parseable rejection JSON, we FALL THROUGH to the
        # documented state machine — we do NOT infer success.
        outcome = self._parse_event_info_outcome(
            first_hit.get("event_info", "")
        )
        if outcome.get("is_rejection"):
            return {
                "verification_status": "confirmed_rejected",
                "verification_attempts": reads_performed,
                "verification_wall_time_s": wall_time_s,
                "final_sleep_ms": sleep_ms,
                "tx_lookup_diagnostic": first_hit,
                "transport_only": False,
                "error": outcome["message"],
                "exchange_outcome": outcome,
            }

        # PHASE 2 — Documented observable-field state machine.
        #
        #   executed_at > 0 && block_height > 0
        #       → confirmed_transaction
        #           The tx was both included in a block (block_height)
        #           and applied by the matching engine (executed_at).
        #           This is the documented "executed" state.
        #
        #   executed_at == 0 && block_height == 0
        #       → confirmed_rejected
        #           The tx was registered with /api/v1/tx but the
        #           matching engine did not apply it (no block, no
        #           execution timestamp). This is the documented
        #           "rejected" state.
        #
        #   block_height > 0 && executed_at == 0
        #       → confirmed_sequenced  (NEUTRAL, no inferred
        #         semantics)
        #           The tx is in a block but the matching engine
        #           has not applied it. The exchange does NOT
        #           document this state. We deliberately do NOT
        #           classify this as "queued", "accepted",
        #           "pending", or "confirmed", because the
        #           exchange documentation does not define whether
        #           this state corresponds to a resting order, a
        #           queued-but-not-yet-applied order, or an
        #           eventually-discarded order. The Lighter
        #           Order.status enum (visible via
        #           accountActiveOrders / accountInactiveOrders) is
        #           the authoritative source for resting order
        #           state, and we report that downstream via
        #           confirmed_resting if / when the order appears
        #           in the resting list.
        #
        #   any impossible combination (executed_at > 0 &&
        #   block_height == 0)
        #       → verification_error
        executed_at = first_hit["executed_at"]
        block_height = first_hit["block_height"]
        if executed_at > 0 and block_height > 0:
            verification_status = "confirmed_transaction"
        elif executed_at == 0 and block_height == 0:
            verification_status = "confirmed_rejected"
        elif block_height > 0 and executed_at == 0:
            # Documented-UNDOCUMENTED intermediate state.
            verification_status = "confirmed_sequenced"
        else:
            # executed_at > 0 && block_height == 0
            # Cannot execute without being in a block; treat as
            # an undocumented anomaly and surface it to the
            # operator rather than silently classify as success.
            verification_status = "verification_error"
        return {
            "verification_status": verification_status,
            "verification_attempts": reads_performed,
            "verification_wall_time_s": wall_time_s,
            "final_sleep_ms": sleep_ms,
            "tx_lookup_diagnostic": first_hit,
            "transport_only": False,
            "error": None,
        }

    def _apply_tpsl_verification(
        self,
        envelope: dict,
        verification: dict,
        *,
        client_order_index: str,
        client: Any,
        market_id: int,
        account_index: int,
    ) -> dict:
        """Augment an envelope with the verification result.

        The renderer branches on ``envelope["verification_status"]``,
        NOT on ``envelope["success"]`` alone. For the wizard (Telegram)
        path, the operator should see:

          - confirmed_resting            : "TP live on Lighter"
          - confirmed_transaction        : "TP confirmed at exchange"
          - confirmed_rejected           : "TP rejected at exchange"
          - unconfirmed_at_exchange      : "TP submitted but Lighter
                                          did not register it"
          - verification_timeout /
            verification_error           : "TP could not be confirmed"

        Branch rules (envelope is mutated and returned):

          - confirmed_transaction → success=True, status="confirmed".
            secondary: check accountActiveOrders for the resting
            order and, if found, upgrade to confirmed_resting with
            server-assigned order_index.

          - confirmed_rejected → success=False. Preserve the raw
            server diagnostic in the envelope for the operator.

          - confirmed_sequenced → success=False. The exchange
            documented a tx in a block but no execution
            timestamp. We do NOT infer resting, queued, or
            discarded semantics. The envelope preserves the
            observable fields so the operator can consult the
            accountActiveOrders / accountInactiveOrders endpoint
            for a definitive resting-order read.

          - unconfirmed_at_exchange / verification_timeout /
            verification_error → success=False. Preserve the bounded-
            verification diagnostic for the operator. The envelope
            still carries the original transport-200 sendTx record
            for audit.

        NO exchange-specific status-enum interpretation: we use
        ONLY the documented observable fields (executed_at,
        block_height, committed_at, verified_at) and the literal
        "transaction not found" 400 from the public /api/v1/tx
        endpoint.

        We PRESERVE the pre-verification ``status`` field (e.g.
        ``submitted``) for backward compatibility with existing
        tests and the TradeDesk wire. The verification verdict is
        surfaced via ``envelope["verification_status"]`` only; the
        renderer is responsible for branching on that field.
        """
        # The envelope is whatever was returned by _execution_result /
        # the success branch. _execution_result stores
        # ``exchange_response`` at the TOP LEVEL (not nested under a
        # "data" key), so we augment the top-level dict directly.
        status = verification.get("verification_status")
        diagnostic = verification.get("tx_lookup_diagnostic")
        attempts = verification.get("verification_attempts", 0)
        wall_time_s = verification.get("verification_wall_time_s", 0.0)
        error_msg = verification.get("error")
        exchange_outcome = verification.get("exchange_outcome")

        existing_exch = envelope.get("exchange_response") or {}
        if not isinstance(existing_exch, Mapping):
            existing_exch = {}
        existing_exch = {
            **existing_exch,
            "verification_status": status,
            "verification_attempts": attempts,
            "verification_wall_time_s": wall_time_s,
        }
        if diagnostic is not None:
            existing_exch["tx_lookup_diagnostic"] = diagnostic
        if error_msg is not None:
            existing_exch["verification_error"] = error_msg
        if exchange_outcome is not None:
            existing_exch["exchange_outcome"] = exchange_outcome
        envelope["exchange_response"] = existing_exch
        envelope["verification_status"] = status

        if status == "confirmed_transaction":
            # Continue to attempt the resting confirmation. The
            # verification_status is upgraded to "confirmed_resting"
            # ONLY if the resting order is found in accountActiveOrders.
            resting = self._verify_tpsl_resting_order(
                client=client,
                account_index=account_index,
                client_order_index=client_order_index,
                market_id=market_id,
            )
            existing_exch["resting_diagnostic"] = resting
            if resting.get("found"):
                envelope["order_index"] = str(
                    resting.get("order_index") or ""
                )
                envelope["verification_status"] = "confirmed_resting"
                existing_exch["verification_status"] = "confirmed_resting"
            else:
                # No resting order found; keep confirmed_transaction
                # status. This is the expected state if the order was
                # filled/cancelled quickly after submission. We DO
                # NOT overwrite envelope["status"] — the pre-verification
                # transport status is preserved.
                envelope["verification_status"] = "confirmed_transaction"
                existing_exch["verification_status"] = "confirmed_transaction"
            return envelope
        if status == "confirmed_sequenced":
            # The tx is documented as sequenced (block_height > 0) but
            # its execution state is NOT documented by the exchange.
            # We do NOT silently classify it as resting, queued, or
            # discarded. We CAN, however, opportunistically probe the
            # resting list: if the order IS resting, we promote to
            # confirmed_resting (the resting read is the authoritative
            # source). If the order is NOT resting, we KEEP the
            # confirmed_sequenced status — we do NOT downgrade to
            # confirmed_transaction and we do NOT upgrade to
            # confirmed_resting on a negative read.
            resting = self._verify_tpsl_resting_order(
                client=client,
                account_index=account_index,
                client_order_index=client_order_index,
                market_id=market_id,
            )
            existing_exch["resting_diagnostic"] = resting
            if resting.get("found"):
                envelope["order_index"] = str(
                    resting.get("order_index") or ""
                )
                envelope["verification_status"] = "confirmed_resting"
                existing_exch["verification_status"] = "confirmed_resting"
            else:
                # The exchange documentation does not define
                # whether confirmed_sequenced corresponds to a
                # resting order. We keep the neutral status but
                # DO flip success to False because the operator
                # cannot rely on this state being a live TP.
                # We DO NOT overwrite envelope["status"]; the
                # pre-verification transport status is preserved
                # for audit (rendering uses verification_status).
                envelope["success"] = False
            return envelope

        if status == "confirmed_rejected":
            # Rejected by exchange: flip success to False and surface
            # the diagnostic. We DO NOT overwrite envelope["status"];
            # the pre-verification transport status is preserved for
            # audit (rendering uses verification_status).
            #
            # If the verifier parsed an exchange-direct rejection
            # from event_info.ae, use that as the operator-facing
            # error message; otherwise fall back to the documented
            # "executed_at==0 && block_height==0" message.
            envelope["success"] = False
            envelope["verification_status"] = "confirmed_rejected"
            if exchange_outcome and exchange_outcome.get("is_rejection"):
                envelope["error"] = exchange_outcome.get("message") or (
                    "Lighter matching-engine rejection (no message)"
                )
            else:
                envelope["error"] = (
                    "Lighter registered the transaction but the matching "
                    "engine did not execute it (executed_at==0, "
                    "block_height==0). "
                    "See exchange_response.tx_lookup_diagnostic for the "
                    "server's raw transaction record."
                )
            return envelope

        # unconfirmed_at_exchange | verification_timeout | verification_error
        envelope["success"] = False
        envelope["verification_status"] = status
        envelope["error"] = (
            error_msg
            or "Lighter transaction could not be confirmed via /api/v1/tx."
        )
        return envelope

    def _verify_tpsl_resting_order(
        self,
        *,
        client: Any,
        account_index: int,
        client_order_index: str,
        market_id: int,
    ) -> dict:
        """Look for the resting order in accountActiveOrders.

        Single-shot GET, no polling (the tx-level verification is the
        authoritative proof; this is just for upgrading the
        envelope from confirmed_transaction to confirmed_resting and
        capturing the server-assigned order_index).
        """
        try:
            payload = client.account_active_orders(
                account_index, market_id=market_id
            )
        except LighterHTTPError as exc:
            return {
                "found": False,
                "error": (
                    f"Lighter accountActiveOrders failed: "
                    f"status={getattr(exc, 'status', None)}"
                ),
            }
        except Exception as exc:
            return {
                "found": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        orders = (payload or {}).get("orders") or []
        for o in orders:
            if not isinstance(o, Mapping):
                continue
            if str(o.get("client_order_index")) == str(client_order_index):
                return {
                    "found": True,
                    "order_index": o.get("order_index"),
                    "type": o.get("type"),
                    "trigger_price": o.get("trigger_price"),
                    "price": o.get("price"),
                    "status": o.get("status"),
                }
        return {"found": False, "inspected_count": len(orders)}

    def _send_tpsl_order_locked(
        self, request: Mapping[str, Any], *, kind: str,
    ) -> dict:
        """Inner locked primitive for set_tp / set_sl.

        Holds the per-(chain, account, api_key) nonce lock for the
        duration of the signed POST. Mirrors the structure of
        ``_place_order_locked`` (no retry, no GET inside the lock).
        """
        if kind not in {"tp", "sl"}:
            return _execution_result(
                request, success=False,
                error=f"internal: invalid tpsl kind {kind!r}",
            )

        prepared = self._prepare_tpsl_order(request)
        if not prepared.get("success"):
            # Re-shape a pre-flight error into the canonical envelope.
            return _execution_result(
                request,
                success=False,
                error=str(prepared.get("error") or "tpsl preparation failed"),
                account=prepared.get("account"),
                chain=prepared.get("chain"),
                symbol=prepared.get("symbol"),
                side=prepared.get("side"),
            )

        chain = prepared["chain"]
        client = prepared["client"]
        creds = prepared["creds"]
        market_id_int = prepared["market_id_int"]
        symbol = prepared["symbol"]
        side = prepared["side"]
        is_ask = prepared["is_ask"]
        trigger_price_str = prepared["trigger_price_str"]
        wire_trigger_price = prepared["wire_trigger_price"]
        account = prepared["account"]
        api_key_index = int(creds["apikey_index"])
        account_index = int(creds["account_index"])
        api_private_key = str(creds["private_key"])

        # Lighter TP/SL orders carry BOTH a trigger_price (the
        # activation price) AND a limit price (where they execute
        # once triggered). Per the wizard contract we receive ONE
        # "price" field; for a market TP/SL the limit_price is set
        # equal to the trigger_price so the order converts to a
        # market-on-trigger. This matches the canonical Telegram UX
        # for TP/SL "set a price, execute at that price on trigger".
        wire_limit_price = wire_trigger_price

        # The SDK sign helpers expect an unused base_amount argument
        # (the underlying transaction struct still requires a field,
        # but for a TP/SL the size is the OPEN position size which
        # we don't have here). Per the wizard contract a TP/SL
        # request carries the symbol + side; the underlying Lighter
        # TP/SL order placement requires a NON-ZERO base_amount on
        # the wire. We use a sentinel of 1 (the smallest legal wire
        # value). The user / wizard never sees this number; the
        # actual position size is implied by reduce_only=True +
        # matching trigger_price/symbol/side on the matching engine.
        wire_base_amount = 1

        # Generate a stable client_order_index.
        client_order_index = _generate_client_order_index()

        # Sign + POST under the per-key nonce lock.
        try:
            with self._get_nonce_lock(
                chain=chain, account_index=account_index,
                api_key_index=api_key_index,
            ):
                try:
                    if kind == "tp":
                        sanitized_resp_send_tx, raw_resp_send_tx = (
                            client.create_tp_order(
                                market_index=market_id_int,
                                client_order_index=client_order_index,
                                base_amount=wire_base_amount,
                                trigger_price=wire_trigger_price,
                                price=wire_limit_price,
                                is_ask=is_ask,
                                reduce_only=True,
                                api_private_key=api_private_key,
                            )
                        )
                    else:  # kind == "sl"
                        sanitized_resp_send_tx, raw_resp_send_tx = (
                            client.create_sl_order(
                                market_index=market_id_int,
                                client_order_index=client_order_index,
                                base_amount=wire_base_amount,
                                trigger_price=wire_trigger_price,
                                price=wire_limit_price,
                                is_ask=is_ask,
                                reduce_only=True,
                                api_private_key=api_private_key,
                            )
                        )
                except LighterHTTPError as exc:
                    err_response: dict = {"diagnostics": exc.diagnostics}
                    err_status = getattr(exc, "status", None)
                    if err_status is not None:
                        err_response["status"] = int(err_status)
                    err_body = getattr(exc, "body", None)
                    if err_body:
                        err_response["body"] = err_body
                    return _execution_result(
                        request, success=False, error=str(exc),
                        account=account, chain=chain,
                        market_id=market_id_int, symbol=symbol,
                        client_order_index=str(client_order_index),
                        exchange_response=err_response,
                    )
                except Exception as exc:
                    return _execution_result(
                        request, success=False,
                        error=(
                            f"Lighter {kind}_order failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        account=account, chain=chain,
                        market_id=market_id_int, symbol=symbol,
                        client_order_index=str(client_order_index),
                    )

                # Classify the response exactly like Phase 3A.
                code = int(sanitized_resp_send_tx.get("code", 0))
                message = sanitized_resp_send_tx.get("message")
                tx_hash = sanitized_resp_send_tx.get("tx_hash")
                msg_kind = _classify_lighter_sendtx_message(message)
                has_tx_hash = bool(tx_hash and str(tx_hash).strip())
                is_accepted_for_processing = (
                    code == 200
                    and has_tx_hash
                    and msg_kind in ("empty", "advisory")
                )

                if not is_accepted_for_processing:
                    if code != 200:
                        failure_label = "rejected"
                    elif not has_tx_hash:
                        failure_label = "ambiguous (no tx_hash)"
                    else:
                        failure_label = (
                            f"ambiguous (unrecognized message: {msg_kind!r})"
                        )
                    return _execution_result(
                        request, success=False,
                        error=(
                            f"Lighter sendTx returned code={code}, "
                            f"message={message!r}, tx_hash={tx_hash!r}; "
                            f"{failure_label}"
                        ),
                        account=account, chain=chain,
                        market_id=market_id_int, symbol=symbol,
                        client_order_index=str(client_order_index),
                        exchange_response={
                            "send_tx": sanitized_resp_send_tx
                        },
                        ambiguous=True,
                    )

                submission_advisory = (
                    message if msg_kind == "advisory" else None
                )

                # Build the success envelope INSIDE the lock but defer the
                # ``return`` so we can run bounded verification AFTER the
                # nonce lock releases. The bounded verification is
                # GET-only (no nonce lock required) and would otherwise
                # be holding the lock unnecessarily for the entire
                # VERIFICATION_MAX_WALL_TIME_S window.
                success_envelope = _execution_result(
                    request,
                    success=True,
                    account=account, chain=chain,
                    market_id=str(market_id_int), symbol=symbol,
                    side=side,
                    operation=f"set_{kind}",
                    price=trigger_price_str,
                    client_order_index=str(client_order_index),
                    submission_status="accepted_for_processing",
                    status="submitted",
                    tx_hash=tx_hash,
                    order_id=None,
                    order_index=None,
                    predicted_execution_time_ms=(
                        sanitized_resp_send_tx.get(
                            "predicted_execution_time_ms"
                        )
                    ),
                    volume_quota_remaining=(
                        sanitized_resp_send_tx.get("volume_quota_remaining")
                    ),
                    submission_advisory=submission_advisory,
                    exchange_response={
                        "send_tx": sanitized_resp_send_tx,
                        "submitted_transaction": {
                            "tx_type": (
                                14  # CreateOrder (Lighter TP/SL use the
                                   # same on-chain tx as limit orders)
                            ),
                            "account_index": account_index,
                            "api_key_index": api_key_index,
                            "market_index": market_id_int,
                            "client_order_index": str(client_order_index),
                            "trigger_price": wire_trigger_price,
                            "limit_price": wire_limit_price,
                            "is_ask": is_ask,
                            "reduce_only": True,
                        },
                        "raw_signed_response": raw_resp_send_tx,
                    },
                )
        except Exception as exc:
            return _execution_result(
                request, success=False,
                error=(
                    f"Lighter set_{kind} nonce-lock path failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                account=account, chain=chain,
                market_id=market_id_int, symbol=symbol,
            )

        # Lock has been released. Run bounded transaction-based
        # verification BEFORE returning. The ``/api/v1/tx`` endpoint
        # is the SDK-proven authoritative confirmation that the
        # matching engine registered our transaction (not just
        # acknowledged the transport POST).
        if isinstance(success_envelope, dict) and success_envelope.get("success"):
            verification = self._verify_tpsl_transaction_landed(
                client=client,
                tx_hash=tx_hash,
                client_order_index=str(client_order_index),
                account_index=account_index,
                api_key_index=api_key_index,
            )
            success_envelope = self._apply_tpsl_verification(
                success_envelope, verification,
                client_order_index=str(client_order_index),
                client=client,
                market_id=market_id_int,
                account_index=account_index,
            )
        return success_envelope

    # -- place_order (Phase 3A: read-only authentication, single write) -

    def _get_nonce_lock(
        self, *, chain: str, account_index: int, api_key_index: int,
    ) -> threading.Lock:
        """Return the per-(chain, account, api_key) synchronous lock,
        lazily creating it on first use.

        A guard around the dict itself prevents a TOCTOU race when two
        threads first observe a missing key.
        """
        key = (str(chain), int(account_index), int(api_key_index))
        lock = self._nonce_locks.get(key)
        if lock is None:
            with self._nonce_locks_guard:
                lock = self._nonce_locks.get(key)
                if lock is None:
                    lock = threading.Lock()
                    self._nonce_locks[key] = lock
        return lock

    def _place_order(self, request: Mapping[str, Any]) -> dict:
        """Authenticated ``place_order`` dispatcher (Phase 3A).

        Phase 3A authorizes LIMIT + GTT only. ``reduce_only=false``,
        ``post_only=false``, no triggers. Caller supplies exact decimal
        price and quantity; the implementation scales them to integer
        wire values via ``_exact_scale_to_wire``.

        Thin orchestrator. The actual work is split into two reusable
        internal helpers:

          - ``_prepare_place_order`` does all validation, market
            resolution, and price/size wire conversion (no POST,
            no lock acquisition).
          - ``_place_order_locked`` does the signed POST + response
            classification and assumes the nonce lock is already held.

        The ladder path (``_batch_orders``) reuses the same two helpers
        under a single nonce lock for the whole group.

        Returns ``_execution_result(success=True, ...,
        submission_status="accepted_for_processing", order_id=None,
        order_index=None, status="submitted", ...)``.

        Verification of the placed order is performed by the bounded
        GET-only post-read loop (``_run_bounded_post_read``), which is
        invoked by the operator's verification script after the
        submit. We do NOT trigger the post-read here because the
        post-read involves multiple GETs and is separate from the
        single submit.
        """
        # Per-trade-call exactly-one-POST invariant is enforced at the
        # HTTP client layer (LighterHttpClient.place_order increments
        # a post-count and refuses a second POST per call). The
        # dispatcher below may be invoked any number of times across
        # the agent's lifetime; each invocation issues exactly one POST.

        prepared = self._prepare_place_order(request)
        if not prepared.get("success"):
            # _prepare_place_order returned a pre-shaped error dict.
            # Re-shape it into the canonical _execution_result envelope.
            return _execution_result(
                request,
                success=False,
                error=prepared.get("error"),
                account=prepared.get("account"),
                chain=prepared.get("chain"),
                market_id=prepared.get("market_id"),
                symbol=prepared.get("symbol"),
                exchange_response=prepared.get("exchange_response"),
            )

        # ----- The locked sequence: nextNonce -> sign -> POST -----
        # The lock is acquired once and held until the POST completes
        # (success or raise). The with-statement releases on exit.
        with self._get_nonce_lock(
            chain=prepared["chain"],
            account_index=prepared["account_index"],
            api_key_index=prepared["api_key_index"],
        ):
            return self._place_order_locked(prepared, request)


    # -- Phase 3A refactor: shared helpers for single + batch placement --
    #
    # The Phase 3A single-order path (``_place_order``) was a single
    # monolithic method that:
    #   1) parsed the request,
    #   2) resolved the symbol → market_id,
    #   3) fetched and validated market metadata,
    #   4) converted price/size to integer wire values,
    #   5) validated min_base_amount / min_quote_amount,
    #   6) generated a client_order_index,
    #   7) acquired the per-(chain, account, api_key) nonce lock,
    #   8) signed + POSTed the create_order via LighterHttpClient.place_order,
    #   9) classified the response into a canonical envelope.
    #
    # The Phase 6 ladder path needs to call steps 8-9 per child under
    # a SINGLE nonce lock for the whole ladder. To avoid duplicating the
    # signing + POST + classification logic, we factor the existing
    # single-order implementation into two reusable internal helpers:
    #
    #   _prepare_place_order(request)
    #     → parses, validates, and converts ONE request into a
    #       prepared-fields dict that includes wire_price / wire_size
    #       / client_order_index / market_index / side / chain / creds
    #       / client. Either returns the prepared-fields dict on
    #       success, or an _execution_result-shaped error dict on
    #       failure (caller can detect by ``"success" in result``).
    #
    #   _place_order_locked(prepared, request)
    #     → ASSUMES the per-(chain, account, api_key) nonce lock is
    #       ALREADY HELD. Performs the signed POST + response
    #       classification and returns the canonical envelope.
    #
    # The public ``_place_order`` keeps its exact Phase 3A behavior
    # (one POST per call, acquire-and-release the lock, the same
    # canonical envelope shape) but is now a thin orchestrator over
    # the two helpers. The ladder path calls
    # ``_prepare_place_order(child_request)`` once per child and
    # ``_place_order_locked(prepared, request)`` once per child
    # inside a single ``with self._get_nonce_lock(...):`` block.

    def _prepare_place_order(
        self, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate, resolve market, and convert ONE child order to
        integer wire values.

        Pure preparation. Performs NO POST and acquires no lock.
        Returns one of two shapes:

          - On success: a prepared-fields dict with the keys
            ``account``, ``chain``, ``base_url``, ``client``,
            ``api_key_index``, ``account_index``, ``api_private_key``,
            ``market_id_int``, ``symbol``, ``side``, ``order_type``,
            ``time_in_force``, ``is_ask``, ``client_order_index``,
            ``wire_price``, ``wire_size``, ``price_str``,
            ``quantity_str``, ``min_base_amount``, ``min_quote_amount``,
            ``market_entry``.

          - On failure: an ``_execution_result``-shaped dict with
            ``success=False`` and a sanitized ``error`` string.
            Callers MUST check ``"success" in result`` to discriminate.

        The same validation rules as Phase 3A apply:
          order_type="limit", time_in_force="good-till-time",
          reduce_only=False, trigger_price=0, market status="active",
          price/size must be exactly representable per
          price_decimals/size_decimals, notional >= min_quote_amount,
          size >= min_base_amount.
        """
        # Canonical contract: TradeDesk.normalize() emits a dict whose
        # user-facing request lives under "structured_request" and the
        # canonical single-order child lives under "child_order" /
        # "child_orders[0]". Helper: read a field from any of these
        # locations, top to bottom, to support BOTH:
        #   (a) the canonical wizard-path invocation (TradeDesk.execute()),
        #   (b) the direct-call invocation (legacy tests) with fields
        #       at the top level.
        # TradeDesk and the wizard are FROZEN; we adapt here only.

        def _field(name: str) -> Any:
            for source in (request,
                           request.get("structured_request") if isinstance(request.get("structured_request"), Mapping) else None,
                           request.get("child_order") if isinstance(request.get("child_order"), Mapping) else None,
                           (request.get("child_orders") or [None])[0] if isinstance(request.get("child_orders"), list) else None):
                if source is not None and source.get(name) is not None:
                    return source.get(name)
            return None

        account = str(_field("account") or "").strip().lower()
        if not account:
            return {
                "success": False,
                "error": "missing account name",
            }

        # Phase 3A: only "order" is supported (canonical op);
        # side must be buy/sell; order_type must be "limit" or
        # "market"; time_in_force must be "good-till-time" for limit
        # orders and "immediate-or-cancel" (or unset, which maps to
        # IOC) for market orders; reduce_only is now supported (used
        # by the Position Manager Close action to atomically close
        # up to the supplied size); no triggers (trigger_price must
        # be 0 for limit orders; market orders MUST NOT carry a
        # trigger_price — the Lighter SDK accepts 0 as the "no
        # trigger" sentinel).
        side = str(_field("side") or "").strip().lower()
        order_type = str(_field("order_type") or "limit").strip().lower()
        time_in_force = str(
            _field("time_in_force") or "good-till-time"
        ).strip().lower()
        reduce_only = bool(_field("reduce_only") or False)
        trigger_price_raw = _field("trigger_price")
        trigger_price = (
            str(trigger_price_raw) if trigger_price_raw is not None else "0"
        )

        if side not in {"buy", "sell"}:
            return {
                "success": False,
                "error": f"invalid side {side!r}; must be 'buy' or 'sell'",
                "account": account,
            }
        if order_type not in {"limit", "market"}:
            return {
                "success": False,
                "error": (
                    f"order_type {order_type!r} not supported; "
                    f"must be 'limit' or 'market'"
                ),
                "account": account, "side": side,
            }
        # Market orders in Lighter are IOC by definition (they match
        # against the resting book). Limit orders must be GTT. We
        # reject any other combination explicitly to avoid silent
        # misconfiguration.
        if order_type == "limit" and time_in_force not in {"good-till-time"}:
            return {
                "success": False,
                "error": (
                    f"time_in_force {time_in_force!r} not supported for "
                    f"limit orders; must be 'good-till-time'"
                ),
                "account": account, "side": side, "order_type": order_type,
            }
        if order_type == "market" and time_in_force not in {
            "good-till-time", "immediate-or-cancel", "ioc",
        }:
            return {
                "success": False,
                "error": (
                    f"time_in_force {time_in_force!r} not supported for "
                    f"market orders; must be 'immediate-or-cancel' "
                    f"(or unset / 'good-till-time' which the SDK treats "
                    f"as IOC for market orders)"
                ),
                "account": account, "side": side, "order_type": order_type,
            }
        # Phase 3A: trigger_price must be 0. We allow zero because
        # the Lighter SDK uses 0 as the sentinel for "no trigger".
        try:
            tp_check = _exact_scale_to_wire(
                trigger_price, decimals=0, field_name="trigger_price",
                allow_zero=True,
            )
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "account": account, "side": side, "order_type": order_type,
            }
        if tp_check != 0:
            return {
                "success": False,
                "error": "trigger_price must be 0 (no triggers)",
                "account": account, "side": side, "order_type": order_type,
            }
        # Market orders MUST NOT carry a trigger price — they are
        # IOC by definition and any non-zero trigger_price is
        # nonsensical for a market order. (The check above already
        # enforces tp_check == 0, but we keep this guard for clarity.)
        if order_type == "market" and reduce_only is False and (
            trigger_price_raw is not None
            and str(trigger_price_raw).strip() not in ("0", "", "0.0", "0.00")
        ):
            return {
                "success": False,
                "error": (
                    "trigger_price must be unset or '0' for market orders"
                ),
                "account": account, "side": side, "order_type": order_type,
            }

        # Resolve credentials and chain.
        try:
            creds = _resolve_account_credentials(account)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc), "account": account,
            }
        chain = creds["chain"]
        try:
            label, base_url = _get_chain_config(chain)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "account": account, "chain": chain,
            }

        # Resolve market symbol -> market_id via the authoritative
        # metadata cache. If the cache is empty (cold start) or does
        # not contain this symbol (stale cache), refresh once.
        symbol = str(_field("symbol") or "").strip().upper()
        if not symbol:
            return {
                "success": False,
                "error": "missing symbol",
                "account": account, "chain": chain,
            }
        md = self._effective_symbol_map(base_url)
        if not md or symbol not in md.values():
            refreshed = self._refresh_market_symbol_map(base_url)
            if refreshed.get("ok"):
                md = self._effective_symbol_map(base_url)
        market_id_int = None
        for mid, sym in md.items():
            if sym == symbol:
                market_id_int = int(mid)
                break
        if market_id_int is None:
            return {
                "success": False,
                "error": (
                    f"symbol {symbol!r} not found in authoritative "
                    f"market catalog on chain {chain}"
                ),
                "account": account, "chain": chain,
            }

        # Re-fetch the market details for size_decimals / price_decimals
        # / min_base_amount / min_quote_amount / status.
        client = self._http_client_for_chain(chain, base_url, creds)
        try:
            market_details_payload = client.order_book_details()
        except LighterHTTPError as exc:
            return {
                "success": False,
                "error": str(exc),
                "account": account, "chain": chain,
                "exchange_response": exc.diagnostics,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": (
                    f"Lighter orderBookDetails fetch failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "account": account, "chain": chain,
            }

        # Find the market entry for our market_id.
        market_entry = None
        for entry in (market_details_payload.get("order_book_details")
                     or []):
            if not isinstance(entry, Mapping):
                continue
            if int(entry.get("market_id") or 0) == market_id_int:
                market_entry = entry
                break
        if market_entry is None:
            return {
                "success": False,
                "error": (
                    f"market_id={market_id_int} ({symbol}) not in "
                    f"orderBookDetails"
                ),
                "account": account, "chain": chain,
            }

        status = str(market_entry.get("status") or "").lower()
        if status != "active":
            return {
                "success": False,
                "error": (
                    f"market {symbol} status={status!r}; "
                    f"must be 'active'"
                ),
                "account": account, "chain": chain,
                "market_id": market_id_int, "symbol": symbol,
            }

        price_decimals = int(market_entry.get("price_decimals") or 0)
        size_decimals = int(market_entry.get("size_decimals") or 0)
        try:
            min_base_amount = Decimal(
                str(market_entry.get("min_base_amount") or "0")
            )
            min_quote_amount = Decimal(
                str(market_entry.get("min_quote_amount") or "0")
            )
        except (InvalidOperation, ValueError) as exc:
            return {
                "success": False,
                "error": f"market metadata has malformed min amounts: {exc}",
                "account": account, "chain": chain,
                "market_id": market_id_int, "symbol": symbol,
            }

        # Caller-supplied price and quantity as decimal strings.
        # The canonical contract puts the size under ``child_order["size"]``
        # (and the price under ``child_order["price"]``); the user's
        # wizard request uses ``size``/``price`` and is preserved under
        # ``structured_request``. Read either in that priority order.
        price_str = str(_field("price") or "").strip()
        quantity_str = (
            str(_field("quantity") or _field("size") or "").strip()
        )
        # For market orders the caller MAY supply price=0 (the SDK
        # does not require a price for IOC market orders). We
        # auto-populate from the market's last_trade_price when
        # missing or zero, so downstream notional checks remain
        # meaningful. The wire-price sent to the SDK is still
        # ``last_trade_price * size_decimals_factor`` because Lighter
        # requires a wire price on every order — for market orders
        # the price is informational (the order matches immediately
        # against the resting book at the live price).
        if order_type == "market":
            if not price_str or Decimal(price_str or "0") == 0:
                # Use last_trade_price as the notional reference.
                price_str = str(
                    market_entry.get("last_trade_price") or "0"
                ).strip() or "0"
        if not price_str:
            return {
                "success": False,
                "error": "missing price",
                "account": account, "chain": chain,
                "market_id": market_id_int, "symbol": symbol,
            }
        if not quantity_str:
            return {
                "success": False,
                "error": "missing quantity",
                "account": account, "chain": chain,
                "market_id": market_id_int, "symbol": symbol,
            }

        # ------------------------------------------------------------------
        # Lighter-specific price quantization (Phase 6 price-floor fix).
        #
        # Lighter requires the wire-price to be an exact multiple of
        # ``1 / (10 ** price_decimals)``. Some upstream ladder
        # distributions (notably TradeDesk's ``half_gaussian`` with
        # sigma=0.45) can produce irrational prices — e.g.
        # ``75263.15789473684`` — that fail this precision check at
        # the agent's pre-validation, even when the price is otherwise
        # reasonable.
        #
        # To handle this defensively at the Lighter adapter boundary
        # (without modifying the frozen exchange-agnostic TradeDesk
        # ``_normalize_ladder`` path), we floor the price to the
        # nearest tick-size increment when it does not fit. The floor:
        #
        #   - preserves exact-precision prices unchanged (the modulo
        #     check skips when the price is already a multiple);
        #   - is at most ``1 / (10 ** price_decimals)`` smaller than the
        #     upstream value, so price-distance effects are bounded;
        #   - never increases the price (and thus never increases
        #     notional or price-distance from mark), so downstream
        #     checks remain conservative;
        #   - never changes a positive price to zero.
        #
        # If the floored price would be non-positive, we surface a
        # sanitized pre-validation error rather than silently
        # substituting a tiny value.
        # ------------------------------------------------------------------
        try:
            price_decimal = Decimal(price_str)
        except (InvalidOperation, ValueError):
            price_decimal = None
        if price_decimal is not None:
            try:
                price_is_positive = price_decimal > 0
            except (InvalidOperation, ValueError):
                # NaN, sNaN, or other non-finite Decimal: skip floor
                price_is_positive = False
        if price_decimal is not None and price_is_positive:
            tick_increment = _price_increment(price_decimals)
            # Floor to tick_increment only if the raw price is not
            # already an exact multiple (preserve exact-precision inputs).
            try:
                not_exact = price_decimal % tick_increment != 0
            except (InvalidOperation, ValueError):
                not_exact = False
            if not_exact:
                floored = (price_decimal // tick_increment) * tick_increment
                if floored <= 0:
                    return {
                        "success": False,
                        "error": (
                            f"price {price_str!r} below tick "
                            f"increment {tick_increment} after floor"
                        ),
                        "account": account, "chain": chain,
                        "market_id": market_id_int, "symbol": symbol,
                    }
                price_str = _scale_from_wire(
                    int(floored * (Decimal(10) ** int(price_decimals))),
                    price_decimals,
                )

        # ------------------------------------------------------------------
        # Lighter-specific size quantization (Phase 6 final fix).
        #
        # Lighter requires the wire-quantity to be an exact multiple of
        # ``1 / (10 ** size_decimals)``. Some upstream ladder distributions
        # (notably TradeDesk's ``half_gaussian`` with sigma=0.45) can
        # produce irrational sizes that fail this precision check at
        # the agent's pre-validation, even when the size fits the
        # exchange's ``min_base_amount`` and notional constraints.
        #
        # To handle this defensively at the Lighter adapter boundary
        # (without modifying the frozen exchange-agnostic TradeDesk
        # ``_normalize_ladder`` path), we floor the quantity to the
        # nearest lot-size increment when it does not fit. The floor:
        #
        #   - preserves exact-precision sizes unchanged (the
        #     `to_integral_value` check below accepts the floored value
        #     when it is already a multiple of the lot increment);
        #   - is at most ``1 / (10 ** size_decimals)`` smaller than the
        #     upstream value, so notional effects are bounded;
        #   - never increases the size, so the notional and min-base
        #     checks downstream remain conservative;
        #   - never changes a ``0`` to a non-zero, so zero-quantity
        #     children remain zero.
        #
        # If the floored quantity would be zero or below
        # ``min_base_amount``, we surface a sanitized pre-validation
        # error rather than silently substituting a tiny value.
        # ------------------------------------------------------------------
        try:
            quantity_decimal = Decimal(quantity_str)
        except (InvalidOperation, ValueError):
            quantity_decimal = None
        if quantity_decimal is not None:
            try:
                quantity_is_positive = quantity_decimal > 0
            except (InvalidOperation, ValueError):
                quantity_is_positive = False
        if quantity_decimal is not None and quantity_is_positive:
            lot_increment = _size_increment(size_decimals)
            # Floor to lot_increment only if the raw quantity is not
            # already an exact multiple (preserve exact-precision inputs).
            try:
                not_exact = quantity_decimal % lot_increment != 0
            except (InvalidOperation, ValueError):
                not_exact = False
            if not_exact:
                floored = (quantity_decimal // lot_increment) * lot_increment
                if floored <= 0:
                    return {
                        "success": False,
                        "error": (
                            f"quantity {quantity_str!r} below lot "
                            f"increment {lot_increment} after floor"
                        ),
                        "account": account, "chain": chain,
                        "market_id": market_id_int, "symbol": symbol,
                    }
                quantity_str = _scale_from_wire(
                    int(floored * (Decimal(10) ** int(size_decimals))),
                    size_decimals,
                )

        # Exact-scale validation and wire conversion.
        try:
            wire_price = _exact_scale_to_wire(
                price_str, decimals=price_decimals, field_name="price",
            )
            wire_size = _exact_scale_to_wire(
                quantity_str, decimals=size_decimals, field_name="quantity",
            )
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "account": account, "chain": chain,
                "market_id": market_id_int, "symbol": symbol,
            }

        # Pre-notional and min-base validation.
        notional = Decimal(quantity_str) * Decimal(price_str)
        if Decimal(quantity_str) < min_base_amount:
            return {
                "success": False,
                "error": (
                    f"quantity {quantity_str} below min_base_amount "
                    f"{min_base_amount}"
                ),
                "account": account, "chain": chain,
                "market_id": market_id_int, "symbol": symbol,
            }
        if notional < min_quote_amount:
            return {
                "success": False,
                "error": (
                    f"notional {notional} below min_quote_amount "
                    f"{min_quote_amount}"
                ),
                "account": account, "chain": chain,
                "market_id": market_id_int, "symbol": symbol,
            }

        # Generate client_order_index exactly once.
        client_order_index = _generate_client_order_index()

        is_ask = 1 if side == "sell" else 0

        return {
            "success": True,
            "account": account,
            "chain": chain,
            "base_url": base_url,
            "client": client,
            "api_key_index": int(creds["apikey_index"]),
            "account_index": int(creds["account_index"]),
            "api_private_key": str(creds["private_key"]),
            "market_id_int": market_id_int,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "is_ask": is_ask,
            "client_order_index": client_order_index,
            "wire_price": wire_price,
            "wire_size": wire_size,
            "price_str": price_str,
            "quantity_str": quantity_str,
            "min_base_amount": min_base_amount,
            "min_quote_amount": min_quote_amount,
            "market_entry": market_entry,
            "reduce_only": reduce_only,
            "trigger_price_str": trigger_price,
        }

    def _place_order_locked(
        self,
        prepared: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Inner single-order placement implementation.

        Assumes the per-(chain, account, api_key) nonce lock is
        ALREADY HELD by the caller. Performs the signed POST and
        response classification, then returns the canonical envelope.

        Reuses the Phase 3A signing/POST/sanitize pipeline verbatim.
        No POST/GET happens anywhere outside the locked ``with`` block
        in the caller; this method does not acquire or release the
        lock itself.
        """
        chain = prepared["chain"]
        client = prepared["client"]
        market_id_int = prepared["market_id_int"]
        symbol = prepared["symbol"]
        side = prepared["side"]
        order_type = prepared["order_type"]
        time_in_force = prepared["time_in_force"]
        is_ask = prepared["is_ask"]
        client_order_index = prepared["client_order_index"]
        wire_price = prepared["wire_price"]
        wire_size = prepared["wire_size"]
        price_str = prepared["price_str"]
        quantity_str = prepared["quantity_str"]
        api_key_index = prepared["api_key_index"]
        account_index = prepared["account_index"]
        api_private_key = prepared["api_private_key"]
        account = prepared["account"]
        # reduce_only is now passed through (Position Manager Close uses
        # reduce_only=True to atomically close up to the supplied size).
        # Phase 3A's hard-coded ``reduce_only=False`` is removed.
        reduce_only = bool(prepared.get("reduce_only", False))

        # Map order_type / time_in_force to the Lighter SDK constants.
        if order_type == "market":
            sdk_order_type = SignerClient.ORDER_TYPE_MARKET
        else:
            sdk_order_type = SignerClient.ORDER_TYPE_LIMIT
        if order_type == "market":
            # Lighter market orders are always IOC (immediate-or-cancel).
            # We accept any explicit IOC value from the caller, defaulting
            # to IOC if the caller passed good-till-time.
            if time_in_force in {"immediate-or-cancel", "ioc"}:
                sdk_time_in_force = (
                    SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
                )
            else:
                sdk_time_in_force = (
                    SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
                )
        else:
            sdk_time_in_force = (
                SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
            )

        try:
            sanitized_resp_send_tx, raw_resp_send_tx = client.place_order(
                account_index=account_index,
                api_key_index=api_key_index,
                market_index=market_id_int,
                client_order_index=client_order_index,
                wire_price=wire_price,
                wire_base_amount=wire_size,
                is_ask=is_ask,
                order_type=sdk_order_type,
                time_in_force=sdk_time_in_force,
                reduce_only=reduce_only,
                trigger_price=0,
                order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY,
                api_private_key=api_private_key,
            )
        except LighterHTTPError as exc:
            err_response: dict = {"diagnostics": exc.diagnostics}
            err_status = getattr(exc, "status", None)
            if err_status is not None:
                err_response["status"] = int(err_status)
            err_body = getattr(exc, "body", None)
            if err_body:
                err_response["body"] = err_body
            return _execution_result(
                request, success=False, error=str(exc),
                account=account, chain=chain,
                market_id=market_id_int, symbol=symbol,
                client_order_index=str(client_order_index),
                exchange_response=err_response,
            )
        except Exception as exc:
            return _execution_result(
                request, success=False,
                error=(
                    f"Lighter place_order failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                account=account, chain=chain,
                market_id=market_id_int, symbol=symbol,
                client_order_index=str(client_order_index),
            )

        # Interpret the response. (Same classification as the
        # monolithic Phase 3A implementation, factored verbatim.)
        code = int(sanitized_resp_send_tx.get("code", 0))
        message = sanitized_resp_send_tx.get("message")
        tx_hash = sanitized_resp_send_tx.get("tx_hash")

        msg_kind = _classify_lighter_sendtx_message(message)
        has_tx_hash = bool(tx_hash and str(tx_hash).strip())
        is_accepted_for_processing = (
            code == 200
            and has_tx_hash
            and msg_kind in ("empty", "advisory")
        )

        if not is_accepted_for_processing:
            if code != 200:
                failure_label = "rejected"
            elif not has_tx_hash:
                failure_label = "ambiguous (no tx_hash)"
            else:
                failure_label = (
                    f"ambiguous (unrecognized message: {msg_kind!r})"
                )
            return _execution_result(
                request, success=False,
                error=(
                    f"Lighter sendTx returned code={code}, message="
                    f"{message!r}, tx_hash={tx_hash!r}; {failure_label}"
                ),
                account=account, chain=chain,
                market_id=market_id_int, symbol=symbol,
                client_order_index=str(client_order_index),
                exchange_response={"send_tx": sanitized_resp_send_tx},
                ambiguous=True,
            )

        submission_advisory = (
            message if msg_kind == "advisory" else None
        )
        return _execution_result(
            request,
            success=True,
            account=account, chain=chain,
            market_id=str(market_id_int), symbol=symbol,
            side=side, order_type=order_type, time_in_force=time_in_force,
            price=price_str, quantity=quantity_str,
            client_order_index=str(client_order_index),
            submission_status="accepted_for_processing",
            status="submitted",
            tx_hash=tx_hash,
            order_id=None,
            order_index=None,
            predicted_execution_time_ms=(
                sanitized_resp_send_tx.get("predicted_execution_time_ms")
            ),
            volume_quota_remaining=(
                sanitized_resp_send_tx.get("volume_quota_remaining")
            ),
            submission_advisory=submission_advisory,
            exchange_response={
                "send_tx": sanitized_resp_send_tx,
                "submitted_transaction": {
                    "tx_type": 14,  # CreateOrder tx type
                    "account_index": account_index,
                    "api_key_index": api_key_index,
                    "market_index": market_id_int,
                    "client_order_index": client_order_index,
                    "base_amount": wire_size,
                    "price": wire_price,
                    "is_ask": is_ask,
                    "order_type": SignerClient.ORDER_TYPE_LIMIT,
                    "time_in_force": (
                        SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
                    ),
                    "reduce_only": 0,
                    "trigger_price": 0,
                    "order_expiry": SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY,
                    "signature": "[REDACTED]",
                },
            },
        )
    def _batch_orders(self, request: Mapping[str, Any]) -> dict:
        """Authenticated ``batch_orders`` dispatcher (Phase 6).

        Lighter has no native bulk-order endpoint that accepts a list
        of new orders; the SDK's ``send_tx_batch`` only accepts
        already-signed ``tx_info`` payloads, so each order must be
        signed individually first. This implementation:

          1) performs FULL pre-submission atomic validation of the
             entire ladder — zero POSTs if any child fails validation;
          2) acquires the per-(chain, account, api_key) nonce lock ONCE
             for the whole group;
          3) submits children sequentially via ``_place_order_locked``
             under the held lock — exactly one native ``place_order``
             POST per child, no automatic retry, stop immediately on
             the first failed or ambiguous submission;
          4) on any submission failure, preserves the partial state by
             stopping immediately and reporting the exact stopped child;
          5) runs a single bounded GET-only verification after the
             entire ladder completes (NOT after every child);
          6) returns the canonical batch envelope with
             ``submission_mode="sequential"``.

        ``submission_mode`` is explicitly "sequential" (not "chunked")
        because the operator's directive treats Lighter as one
        canonical batch_orders request that becomes N sequential
        native create_order submissions under a single nonce lock.

        The child envelopes that we receive from
        ``_place_order_locked`` carry Phase 3A submission semantics
        (``submission_status="accepted_for_processing"``,
        ``client_order_index``, ``tx_hash``). We re-aggregate those
        into the batch envelope the operator's spec requires.

        ``verification_status`` follows the same derivation as
        ``_cancel_order_group`` (commit 82b754b): separate from
        submission success. See that commit for the canonical
        algorithm.
        """
        account = str(request.get("account") or "").strip().lower()
        if not account:
            return _execution_result(
                request, success=False, error="missing account name",
            )
        child_orders = request.get("child_orders") or []
        if not isinstance(child_orders, list) or not child_orders:
            return _execution_result(
                request, success=False,
                error=(
                    "Lighter batch_orders requires non-empty child_orders"
                ),
            )

        # ----------------------------------------------------------------
        # NOTE: The 5% defensive symmetric price-band preflight that
        # previously lived here (commit 60d4478) has been REMOVED.
        # Lighter's server-side limit-price rule is asymmetric or BUY-side
        # only, is not documented, and is not exposed via metadata. Manual
        # SELL orders at prices up to +365.8% above mark are accepted.
        # A constant-based client-side guard incorrectly rejected
        # legitimate SELL ladders.
        #
        # The exchange is now the authoritative validator for allowable
        # price distance. Server-side rejections (HTTP 400 + code 21734)
        # are surfaced through the existing diagnostic pipeline (d1a81cd).
        # ----------------------------------------------------------------

        # ----------------------------------------------------------------
        # 1) Pre-submission atomic validation. Each child is validated
        #    via _prepare_place_order. ZERO POSTs if any child fails.
        # ----------------------------------------------------------------
        prepared_children: list[dict[str, Any]] = []
        for index, child in enumerate(child_orders):
            if not isinstance(child, Mapping):
                return _execution_result(
                    request, success=False,
                    error=(
                        f"child_orders[{index}] is not a mapping"
                    ),
                )
            # The shared _prepare_place_order expects a "request" with
            # the same field sources as the wizard path:
            #   - top-level (legacy direct calls)
            #   - structured_request
            #   - child_order
            #   - child_orders[0]
            # For the ladder we put the per-child fields at the top
            # level inside a synthetic single-child request envelope
            # so _prepare_place_order's _field() helper finds them
            # without us touching TradeDesk.
            child_request = {
                "operation": "order",
                "parent_operation": "batch_orders",
                "exchange": request.get("exchange"),
                "account": request.get("account"),
                "structured_request": dict(request.get("structured_request") or {}),
                "child_order": dict(child),
                "child_orders": [dict(child)],
            }
            # Copy per-child fields to the top level so _field() finds
            # them via its "request" lookup. We deliberately do NOT
            # remove the existing structured_request, child_order, etc.
            # — _field() walks sources in order and stops at the first
            # non-empty value, so redundant writes are harmless.
            for key in ("symbol", "side", "order_type", "size",
                        "quantity", "price", "reduce_only",
                        "time_in_force", "trigger_price"):
                if child.get(key) is not None:
                    child_request[key] = child.get(key)
            prepared = self._prepare_place_order(child_request)
            if not prepared.get("success"):
                # Re-shape the pre-shaped error into the canonical
                # batch envelope so the wizard can show the operator
                # which child failed validation.
                err = prepared.get("error") or "preparation failed"
                return self._build_batch_envelope(
                    request=request,
                    account=account,
                    child_orders=child_orders,
                    prepared_children=prepared_children,
                    child_results=[],
                    pre_validation_error={
                        "child_id": child.get("child_id", index + 1),
                        "child_index": index,
                        "error": err,
                    },
                )
            # Tag each prepared child with its child_id so the result
            # envelope can order results deterministically.
            prepared["_child_id"] = child.get(
                "child_id", index + 1
            )
            prepared["_child_index"] = index
            prepared_children.append(prepared)

        # ----------------------------------------------------------------
        # 2) Acquire the nonce lock once. All subsequent
        #    _place_order_locked calls run under this single lock.
        # ----------------------------------------------------------------
        # All children share the same chain + account + api_key; pick
        # the first prepared child to derive the lock key.
        head = prepared_children[0]
        chain = head["chain"]
        account_index = head["account_index"]
        api_key_index = head["api_key_index"]

        # ----------------------------------------------------------------
        # 3) Submit children sequentially, stop on first failure.
        # 4) Capture per-child results; on stop, preserve partial state.
        # ----------------------------------------------------------------
        per_child_results: list[dict[str, Any]] = []
        stopped_at_index: Optional[int] = None
        stopped_error: Optional[str] = None
        exchange_accepted_count = 0
        submitted_count = 0
        with self._get_nonce_lock(
            chain=chain,
            account_index=account_index,
            api_key_index=api_key_index,
        ):
            for idx, prepared in enumerate(prepared_children):
                # Build a per-child request envelope so the canonical
                # _execution_result helper has an "operation" key to
                # pass through to the inner result.
                child_request = {
                    "operation": "order",
                    "parent_operation": "batch_orders",
                    "exchange": request.get("exchange"),
                    "account": request.get("account"),
                    "chain": chain,
                    "structured_request": dict(
                        request.get("structured_request") or {}
                    ),
                    "child_order": {
                        "child_id": prepared["_child_id"],
                        "symbol": prepared["symbol"],
                        "side": prepared["side"],
                        "order_type": prepared["order_type"],
                        "size": prepared["quantity_str"],
                        "price": prepared["price_str"],
                    },
                    "child_orders": [
                        {
                            "child_id": prepared["_child_id"],
                            "symbol": prepared["symbol"],
                            "side": prepared["side"],
                            "order_type": prepared["order_type"],
                            "size": prepared["quantity_str"],
                            "price": prepared["price_str"],
                        }
                    ],
                }
                inner = self._place_order_locked(prepared, child_request)
                submitted_count += 1
                if inner.get("success"):
                    exchange_accepted_count += 1
                    per_child_results.append({
                        "child_id": prepared["_child_id"],
                        "child_index": idx,
                        "market_index": prepared["market_id_int"],
                        "price": prepared["price_str"],
                        "size": prepared["quantity_str"],
                        "client_order_index": (
                            str(prepared["client_order_index"])
                        ),
                        "tx_hash": inner.get("tx_hash"),
                        "success": True,
                        "submission_status": "submitted",
                        "verification_status": "pending",
                        "order_index": None,
                        "error": None,
                        "ambiguous": False,
                    })
                else:
                    stopped_at_index = idx
                    stopped_error = (
                        inner.get("error") or "submission failed"
                    )
                    # Preserve the sanitized diagnostic that
                    # _place_order_locked stored in ``exchange_response``.
                    # This carries the HTTP status, exchange error
                    # code/message, market_index, client_order_index,
                    # and the bounded sanitized response body. Without
                    # this, the operator has no way to know WHY the
                    # submission failed (e.g. Lighter returned HTTP 400
                    # with a JSON body containing a precise reason).
                    stopped_diagnostic = _build_placement_diagnostic_from_inner(
                        inner, endpoint="/api/v1/sendTx",
                    )
                    per_child_results.append({
                        "child_id": prepared["_child_id"],
                        "child_index": idx,
                        "market_index": prepared["market_id_int"],
                        "price": prepared["price_str"],
                        "size": prepared["quantity_str"],
                        "client_order_index": (
                            str(prepared["client_order_index"])
                        ),
                        "tx_hash": inner.get("tx_hash"),
                        "success": False,
                        "submission_status": "failed",
                        "verification_status": "failed",
                        "order_index": None,
                        "error": stopped_error,
                        "ambiguous": bool(inner.get("ambiguous")),
                        "exchange_response": inner.get(
                            "exchange_response"
                        ),
                        "diagnostic": stopped_diagnostic,
                    })
                    # Stop on first failure. The loop releases the
                    # nonce lock at the end of the `with` block.
                    break

        # ----------------------------------------------------------------
        # 5) Run ONE bounded GET-only verification after the entire
        #    ladder completes. NOT per-child. This mirrors the operator's
        #    directive: "Run bounded verification once after the entire
        #    ladder submission completes."
        # ----------------------------------------------------------------
        # The head prepared child carries the client we need.
        client = head["client"]
        # Build the list of accepted children for verification: for each
        # accepted child, we have its market_index and a stable
        # identifier (client_order_index). We pass through to the
        # bounded verifier which polls accountActiveOrders.
        # For simplicity (and to keep the verify loop small and bounded),
        # we verify by the union of (market_index, client_order_index)
        # presence in the post-read. The verifier below is a thin
        # wrapper that polls and reports counts only.
        accepted_client_order_indices = [
            r["client_order_index"] for r in per_child_results
            if r.get("success")
        ]
        verified_open_count, remaining_unverified_count = (
            self._batch_orders_verify(
                client=client,
                account_index=account_index,
                target_client_order_indices=accepted_client_order_indices,
            )
        )
        # Propagate the verification status into each per-child record.
        # For now we report a single status for all children ("pending"
        # or "complete") per the operator's spec; if partial /
        # mismatch, the operator can re-verify or cancel.
        if exchange_accepted_count > 0:
            # Mark each per-child verification_status as either
            # "complete" or "pending" by parent decision. We do NOT
            # try to identify per-child order_index here because the
            # bounded verifier intentionally returns only counts to
            # stay well below the 180s wall-time budget.
            aggregate_verification = (
                "complete" if remaining_unverified_count == 0
                else "pending"
            )
        else:
            aggregate_verification = "failed"
        for r in per_child_results:
            if r.get("success"):
                r["verification_status"] = aggregate_verification

        # ----------------------------------------------------------------
        # 6) Build the canonical batch envelope.
        # ----------------------------------------------------------------
        # If the loop halted, propagate the sanitized diagnostic that
        # the failed per-child record carries. This is what makes the
        # batch envelope's top-level ``stopped_diagnostic`` non-empty
        # when the inner single-order path has already sanitized the
        # server response (HTTP status, exchange error code/message,
        # bounded response body, market_index, client_order_index).
        envelope_stopped_diagnostic: Optional[dict[str, Any]] = None
        if stopped_at_index is not None and per_child_results:
            envelope_stopped_diagnostic = (
                per_child_results[stopped_at_index].get("diagnostic")
            )
        return self._build_batch_envelope(
            request=request,
            account=account,
            child_orders=child_orders,
            prepared_children=prepared_children,
            child_results=per_child_results,
            pre_validation_error=None,
            verified_open_count=verified_open_count,
            remaining_unverified_count=remaining_unverified_count,
            stopped_at_index=stopped_at_index,
            stopped_error=stopped_error,
            stopped_diagnostic=envelope_stopped_diagnostic,
            exchange_accepted_count=exchange_accepted_count,
            submitted_count=submitted_count,
        )

    def _build_batch_envelope(
        self,
        *,
        request: Mapping[str, Any],
        account: str,
        child_orders: list,
        prepared_children: list[dict[str, Any]],
        child_results: list[dict[str, Any]],
        pre_validation_error: Optional[dict[str, Any]],
        verified_open_count: int = 0,
        remaining_unverified_count: int = 0,
        stopped_at_index: Optional[int] = None,
        stopped_error: Optional[str] = None,
        stopped_diagnostic: Optional[dict[str, Any]] = None,
        exchange_accepted_count: int = 0,
        submitted_count: int = 0,
    ) -> dict[str, Any]:
        """Build the canonical batch_orders envelope.

        Used by ``_batch_orders`` to assemble the result envelope that
        the operator's spec requires. Mirrors the result-classification
        semantics introduced in commit 82b754b for cancellations:
        submission and verification are reported separately, and
        ``partial_success`` is true ONLY when actual partial completion
        is evidenced (some POSTs accepted before a later one failed,
        or some targets verified absent while others remain).
        """
        requested_count = len(child_orders)
        validated_count = len(prepared_children)
        # When pre-validation failed, validated_count is 0; we must
        # still report requested_count accurately.
        is_pre_validation_failure = pre_validation_error is not None

        # Derive verification_status per the operator's spec.
        # Note: pre-validation failure is a structural rejection
        # BEFORE any POST. We treat it as verification_status="failed"
        # AND submission_status="failed".
        if is_pre_validation_failure:
            verification_status = "failed"
            submission_status_eq = "failed"
            success_flag = False
            partial_success = False
            error_msg = (
                f"child_orders[{pre_validation_error.get('child_index')}] "
                f"(child_id={pre_validation_error.get('child_id')}) "
                f"failed pre-validation: "
                f"{pre_validation_error.get('error')}"
            )
            ambiguous = False
        else:
            error_msg = None
            ambiguous = False
            if (
                stopped_at_index is not None
                and exchange_accepted_count == 0
            ):
                # First-POST failure (the live ladder case). The
                # top-level ``error`` must be non-null so the operator
                # sees the sanitized exchange reason. We mirror the
                # partial-submission branch below: the same
                # ``stopped_error`` (already sanitized) is the canonical
                # message. ``error_msg = None`` here was a bug that
                # caused the live ladder to return ``error=null``
                # despite a real HTTP 400.
                verification_status = "failed"
                submission_status_eq = "failed"
                success_flag = False
                partial_success = False
                error_msg = stopped_error
            elif (
                stopped_at_index is not None
                and exchange_accepted_count > 0
            ):
                # Loop halted. Some accepted, then a failure.
                verification_status = "partial"
                submission_status_eq = "partial"
                success_flag = False
                partial_success = True
                error_msg = stopped_error
            elif (
                exchange_accepted_count == requested_count
                and remaining_unverified_count == 0
            ):
                verification_status = "complete"
                submission_status_eq = "complete"
                success_flag = True
                partial_success = False
            elif (
                exchange_accepted_count == requested_count
                and verified_open_count > 0
                and verified_open_count < exchange_accepted_count
            ):
                # All POSTs accepted but only some verified absent.
                verification_status = "partial"
                submission_status_eq = "partial"
                success_flag = True
                partial_success = True
            elif exchange_accepted_count == requested_count:
                # All POSTs accepted; bounded post-read either
                # confirmed 0 or ran out of time.
                #
                # SUBMISSION status: "submitted" (every POST was
                # accepted; nothing went wrong on the wire).
                # VERIFICATION status: "pending" (bounded post-read
                # has not yet confirmed propagation through the
                # exchange read model). These are reported
                # separately per commit 82b754b semantics.
                verification_status = "pending"
                submission_status_eq = "submitted"
                success_flag = True
                partial_success = False
            else:
                verification_status = "mismatch"
                submission_status_eq = "mismatch"
                success_flag = False
                partial_success = False
                error_msg = (
                    "batch_orders result counts could not be reconciled"
                )

        # ``submission_status`` is the canonical placement-side field.
        # We never emit ``cancellation_status`` here because
        # batch_orders is a placement operation. ``status`` is kept as
        # an alias of ``submission_status`` so legacy renderers still
        # work, but the placement-specific meaning lives in
        # ``submission_status`` alone.
        return _execution_result(
            request,
            success=success_flag,
            account=account,
            chain=(
                prepared_children[0].get("chain")
                if prepared_children
                else None
            ),
            exchange=request.get("exchange"),
            operation="batch_orders",
            parent_operation=request.get("parent_operation") or "ladder",
            submission_status=submission_status_eq,
            verification_status=verification_status,
            status=submission_status_eq,
            submission_mode="sequential",
            requested_count=requested_count,
            validated_count=validated_count,
            submitted_count=submitted_count,
            exchange_accepted_count=exchange_accepted_count,
            verified_open_count=verified_open_count,
            remaining_unverified_count=remaining_unverified_count,
            stopped_at_index=stopped_at_index,
            stopped_error=stopped_error,
            stopped_diagnostic=stopped_diagnostic,
            ambiguous=ambiguous,
            partial_success=partial_success,
            error=error_msg,
            children=child_results,
            distribution=request.get("distribution"),
            structured_request=dict(
                request.get("structured_request") or {}
            ),
        )

    def _batch_orders_verify(
        self,
        *,
        client: Any,
        account_index: int,
        target_client_order_indices: list[str],
    ) -> tuple[int, int]:
        """Bounded GET-only verification for a batch_orders submission.

        Polls ``/api/v1/accountActiveOrders`` up to
        ``VERIFICATION_MAX_READS`` times within
        ``VERIFICATION_MAX_WALL_TIME_S`` seconds. Returns
        ``(verified_open_count, remaining_unverified_count)``.

        Per the operator's directive, this is called ONCE after the
        entire ladder submission completes — NOT after each child.

        The verifier is conservative: it does NOT try to identify
        which specific child corresponds to which open-order row.
        Instead it counts the target ``client_order_index`` set
        against the bounded post-read. If even one target is
        still present in the post-read, we report it as
        ``remaining_unverified_count > 0`` so the envelope can
        surface "pending" rather than a misleading "complete".

        On any GET error the verifier stops immediately and reports
        whatever it confirmed so far.
        """
        if not target_client_order_indices:
            return 0, 0
        targets: set[str] = set(str(t) for t in target_client_order_indices)
        verified: set[str] = set()
        deadline = time.monotonic() + VERIFICATION_MAX_WALL_TIME_S
        sleep_ms = VERIFICATION_MIN_SLEEP_MS
        prev_current_indices: Optional[set[str]] = None
        no_progress_streak = 0
        for _ in range(VERIFICATION_MAX_READS):
            if time.monotonic() >= deadline:
                break
            try:
                payload = client.account_active_orders(account_index)
            except Exception:
                # GET failure: stop conservatively.
                break
            current_client_order_indices: set[str] = set()
            for o in (payload.get("orders") or []):
                if not isinstance(o, Mapping):
                    continue
                coi = str(o.get("client_order_index") or "")
                if coi and coi != "0":
                    current_client_order_indices.add(coi)
            for tgt in list(targets):
                if tgt in verified:
                    continue
                if tgt not in current_client_order_indices:
                    verified.add(tgt)
            if targets.issubset(verified):
                break
            # Conservative no-progress detector.
            if (
                prev_current_indices is not None
                and prev_current_indices == current_client_order_indices
            ):
                no_progress_streak += 1
                if no_progress_streak >= 2:
                    break
            else:
                no_progress_streak = 0
            prev_current_indices = current_client_order_indices
            remaining = max(int(time.monotonic() + VERIFICATION_MAX_WALL_TIME_S - deadline), 0)
            if remaining <= 0:
                break
            sleep_ms = min(
                sleep_ms * 2, VERIFICATION_MAX_SLEEP_MS, remaining * 1000
            )
            if sleep_ms <= 0:
                break
            time.sleep(sleep_ms / 1000.0)
        verified_open = len(targets & verified)
        remaining_unverified = max(len(targets) - verified_open, 0)
        return verified_open, remaining_unverified
    # -- Phase 3B: cancel_order (read-write LIMIT order cancellation) --

    def _cancel_order(self, request: Mapping[str, Any]) -> dict:
        """Authenticated ``cancel_order`` dispatcher.

        Phase 3B authorizes cancelling a single existing open order by
        its server-assigned ``order_index``. The implementation reuses
        Phase 3A's nonce-lock, signing, request construction,
        response sanitization, and verification primitives unchanged.

        The exchange-agnostic Cancel Orders wizard (commit 3eca9ce)
        issues a single canonical request like::

            {operation: "cancel_orders", exchange, account,
             symbol: "BTC", side: "sell", order_type: "limit"}

        without an explicit ``order_index``. Lighter, however, requires
        a server-assigned ``order_index`` for every cancel-order
        transaction. This dispatcher therefore accepts BOTH:

          (a) a single-order request with explicit ``order_index``
              (legacy direct-call path, preserved unchanged), and
          (b) a group request with symbol/side filters — which
              internally reads the current open orders, applies the
              canonical filters, extracts each matched order's
              server-assigned ``order_index``, validates every
              ``order_index`` is a positive integer (never substitutes
              ``client_order_index``), and submits one cancel POST per
              matched order with stop-on-first-failure semantics.

        Required request fields:

          account      - account name (e.g. "example")
          order_index  - (optional, single-order path) the server-assigned
                          numeric order index of one order to cancel
          market_id    - Lighter market id (1=BTC, 24=HYPE, 180=US500)
                         OR symbol (e.g. "BTC") which is resolved via
                         the existing Phase 2B market-symbol cache.
                         Used to scope the group fetch when supplied.
          symbol       - (group path) target symbol; mutually
                         informative with market_id
          side         - (group path) "buy" or "sell"; if absent,
                         every matched symbol is selected

        The dispatcher does NOT validate that the order exists or
        that it is owned by the account; the Lighter server is the
        authority on both. The bounded GET-only verification after the
        submit confirms the cancellation has been applied.
        """
        # Canonical contract: TradeDesk.normalize() emits a dict whose
        # user-facing request lives under "structured_request". Helper:
        # read a field from any of (top-level, structured_request,
        # child_order, child_orders[0]) to support BOTH the canonical
        # wizard-path invocation (TradeDesk.execute()) and the direct-
        # call invocation (legacy tests).
        def _field(name: str) -> Any:
            for source in (request,
                           request.get("structured_request") if isinstance(request.get("structured_request"), Mapping) else None,
                           request.get("child_order") if isinstance(request.get("child_order"), Mapping) else None,
                           (request.get("child_orders") or [None])[0] if isinstance(request.get("child_orders"), list) else None):
                if source is not None and source.get(name) is not None:
                    return source.get(name)
            return None

        account = str(_field("account") or "").strip().lower()
        if not account:
            return _execution_result(
                request, success=False, error="missing account name",
            )

        # ---- resolve market_id: from int, from string-of-int, or
        # from "symbol" via the market-symbol cache. Used to scope
        # the open-orders fetch on the group path. ----
        market_id_int: Optional[int] = None
        for key in ("market_id", "market_index"):
            v = _field(key)
            if v is None:
                continue
            try:
                market_id_int = int(v)
                break
            except (TypeError, ValueError):
                continue

        # ---- resolve the order_index (single-order path only).
        # If absent, we fall through to the group-cancel path. ----
        order_index_raw = _field("order_index")
        order_index_int: Optional[int] = None
        if order_index_raw is not None:
            try:
                order_index_int = int(order_index_raw)
            except (TypeError, ValueError):
                return _execution_result(
                    request, success=False,
                    error=(
                        "invalid order_index; supply the "
                        "server-assigned numeric order_index of the "
                        "order to cancel"
                    ),
                    account=account,
                    market_id=market_id_int,
                )
            if order_index_int <= 0:
                return _execution_result(
                    request, success=False,
                    error="order_index must be a positive integer",
                    account=account, market_id=market_id_int,
                )

        # ---- group-cancel canonical filters (read from the request;
        # None means "no filter"). ----
        group_symbol = str(_field("symbol") or "").strip().upper() or None
        group_side_raw = str(_field("side") or "").strip().lower() or None
        group_side: Optional[str] = None
        if group_side_raw in {"buy", "b", "bid", "long"}:
            group_side = "buy"
        elif group_side_raw in {"sell", "s", "ask", "short"}:
            group_side = "sell"

        # If a symbol string was provided but market_id wasn't, resolve
        # market_id from the authoritative cache.
        if group_symbol and market_id_int is None:
            try:
                creds_for_resolve = _resolve_account_credentials(account)
            except ValueError:
                creds_for_resolve = None
            if creds_for_resolve is not None:
                try:
                    base_url_for_resolve = _get_chain_config(
                        creds_for_resolve["chain"]
                    )[1]
                    md = self._effective_symbol_map(base_url_for_resolve)
                    if not md or group_symbol not in md.values():
                        self._refresh_market_symbol_map(base_url_for_resolve)
                        md = self._effective_symbol_map(base_url_for_resolve)
                    for mid, sym in md.items():
                        if sym == group_symbol:
                            market_id_int = int(mid)
                            break
                except (ValueError, Exception):  # noqa: BLE001
                    pass

        # ---- dispatch. ----
        if order_index_int is not None:
            # Single-order legacy path.
            return self._cancel_order_single(
                request=request,
                account=account,
                market_id_int=market_id_int,
                order_index_int=order_index_int,
            )
        # Group-cancel canonical path.
        return self._cancel_order_group(
            request=request,
            account=account,
            market_id_int=market_id_int,
            group_symbol=group_symbol,
            group_side=group_side,
        )

    def _cancel_order_single(
        self,
        *,
        request: Mapping[str, Any],
        account: str,
        market_id_int: Optional[int],
        order_index_int: int,
    ) -> dict:
        """Single-order cancel primitive.

        Validated native path: takes one server-assigned
        ``order_index`` and submits one cancel-order transaction. Used
        by the single-order direct-call path AND by the group-cancel
        path (one call per matched order).

        Preserves Phase 3A's nonce-lock, signing, request construction,
        response sanitization, and stop-on-first-failure semantics.
        """        # ---- resolve account credentials and chain. ----
        try:
            creds = _resolve_account_credentials(account)
        except ValueError as exc:
            return _execution_result(
                request, success=False, error=str(exc),
                account=account, market_id=market_id_int,
                order_index=str(order_index_int),
            )
        chain = creds["chain"]
        try:
            label, base_url = _get_chain_config(chain)
        except ValueError as exc:
            return _execution_result(
                request, success=False, error=str(exc),
                account=account, market_id=market_id_int,
                order_index=str(order_index_int),
            )

        # The Lighter cancel-order wire payload requires a market_id.
        # If the caller supplied ``order_index`` but no market_id
        # (or a symbol that didn't resolve), refuse safely BEFORE
        # any POST.
        if market_id_int is None:
            return _execution_result(
                request, success=False,
                error=(
                    "missing market_id; the single-order cancel path "
                    "requires the Lighter market_id of the order "
                    "(or a resolvable symbol)"
                ),
                account=account, chain=chain,
                market_id=market_id_int,
                order_index=str(order_index_int),
            )

        # ---- generate client_order_index and call the HTTP client
        # under the per-key nonce lock. The nonce-lock uses the
        # exact same per-(chain, account, api_key) tuple as
        # ``_place_order``. An external cancellation shares the
        # nonce lock with a placement; this prevents nonce races
        # between concurrent writes for the same API key. ----
        client_order_index = _generate_client_order_index()
        api_key_index = int(creds["apikey_index"])
        account_index = int(creds["account_index"])
        api_private_key = str(creds["private_key"])

        client = self._http_client_for_chain(chain, base_url, creds)
        with self._get_nonce_lock(
            chain=chain,
            account_index=account_index,
            api_key_index=api_key_index,
        ):            return self._cancel_order_single_locked(
                request=request,
                account=account,
                chain=chain,
                client=client,
                market_id_int=market_id_int,
                order_index_int=order_index_int,
                api_key_index=api_key_index,
                account_index=account_index,
                api_private_key=api_private_key,
            )

    def _cancel_order_single_locked(
        self,
        *,
        request: Mapping[str, Any],
        account: str,
        chain: str,
        client: Any,
        market_id_int: Optional[int],
        order_index_int: int,
        api_key_index: int,
        account_index: int,
        api_private_key: str,
    ) -> dict:
        """Inner single-order cancel implementation.

        Assumes the per-(chain, account, api_key) nonce lock is
        ALREADY HELD by the caller. The single-order public path
        acquires it; the group-cancel path holds it across the
        loop and calls this internal method directly.

        Performs the cancel POST, classifies the response, and
        returns the canonical envelope. Reuses the Phase 3A
        signing/POST/sanitize pipeline verbatim.
        """
        client_order_index = _generate_client_order_index()
        try:
            sanitized_resp_send_tx, raw_resp_send_tx = client.cancel_order(
                account_index=account_index,
                api_key_index=api_key_index,
                market_index=market_id_int,
                order_index=order_index_int,
                api_private_key=api_private_key,
            )
        except LighterHTTPError as exc:
            err_response: dict = {"diagnostics": exc.diagnostics}
            err_body = getattr(exc, "body", None)
            if err_body:
                err_response["body"] = err_body
            return _execution_result(
                request, success=False, error=str(exc),
                account=account, chain=chain,
                market_id=market_id_int,
                order_index=str(order_index_int),
                client_order_index=str(client_order_index),
                exchange_response=err_response,
            )
        except Exception as exc:
            return _execution_result(
                request, success=False,
                error=(
                    f"Lighter cancel_order failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                account=account, chain=chain,
                market_id=market_id_int,
                order_index=str(order_index_int),
                client_order_index=str(client_order_index),
            )

        # ---- interpret the response. We reuse the Phase 3A
        # classification logic verbatim (the response envelope is
        # the same RespSendTx shape). ----
        code = int(sanitized_resp_send_tx.get("code", 0))
        message = sanitized_resp_send_tx.get("message")
        tx_hash = sanitized_resp_send_tx.get("tx_hash")

        msg_kind = _classify_lighter_sendtx_message(message)
        has_tx_hash = bool(tx_hash and str(tx_hash).strip())
        is_cancel_accepted = (
            code == 200
            and has_tx_hash
            and msg_kind in ("empty", "advisory")
        )

        if not is_cancel_accepted:
            if code != 200:
                failure_label = "rejected"
            elif not has_tx_hash:
                failure_label = "ambiguous (no tx_hash)"
            else:
                failure_label = (
                    f"ambiguous (unrecognized message: {msg_kind!r})"
                )
            return _execution_result(
                request, success=False,
                error=(
                    f"Lighter sendTx (cancel) returned code={code}, "
                    f"message={message!r}, tx_hash={tx_hash!r}; "
                    f"{failure_label}"
                ),
                account=account, chain=chain,
                market_id=market_id_int,
                order_index=str(order_index_int),
                client_order_index=str(client_order_index),
                exchange_response={
                    "send_tx": sanitized_resp_send_tx,
                },
                ambiguous=True,
            )

        submission_advisory = (
            message if msg_kind == "advisory" else None
        )

        # Phase 3B success envelope. We deliberately do NOT
        # synthesize any cancellation confirmation beyond what the
        # server returned. ``cancellation_status`` is set to
        # "submitted" — the same semantics as place_order's
        # accepted_for_processing — and only flips to
        # "confirmed_cancelled" after the bounded GET-only
        # verification finds the order absent from
        # accountActiveOrders.
        return _execution_result(
            request,
            success=True,
            account=account, chain=chain,
            market_id=str(market_id_int),
            order_index=str(order_index_int),
            client_order_index=str(client_order_index),
            cancellation_status="submitted",
            status="submitted",
            order_id=None,
            tx_hash=tx_hash,
            submission_advisory=submission_advisory,
            predicted_execution_time_ms=(
                sanitized_resp_send_tx.get("predicted_execution_time_ms")
            ),
            volume_quota_remaining=(
                sanitized_resp_send_tx.get("volume_quota_remaining")
            ),
            exchange_response={
                "send_tx": sanitized_resp_send_tx,
                "submitted_cancellation": {
                    "tx_type": 15,  # CancelOrder tx type (Lighter protocol)
                    "account_index": account_index,
                    "api_key_index": api_key_index,
                    "market_index": market_id_int,
                    "order_index": order_index_int,
                    "client_order_index": client_order_index,
                    "signature": "[REDACTED]",
                },
            },
        )

    def _cancel_order_group(
        self,
        *,
        request: Mapping[str, Any],
        account: str,
        market_id_int: Optional[int],
        group_symbol: Optional[str],
        group_side: Optional[str],
    ) -> dict:
        """Group-cancel canonical path.

        Resolves the canonical filter intent
        (symbol, side, market_id) against the current open-orders
        list, extracts each matched order's server-assigned numeric
        ``order_index``, and submits one cancel-order transaction per
        matched order. Never substitutes ``client_order_index`` for
        ``order_index`` — those are different fields with different
        semantics on the Lighter protocol.

        Safety properties preserved:

          - Reads via the Phase 2B ``_fetch_all_active_orders_raw``
            (paginated, capped, read-only).
          - Pre-submit validation: every matched order must carry a
            positive numeric ``order_index``. If even one matched
            order lacks this, we fail safely before any POST.
          - One POST per matched order under the same per-(chain,
            account, api_key) nonce lock.
          - Stop on first failure: any single-order rejection or
            exception halts the loop and returns the partial result.
            No automatic retry.
          - Bounded GET-only post-read verification: at most
            ``VERIFICATION_MAX_READS`` (6) reads within
            ``VERIFICATION_MAX_WALL_TIME_S`` (180s) to confirm the
            cancelled orders are absent from accountActiveOrders.
          - The result envelope carries ``requested_count``,
            ``matched_orders``, ``verified_canceled_count``,
            ``remaining_target_count``, plus per-order diagnostics.
        """        # ---- resolve account credentials and chain. ----
        try:
            creds = _resolve_account_credentials(account)
        except ValueError as exc:
            return _execution_result(
                request, success=False, error=str(exc),
                account=account,
                market_id=market_id_int,
            )
        chain = creds["chain"]
        try:
            label, base_url = _get_chain_config(chain)
        except ValueError as exc:
            return _execution_result(
                request, success=False, error=str(exc),
                account=account, chain=chain, market_id=market_id_int,
            )
        client = self._http_client_for_chain(chain, base_url, creds)
        # ---- 1. Fetch the current open orders. We fetch with the
        # resolved market_id filter when available so we don't pull
        # orders for irrelevant markets; otherwise we fetch the full
        # account and filter client-side (the canonical wizard
        # typically does not pass market_id directly). ----
        try:
            loaded = self._fetch_all_active_orders_raw(
                client=client,
                account_index=int(creds["account_index"]),
                market_id=market_id_int,
            )
        except LighterHTTPError as exc:
            return _execution_result(
                request, success=False, error=str(exc),
                account=account, chain=chain, market_id=market_id_int,
                exchange_response=exc.diagnostics,
            )
        except Exception as exc:
            return _execution_result(
                request, success=False,
                error=(
                    f"Lighter open-orders fetch failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                account=account, chain=chain, market_id=market_id_int,
            )

        raw_orders = loaded.get("orders") or []
        if not isinstance(raw_orders, list):
            raw_orders = []

        # ---- 2. Apply canonical filters and extract server-assigned
        # numeric order_index values. NEVER substitute
        # ``client_order_index`` for ``order_index``: the Lighter
        # cancel transaction requires the server-assigned order
        # nonce; the client_order_index is only used by place_order
        # for idempotency on the server side, and the cancel-order
        # transaction's OrderNonce field is the order_index. ----
        matched_orders: list[dict[str, Any]] = []
        missing_order_index: list[dict[str, Any]] = []
        for raw in raw_orders:
            if not isinstance(raw, Mapping):
                continue
            try:
                this_market = int(raw.get("market_index") or 0)
            except (TypeError, ValueError):
                continue
            if market_id_int is not None and this_market != market_id_int:
                continue
            # Side: is_ask True means sell. Cancel-wizard sends
            # canonical lowercase side. We accept both representations.
            is_ask_raw = raw.get("is_ask")
            if isinstance(is_ask_raw, bool):
                this_side = "sell" if is_ask_raw else "buy"
            else:
                this_side_norm = str(is_ask_raw or "").strip().lower()
                this_side = (
                    "sell" if this_side_norm in {"1", "true", "sell", "s", "ask"}
                    else "buy" if this_side_norm in {"0", "false", "buy", "b", "bid"}
                    else None
                )
            if this_side is None:
                continue
            if group_side is not None and this_side != group_side:
                continue
            order_index_raw = raw.get("order_index")
            try:
                this_order_index = int(order_index_raw or 0)
            except (TypeError, ValueError):
                this_order_index = 0
            if this_order_index <= 0:
                # We matched by other fields but could not recover a
                # valid order_index. Per spec we MUST NOT substitute
                # client_order_index. Track for safe pre-submit failure.
                missing_order_index.append({
                    "market_index": this_market,
                    "side": this_side,
                    "raw_order_index": order_index_raw,
                    "client_order_index": raw.get("client_order_index"),
                })
                continue
            matched_orders.append({
                "market_index": this_market,
                "side": this_side,
                "order_index": this_order_index,
                "client_order_index": raw.get("client_order_index"),
                "price": raw.get("price"),
                "raw_status": raw.get("status"),
            })
        # ---- 3. Pre-submit safety gate: if any matched order lacks
        # a valid numeric order_index, refuse to submit. ----
        if missing_order_index:
            return _execution_result(
                request, success=False,
                error=(
                    "matched open orders lack a valid server-assigned "
                    "order_index; refusing to submit any cancellations "
                    "to avoid substituting client_order_index"
                ),
                account=account, chain=chain,
                market_id=market_id_int,
                matched_orders=matched_orders,
                missing_order_index=missing_order_index,
                requested_count=(
                    len(matched_orders) + len(missing_order_index)
                ),
                ambiguous=True,
            )

        # ---- 4. Empty selection is a clean no-op. ----
        if not matched_orders:
            return _execution_result(
                request, success=True,
                account=account, chain=chain,
                market_id=market_id_int,
                requested_count=0,
                matched_orders=[],
                canceled_count=0,
                verified_canceled_count=0,
                remaining_target_count=0,
                cancellation_status="submitted",
                status="submitted",
                matched_order_count=0,
            )

        # ---- 5. Iterate: split matched_orders into chunks of at most
        # LIGHTER_CANCEL_CHUNK_SIZE (20). Each chunk is submitted
        # atomically under its own nonce-lock acquisition. If a
        # single-order POST fails inside a chunk, we stop that
        # chunk and stop the whole run — no retry, no attempt to
        # process remaining chunks. ----
        api_key_index = int(creds["apikey_index"])
        account_index = int(creds["account_index"])
        per_order_results: list[dict[str, Any]] = []
        stopped_at_index: Optional[int] = None
        stopped_error: Optional[str] = None
        submitted_count = 0
        exchange_accepted_count = 0
        # Chunk-tracking diagnostics required by the operator's spec.
        chunks_submitted = 0
        chunks_succeeded = 0
        chunks_failed = 0
        failed_chunk_index: Optional[int] = None
        failed_chunk_error: Optional[str] = None
        # matched_orders is consumed in fixed-size chunks. We slice
        # with [start:start+chunk_size] so each iteration is bounded.
        chunk_size = int(LIGHTER_CANCEL_CHUNK_SIZE)
        if chunk_size <= 0:
            chunk_size = 1
        total_to_submit = len(matched_orders)
        i = 0
        # Outer loop: one chunk per iteration.
        while i < total_to_submit:
            chunk = matched_orders[i : i + chunk_size]
            chunk_index = chunks_submitted  # 0-based chunk index
            # Each chunk runs under its own nonce-lock acquisition.
            # Chunks are independent from each other's nonce-monotonicity
            # because each chunk consumes its own consecutive nonces.
            with self._get_nonce_lock(
                chain=chain,
                account_index=account_index,
                api_key_index=api_key_index,
            ):
                chunk_had_failure = False
                for j, target in enumerate(chunk):
                    target_market_id = target["market_index"]
                    target_order_index = target["order_index"]
                    # Build a synthetic single-order request envelope so
                    # _cancel_order_single_locked reuses the full path.
                    sub_request = {
                        "operation": "cancel_orders",
                        "parent_operation": "cancel_orders",
                        "exchange": "lighter",
                        "account": account,
                        "chain": chain,
                        "market_id": target_market_id,
                        "order_index": str(target_order_index),
                        "order_type": "limit",
                        "structured_request": dict(request),
                        "_caller_request": request,
                    }
                    # The nonce lock is already held by the outer
                    # ``with self._get_nonce_lock(...)`` block above.
                    # We MUST NOT acquire it again here, or the
                    # thread deadlocks on itself. Call the inner
                    # helper that assumes the lock is already held.
                    sub_result = self._cancel_order_single_locked(
                        request=sub_request,
                        account=account,
                        chain=chain,
                        client=client,
                        market_id_int=target_market_id,
                        order_index_int=target_order_index,
                        api_key_index=api_key_index,
                        account_index=account_index,
                        api_private_key=str(creds["private_key"]),
                    )
                    submitted_count += 1
                    # Determine if the cancel was exchange-accepted.
                    if sub_result.get("success"):
                        exchange_accepted_count += 1
                        per_order_results.append({
                            "market_index": target_market_id,
                            "order_index": target_order_index,
                            "tx_hash": sub_result.get("tx_hash"),
                            "success": True,
                            "chunk_index": chunk_index,
                            "cancellation_status": sub_result.get(
                                "cancellation_status"
                            ),
                            "submission_advisory": sub_result.get(
                                "submission_advisory"
                            ),
                        })
                    else:
                        # Single-order failure inside this chunk:
                        # record the failure, mark the chunk as
                        # failed, halt this chunk, and BREAK OUT OF
                        # THE OUTER LOOP so no further chunks run.
                        stopped_at_index = i + j
                        stopped_error = sub_result.get("error") or "unknown"
                        chunk_had_failure = True
                        failed_chunk_index = chunk_index
                        failed_chunk_error = stopped_error
                        per_order_results.append({
                            "market_index": target_market_id,
                            "order_index": target_order_index,
                            "tx_hash": sub_result.get("tx_hash"),
                            "success": False,
                            "chunk_index": chunk_index,
                            "error": stopped_error,
                            "ambiguous": bool(sub_result.get("ambiguous")),
                        })
                        break
            # Outside the per-chunk lock: finalize chunk outcome.
            chunks_submitted += 1
            if chunk_had_failure:
                chunks_failed += 1
                # Mark every still-unattempted target as a
                # remaining-not-attempted entry. The outer loop
                # below will terminate.
                i = total_to_submit  # exit the outer chunk loop
            else:
                chunks_succeeded += 1
                i += len(chunk)
        # ---- 6. Bounded GET-only post-read verification: at most
        # VERIFICATION_MAX_READS reads in VERIFICATION_MAX_WALL_TIME_S.
        # We poll /api/v1/accountActiveOrders and check that every
        # submitted order_index is absent. The post-read does NOT
        # acquire the nonce lock; it's GET-only. ----
        verified_canceled_count, remaining_target_count = (
            self._cancel_order_group_verify(
                client=client,
                target_order_indices=[
                    r["order_index"] for r in per_order_results
                    if r.get("success")
                ],
            )
        )

        # ---- 6b. Build the list of orders that were NEVER submitted
        # because chunking was incomplete (stop-on-first-failure or
        # the run had fewer chunks than matched_orders implied). These
        # are the orders the operator must retry.
        #
        # The cutoff is the FIRST index that was not attempted:
        #   - If chunk N failed at child j of that chunk, the failed
        #     child's index is (N * chunk_size + j). The NEXT index
        #     (failed_idx + 1) is the first one that was not attempted.
        #   - All matched_orders[i] for i >= (failed_idx + 1) were
        #     never attempted (the rest of the failed chunk + every
        #     subsequent chunk in its entirety).
        remaining_orders_not_attempted: list[dict[str, Any]] = []
        if chunks_failed > 0 and stopped_at_index is not None:
            cutoff_start = stopped_at_index + 1
            for leftover in matched_orders[cutoff_start:]:
                remaining_orders_not_attempted.append({
                    "market_index": leftover["market_index"],
                    "order_index": leftover["order_index"],
                    "client_order_index": leftover.get("client_order_index"),
                })

        # ---- 7. Build the canonical result envelope.
        #
        # Result-classification semantics (Phase 3B result refinement):
        #
        # Submission and verification are represented SEPARATELY.
        # The envelope ``success`` field reflects SUBMISSION only.
        # Verification is reported in a new ``verification_status`` field
        # that the wizard renders explicitly, never collapsing into a
        # misleading success/failure narrative.
        #
        # - submission_success: every POST was exchange-accepted AND no
        #   earlier POST failed before any accepted submission.
        # - verification_status: complete | pending | partial | failed
        #   | mismatch — derived from the bounded post-read evidence
        #   and the loop's stop point.
        # - partial_success: true ONLY when actual partial completion
        #   is evidenced — some POSTs accepted before a later one
        #   failed, or some targets verified absent while others
        #   remain. NOT set when verification is merely pending.
        # - error: submission-side failure or structural mismatch only.
        #   Never the misleading "N of M still open" string when
        #   every POST was accepted; that case is "pending" not "err"
        # - cancellation_status: submitted when every POST was
        #   accepted but verification is still pending; complete when
        #   all targets verified absent; partial when actual partial;
        #   failed when no successful submission; mismatch when
        #   reconciliation is unsafe.
        #
        # Frozen execution components (canonical dispatch, group
        # resolution, native order_index extraction, client_order_index
        # protection, pre-submit validation, nonce locking, one POST
        # per matched order, stop-on-first-failure, no-retry,
        # sanitizer, bounded verification limits) are NOT touched
        # here. Only the derived envelope fields change.
        #
        # The frozen execution components are unchanged.
        # ----
        requested_count = len(matched_orders)
        exchange_accepted_count = int(exchange_accepted_count)
        verified_canceled_count = int(verified_canceled_count)
        remaining_target_count = int(remaining_target_count)

        # Submission success: every POST was accepted AND no earlier
        # POST failed before any accepted cancellation.
        submission_success = (
            stopped_at_index is None
            and requested_count > 0
            and exchange_accepted_count == requested_count
        )

        # Sanity check: count invariants must hold for any well-formed
        # run. If they don't, surface as a structural mismatch.
        #
        # Edge case: when NO submission was accepted (the loop halted
        # before any successful POST), ``target_order_indices`` passed
        # to the verify loop is empty. The verify loop therefore
        # returns ``(0, 0)`` — both zero, not inconsistent, just
        # trivially satisfied because there is nothing to verify.
        # We must NOT treat that as a mismatch; we should classify
        # based on the loop's stop point and submission evidence.
        #
        # The correct invariant is: for each successfully submitted
        # order_index (i.e. exchange_accepted_count of them), the
        # bounded post-read must place it in either verified-canceled
        # or remaining-pending. So:
        #     verified + remaining >= exchange_accepted_count
        # (NOT requested - exchange_accepted_count: orders that were
        # never submitted have no post-read obligation).
        if (
            0 <= verified_canceled_count <= requested_count
            and 0 <= remaining_target_count <= requested_count
            and verified_canceled_count <= exchange_accepted_count
            and remaining_target_count <= exchange_accepted_count
            and verified_canceled_count + remaining_target_count
                == exchange_accepted_count
        ):
            counts_consistent = True
        elif exchange_accepted_count == 0 and verified_canceled_count == 0 and remaining_target_count == 0:
            # No submission was accepted; the verify loop had nothing
            # to verify. Counts are trivially consistent.
            counts_consistent = True
        else:
            counts_consistent = False

        # When all POSTs were accepted, the post-read must place every
        # target in either verified-canceled or remaining-pending.
        # If the post-read returned fewer rows than expected (which
        # would be a bounded-verify bug or partial-API response), we
        # still treat it as "pending" rather than "mismatch" so the
        # wizard can show the operator and a re-verify can finish.
        # The only structural mismatch condition we raise is when the
        # numbers are physically incompatible (e.g. exchange_accepted
        # > requested_count, or verified + remaining wildly off).
        if not counts_consistent or exchange_accepted_count > requested_count:
            verification_status = "mismatch"
        elif verified_canceled_count == requested_count:
            verification_status = "complete"
        elif stopped_at_index is not None:
            # Loop halted. If we accepted any POST before the halt,
            # the cancellation is partial; otherwise it failed.
            verification_status = (
                "partial" if exchange_accepted_count > 0 else "failed"
            )
        elif exchange_accepted_count == requested_count:
            # All POSTs accepted; bounded post-read returned without
            # confirming every target absent. Distinguish "pending"
            # (verification just hasn't propagated yet) from "partial"
            # (some but not all confirmed absent).
            verification_status = (
                "partial"
                if 0 < verified_canceled_count < requested_count
                else "pending"
            )
        else:
            verification_status = "mismatch"

        # cancellation_status: tracks submission outcome
        # ("submitted"/"complete"/"partial"/"failed"/"mismatch").
        if verification_status == "complete":
            cancellation_status = "complete"
        elif verification_status == "failed":
            cancellation_status = "failed"
        elif verification_status == "mismatch":
            cancellation_status = "mismatch"
        elif verification_status == "partial":
            cancellation_status = "partial"
        else:
            # pending
            cancellation_status = "submitted"

        # partial_success: ONLY when actual partial completion is
        # evidenced. Never set when verification is pending.
        if verification_status == "partial":
            partial_success = True
        elif (
            stopped_at_index is not None
            and 0 < exchange_accepted_count < requested_count
        ):
            partial_success = True
        else:
            partial_success = False

        # Outer success reflects SUBMISSION, not verification. The
        # wizard renders verification_status separately. Mismatch
        # (counts could not be reconciled) is treated as a structural
        # failure that requires operator review, so outer success
        # is False in that case even though submission was clean.
        outer_success = (
            submission_success
            and verification_status not in {"failed", "mismatch"}
        )

        # error: only for submission-side failure or structural mismatch.
        # Never emit "N of M still open after submission" when every
        # POST was accepted — that case is "pending", not an error.
        if verification_status == "failed":
            err = (
                stopped_error
                or "no requested cancellation was accepted"
            )
        elif verification_status == "mismatch":
            err = (
                stopped_error
                or "cancellation result counts could not be reconciled"
            )
        elif verification_status == "partial" and stopped_at_index is not None:
            err = stopped_error
        else:
            err = None

        return _execution_result(
            request,
            success=outer_success,
            account=account, chain=chain,
            market_id=market_id_int,
            symbol=group_symbol,
            side=group_side,
            cancellation_status=cancellation_status,
            status=cancellation_status,
            verification_status=verification_status,
            requested_count=requested_count,
            matched_order_count=requested_count,
            matched_orders=per_order_results,
            submitted_count=submitted_count,
            exchange_accepted_count=exchange_accepted_count,
            verified_canceled_count=verified_canceled_count,
            remaining_target_count=remaining_target_count,
            # ---- Chunked-cancellation diagnostics (operator's spec). ----
            # chunks_submitted: total chunks attempted (regardless of
            #                   success/failure).
            # chunks_succeeded: chunks where every POST was
            #                   exchange-accepted.
            # chunks_failed:    chunks where at least one POST failed.
            # canceled_count:   total cancellations accepted by the
            #                   exchange across all successful chunks
            #                   (= exchange_accepted_count).
            # remaining_count:  orders NOT attempted (stop-on-first
            #                   failure halted subsequent chunks).
            # child_results:    full per-order result list including
            #                   which chunk each child belonged to.
            # remaining_orders_not_attempted: order_index values that
            #                   were never POSTed (must retry).
            chunks_submitted=chunks_submitted,
            chunks_succeeded=chunks_succeeded,
            chunks_failed=chunks_failed,
            canceled_count=exchange_accepted_count,
            remaining_count=len(remaining_orders_not_attempted),
            child_results=per_order_results,
            remaining_orders_not_attempted=remaining_orders_not_attempted,
            failed_chunk_index=failed_chunk_index,
            failed_chunk_error=failed_chunk_error,
            chunk_size=chunk_size,
            stopped_at_index=stopped_at_index,
            stopped_error=stopped_error,
            error=err,
            ambiguous=False,
            partial_success=partial_success,
            exchange_response={
                "submit": [
                    {
                        "market_index": r["market_index"],
                        "order_index": r["order_index"],
                        "chunk_index": r.get("chunk_index"),
                        "tx_hash": r.get("tx_hash"),
                        "cancellation_status": r.get(
                            "cancellation_status"
                        ),
                    }
                    for r in per_order_results
                ],
                "verification": {
                    "verified_canceled_count": verified_canceled_count,
                    "remaining_target_count": remaining_target_count,
                },
                "chunks": {
                    "submitted": chunks_submitted,
                    "succeeded": chunks_succeeded,
                    "failed": chunks_failed,
                    "size": chunk_size,
                    "failed_chunk_index": failed_chunk_index,
                    "failed_chunk_error": failed_chunk_error,
                },
                "remaining_orders_not_attempted": remaining_orders_not_attempted,
            },
        )

    def _cancel_order_group_verify(
        self,
        *,
        client: Any,
        target_order_indices: list[int],
    ) -> tuple[int, int]:
        """Bounded GET-only verification of a group cancellation.

        Polls /api/v1/accountActiveOrders up to
        ``VERIFICATION_MAX_READS`` times within
        ``VERIFICATION_MAX_WALL_TIME_S`` seconds. Returns
        ``(verified_canceled_count, remaining_target_count)``.

        The loop short-circuits as soon as the remaining set is empty
        (best case), when the cap is reached, when the wall-time
        budget is exhausted, or — conservatively — when the previous
        read showed the same open-orders list as the current read
        AND we have already read at least twice. The "no progress"
        exit guards against pathological cases where the
        ``accountActiveOrders`` endpoint does not reflect the
        cancellation on a tolerable time-scale; in such cases we
        return whatever verified count we have so far, with the
        remaining count populated from the still-present targets.

        On any GET error the loop stops immediately and reports the
        verified count as whatever was confirmed up to that point.
        """
        if not target_order_indices:
            return 0, 0
        targets: set[int] = set(int(t) for t in target_order_indices)
        verified: set[int] = set()
        try:
            from decimal import Decimal
        except ImportError:  # pragma: no cover
            Decimal = None  # type: ignore
        deadline = time.monotonic() + VERIFICATION_MAX_WALL_TIME_S
        sleep_ms = VERIFICATION_MIN_SLEEP_MS
        reads_performed = 0
        prev_current_indices: Optional[set[int]] = None
        no_progress_streak = 0
        for _ in range(VERIFICATION_MAX_READS):
            if time.monotonic() >= deadline:
                break
            try:
                payload = client.account_active_orders(
                    # The HTTP client signature is (account_index, ...)
                    # but at the dispatcher level we always have
                    # the chain/account_index from the caller; the
                    # HTTP client here is LighterHttpClient which
                    # already carries its own account_index.
                    # Pass 0 to force "all markets" semantics — the
                    # client will use its configured account_index.
                    0,
                )
            except Exception:
                # GET failure: stop conservatively. Report what we
                # have so far.
                break
            reads_performed += 1
            current_indices: set[int] = set()
            for o in (payload.get("orders") or []):
                if not isinstance(o, Mapping):
                    continue
                try:
                    oi = int(o.get("order_index") or 0)
                except (TypeError, ValueError):
                    continue
                if oi > 0:
                    current_indices.add(oi)            # Anything in our targets that's absent from the live
            # accountActiveOrders is verified cancelled.
            for tgt in list(targets):
                if tgt in verified:
                    continue
                if tgt not in current_indices:
                    verified.add(tgt)
            if targets.issubset(verified):
                break
            # Conservative no-progress detector: if two consecutive
            # reads returned the same open-orders list, the server is
            # not propagating our cancellations on the current
            # time-scale. Stop and report partial state rather than
            # burning the full budget.
            if (
                prev_current_indices is not None
                and prev_current_indices == current_indices
                and reads_performed >= 2
            ):
                no_progress_streak += 1
                if no_progress_streak >= 2:
                    # Two consecutive identical reads after at
                    # least one retry: bail conservatively.
                    break
            else:
                no_progress_streak = 0
            prev_current_indices = current_indices
            remaining = max(int(time.monotonic() + VERIFICATION_MAX_WALL_TIME_S - deadline), 0)
            if remaining <= 0:
                break
            sleep_ms = min(sleep_ms * 2, VERIFICATION_MAX_SLEEP_MS, remaining * 1000)
            if sleep_ms <= 0:
                break
            time.sleep(sleep_ms / 1000.0)
        verified_canceled = len(targets & verified)
        remaining = max(len(targets) - verified_canceled, 0)
        return verified_canceled, remaining

    # -- bounded post-read verification (operator-invoked) -----------

    def _run_bounded_post_read(
        self,
        *,
        client_order_index: int,
        chain: str,
        account: str,
        max_reads: int = VERIFICATION_MAX_READS,
        max_wall_time_s: int = VERIFICATION_MAX_WALL_TIME_S,
        min_sleep_ms: int = VERIFICATION_MIN_SLEEP_MS,
        max_sleep_ms: int = VERIFICATION_MAX_SLEEP_MS,
    ) -> dict:
        """Bounded GET-only post-read verification.

        Called by the operator's verification script AFTER a single
        ``place_order`` submission. We poll the Phase 2B open-orders
        endpoint (read-only) for the ``client_order_index`` we
        submitted. Stop conditions (whichever fires first):

          - row found   -> ``verification_status="confirmed_open"``
          - max_reads   -> ``verification_status="unconfirmed"``
          - max_wall_time_s -> ``verification_status="unconfirmed"``

        No verification outcome may trigger another submission.

        Note: we do NOT acquire the nonce lock here; this is a GET-only
        loop that runs after the nonce lock has been released by the
        caller (the dispatcher releases the lock at the end of the
        locked ``with`` block).
        """
        import time as _time
        deadline = _time.monotonic() + max_wall_time_s
        sleep_ms = min_sleep_ms
        reads_performed = 0
        # Build a request dict compatible with the open_orders
        # dispatcher. We use a synthetic account request and let the
        # Phase 2B read return whatever the server has for that account.
        # This avoids forcing Phase 2B to know about a verify flag.
        try:
            creds = _resolve_account_credentials(account)
        except ValueError as exc:
            return {
                "verification_status": "unconfirmed",
                "ambiguous": True,
                "verified_at": None,
                "error": str(exc),
                "reads_performed": 0,
                "wall_time_s": 0.0,
                "final_sleep_ms": 0,
            }
        base_url = _get_chain_config(creds["chain"])[1]
        client = self._http_client_for_chain(creds["chain"], base_url, creds)

        for attempt in range(max_reads):
            if _time.monotonic() >= deadline:
                break
            try:
                # The Phase 2B dispatcher's payload:
                payload = {
                    "code": 200,
                    "next_cursor": None,
                    "orders": client.account_active_orders(
                        int(creds["account_index"])
                    ).get("orders") or [],
                }
            except LighterHTTPError as exc:
                return {
                    "verification_status": "unconfirmed",
                    "ambiguous": True,
                    "verified_at": None,
                    "error": str(exc),
                    "reads_performed": reads_performed,
                    "wall_time_s": max_wall_time_s - (deadline - _time.monotonic()),
                    "final_sleep_ms": sleep_ms,
                }
            except Exception as exc:
                return {
                    "verification_status": "unconfirmed",
                    "ambiguous": True,
                    "verified_at": None,
                    "error": (
                        f"Lighter accountActiveOrders failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "reads_performed": reads_performed,
                    "wall_time_s": max_wall_time_s - (deadline - _time.monotonic()),
                    "final_sleep_ms": sleep_ms,
                }
            reads_performed = attempt + 1

            for order in (payload.get("orders") or []):
                if not isinstance(order, Mapping):
                    continue
                try:
                    coi_int = int(order.get("client_order_index") or 0)
                except (TypeError, ValueError):
                    continue
                if coi_int == int(client_order_index):
                    # Match found. Build a verified envelope.
                    return {
                        "verification_status": "confirmed_open",
                        "ambiguous": False,
                        "verified_at": _time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", _time.gmtime()
                        ),
                        "verified_order_id": str(order.get("order_id") or ""),
                        "verified_order_index": str(
                            int(order.get("order_index") or 0)
                        ),
                        "verified_market_index": str(
                            int(order.get("market_index") or 0)
                        ),
                        "verified_status": str(order.get("status") or ""),
                        "verified_initial_size": str(
                            order.get("initial_base_amount") or "0"
                        ),
                        "verified_remaining_size": str(
                            order.get("remaining_base_amount") or "0"
                        ),
                        "verified_time_in_force": str(
                            order.get("time_in_force") or ""
                        ),
                        "verified_is_ask": bool(order.get("is_ask")),
                        "verified_price_wire": str(
                            order.get("base_price") or "0"
                        ),
                        "reads_performed": reads_performed,
                        "wall_time_s": max_wall_time_s - (deadline - _time.monotonic()),
                        "final_sleep_ms": sleep_ms,
                    }
            # No match in this read. Sleep with bounded backoff.
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            actual_sleep_ms = min(
                sleep_ms,
                max_sleep_ms,
                int(remaining * 1000),
            )
            if actual_sleep_ms <= 0:
                break
            _time.sleep(actual_sleep_ms / 1000.0)
            sleep_ms = min(sleep_ms * 2, max_sleep_ms)

        return {
            "verification_status": "unconfirmed",
            "ambiguous": True,
            "verified_at": _time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", _time.gmtime()
            ),
            "error": "order not found in bounded post-read window",
            "reads_performed": reads_performed,
            "wall_time_s": max_wall_time_s - max(0.0, deadline - _time.monotonic()),
            "final_sleep_ms": sleep_ms,
        }

    # -- HTTP client cache -----------------------------------------------

    def _http_client_for_chain(self, chain: str, base_url: str,
                              creds: Mapping[str, Any]) -> Any:
        """Return (and cache) a per-chain HTTP client.

        ``self._injected_http_client`` (test seam) is honored if
        present. Production code path constructs a real
        ``LighterHttpClient`` keyed by the canonical chain name.
        """
        if self._injected_http_client is not None:
            return self._injected_http_client
        cached = self._http_clients.get(chain)
        if cached is not None:
            return cached
        client = LighterHttpClient(
            base_url=base_url,
            account_index=creds["account_index"],
            api_key_index=creds["apikey_index"],
            api_private_key=creds["private_key"],
            public_key=creds["public_key"],
        )
        self._http_clients[chain] = client
        return client
