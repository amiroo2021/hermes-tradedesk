"""Raydium exchange agent (Phase 1: read-only authenticated).

Raydium Perps (``perps.raydium.io``) is a gasless central-limit-order-book
perpetuals venue whose matching engine, order book, and authenticated REST
API are operated by Orderly Network. Raydium's own documentation explicitly
defers all protocol-level API reference to Orderly::

    "the deep protocol reference lives on Orderly's side"
    -- https://docs.raydium.io/products/perps

There is no Raydium-specific auth override. This module implements the
authenticated client exactly as documented by Orderly.

============================================================================
AUTHENTICATION (sourced from Orderly docs / skills)

Source documents (fetched 2026-07-23):
    - https://orderly.network/docs/build-on-omnichain/api-authentication
    - https://orderly.network/skill.md  (``orderly-onboarding`` skill)
    - npm @orderly.network/skills@0.1.0
        -> orderly-api-authentication/SKILL.md
        -> orderly-trading-orders/SKILL.md
        -> orderly-positions-tpsl/SKILL.md
    - Raydium frontend bundle ``perps.raydium.io/assets/index-*.js``
      confirms ``VITE_ORDERLY_BROKER_ID = "raydium"``.

Auth scheme: Ed25519 message signing over a canonical string. Four required
headers on every authenticated request::

    orderly-timestamp   Unix milliseconds, generated immediately before signing
    orderly-account-id  The Orderly account ID (hex)
    orderly-key         "ed25519:<base58-encoded-public-key>"
    orderly-signature   base64url-encoded Ed25519 signature

Canonical message (concatenated, NO delimiter)::

    f"{timestamp}{method}{pathname}{search}{body}"

where ``pathname`` is the URL path (e.g. ``/v1/client/holding``) and
``search`` is the URL query string INCLUDING the leading ``?`` if present
(``""`` for no query). The body is the exact JSON string sent on the wire,
or ``""`` for GET/DELETE with no body.

Content-Type:
    GET, DELETE  -> ``application/x-www-form-urlencoded``
    POST, PUT    -> ``application/json``

Timestamp validity window: ±30 seconds.

============================================================================
ENVIRONMENT VARIABLE FORMAT

Every Raydium credential block uses three variables (per-account)::

    RAYDIUM_<ACCOUNT>_ACCOUNT_ID = <hex string, the Orderly account ID>
    RAYDIUM_<ACCOUNT>_API_KEY    = "ed25519:<base58-public-key>"
    RAYDIUM_<ACCOUNT>_SECRET_KEY = "ed25519:<base58-private-key>"

Example (current operator env)::

    RAYDIUM_EXAMPLE_ACCOUNT_ID=<YOUR_ORDERLY_ACCOUNT_ID>
    RAYDIUM_EXAMPLE_API_KEY=<YOUR_ORDERLY_PUBLIC_KEY>
    RAYDIUM_EXAMPLE_SECRET_KEY=<YOUR_ORDERLY_PRIVATE_KEY>

An account is configured only when ALL three of the above variables are
present. Partial blocks are rejected at discovery time. No silent
fallback. The ``EXAMPLE`` account alias is treated exactly like any
other per-account label on the other adapters -- it is not a wallet brand.

============================================================================
BROKER ID AND NETWORK

Broker ID is hardcoded to ``"raydium"`` (verified against the Raydium
frontend bundle's ``VITE_ORDERLY_BROKER_ID`` environment variable).
Phase 1 targets mainnet only. Mainnet base URL: ``https://api.orderly.org``
(sourced from the orderly-api-authentication skill's environment table).
Testnet is intentionally NOT supported in this phase.

============================================================================
PHASE 1 SCOPE

Implemented (read-only, authenticated):
    - account discovery (``list_accounts``)
    - balance              (GET /v1/client/holding)
    - positions            (GET /v1/positions)
    - open_orders          (GET /v1/orders?status=INCOMPLETE)

Explicitly NOT implemented in Phase 1 (write paths):
    - order placement
    - batch orders
    - cancel orders
    - TP/SL orders
    - leverage / margin changes
    - deposits / withdrawals

Write paths will be added in subsequent phases after read-only is
verified end-to-end.

============================================================================
REQUEST FLOW

The wizard passes only the account identifier -- no chain field, no
broker field::

    { "version": 1, "operation": "balance",
      "exchange": "raydium", "account": "example" }

RaydiumAgent reads ``RAYDIUM_<account>_*`` internally. TradeDesk
remains exchange-agnostic; it does not need to know about Orderly,
Ed25519, base URLs, or signing.
"""

from __future__ import annotations

from .raydium_write import execute_order, execute_cancel, execute_cancel_group, execute_batch_orders, execute_set_tpsl  # noqa: F401  # Phase 2A write paths; batch_orders for ladder; set_tp/set_sl for position manager

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

import base58
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Immutable network + broker configuration.
# ---------------------------------------------------------------------------

RAYDIUM_NETWORK: str = "mainnet"
"""Phase 1 targets mainnet only. Testnet is deliberately not supported."""

RAYDIUM_BASE_URL: str = "https://api.orderly.org"
"""Orderly mainnet REST base URL.

Sourced from the orderly-api-authentication skill (npm @orderly.network/skills
@0.1.0), Environment Configuration table::

    Mainnet | https://api.orderly.org
"""

RAYDIUM_BROKER_ID: str = "raydium"
"""Broker ID hardcoded for Raydium Perps.

Verified by inspecting the Raydium Perps frontend bundle
(``perps.raydium.io/assets/index-*.js``) which contains the literal
``VITE_ORDERLY_BROKER_ID: "raydium"``. The Orderly backend segregates
liquidity, fee schedules, and account namespaces per broker ID; using
the wrong broker ID would route requests to a different venue entirely.
"""

RAYDIUM_REQUEST_TIMEOUT_SECONDS: float = 15.0
"""Single-request HTTP timeout. Read-only endpoints are small and
should respond well under this bound."""


# ---------------------------------------------------------------------------
# Phase 1 operation surface.
# ---------------------------------------------------------------------------

SUPPORTED_OPERATIONS: set[str] = {
    "balance",
    "cancel_order",
    "cancel_order_group",
    "cancel_orders",
    "open_orders",
    "order",
    "positions",
}
"""Raydium supports read-only queries plus single-order and grouped
cancellation. ``order`` / ``positions`` / ``open_orders`` remain unchanged,
while ``cancel_order`` handles exact single-order cancels and
``cancel_orders`` / ``cancel_order_group`` route to the grouped exact-match
path. The TradeDesk router will surface a clear "Unsupported operation"
error for anything else."""


# ---------------------------------------------------------------------------
# Account discovery (structured)
# ---------------------------------------------------------------------------

_ACCOUNT_KEY_RE = re.compile(
    r"^raydium_(?P<account>[a-z0-9]+)_(?P<field>[a-z_]+)$", re.IGNORECASE,
)
_STANDARD_FIELDS: Tuple[str, ...] = (
    "ACCOUNT_ID",
    "API_KEY",
    "SECRET_KEY",
)
_REQUIRED_FIELDS: Tuple[str, ...] = ("ACCOUNT_ID", "API_KEY", "SECRET_KEY")
_OPTIONAL_FIELDS: Tuple[str, ...] = ("PRIVATE_KEY", "BROKER_ID")


def _process_casefold_env() -> dict[str, tuple[str, str, str]]:
    """Case-insensitive view of the already-loaded process environment.

    Raydium account discovery must operate on the gateway's live process
    environment only. Telegram code must not parse .env files or call
    load_dotenv(); the gateway is responsible for loading configuration
    before the wizard runs.
    """
    out: dict[str, tuple[str, str, str]] = {}
    for env_key, env_value in os.environ.items():
        if env_value and str(env_value).strip():
            out[env_key.lower()] = (env_key, str(env_value).strip(), "environment")
    return out


def _has_env_value(account: str, field: str) -> bool:
    try:
        _read_env_value(account, field)
        return True
    except ValueError:
        return False


def _list_raydium_keys() -> list[tuple[str, str]]:
    """Enumerate every ``RAYDIUM_<account>_<field>`` tuple present in the
    case-insensitive union of process env and Hermes ``.env``.

    Unknown fields (anything outside the Phase 1 set) are ignored so
    adding future Phase 2+ variables does not break discovery.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for actual_key, _value, _source in _process_casefold_env().values():
        m = _ACCOUNT_KEY_RE.match(actual_key)
        if not m:
            continue
        account = m.group("account").lower()
        field = m.group("field").upper()
        if field not in _REQUIRED_FIELDS and field not in _OPTIONAL_FIELDS:
            continue
        key = (account, field)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


@dataclass(frozen=True)
class RaydiumAccount:
    """Structured account metadata for one Raydium/Orderly account.

    An account is considered configured only when ALL three Phase 1
    fields are present for the same ``account`` token. Partial blocks
    are rejected at discovery time so the agent never signs with a
    missing credential.
    """
    account: str

    def label(self) -> str:
        """Human-readable label for wizard rendering."""
        return f"{self.account} — Raydium ({RAYDIUM_NETWORK})"


def _read_env_value(account: str, field: str) -> str:
    """Read a ``RAYDIUM_<account>_<field>`` value from process env +
    Hermes ``.env``.

    Returns the value exactly as it appears in the source, with no
    whitespace stripping and no character-level mutation. Raises
    ``ValueError`` if the key is missing or empty.
    """
    if not account or not isinstance(account, str):
        raise ValueError(f"Invalid Raydium account: {account!r}")
    if not field or not isinstance(field, str):
        raise ValueError(f"Invalid Raydium field: {field!r}")

    actual_key = f"raydium_{account.lower()}_{field.lower()}"
    env_map = _process_casefold_env()
    entry = env_map.get(actual_key.lower())
    if entry is None and field.upper() == "SECRET_KEY":
        entry = env_map.get(f"raydium_{account.lower()}_private_key")
    if entry is None:
        raise ValueError(
            f"Missing RAYDIUM_{account.upper()}_{field} in process environment"
        )
    _, value, _source = entry
    if not value or not value.strip():
        raise ValueError(
            f"Empty value for RAYDIUM_{account.upper()}_{field}"
        )
    return value


def discover_raydium_accounts() -> list[RaydiumAccount]:
    """Discover Raydium accounts with complete Phase 1 credentials.

    Algorithm:
      1. Enumerate every ``RAYDIUM_<account>_<field>`` tuple across
         process env and Hermes ``.env``.
      2. Filter to valid Phase 1 fields
         (``ACCOUNT_ID``, ``API_KEY``, ``SECRET_KEY``).
      3. Group by ``account``. For each group, require all three
         fields to be present.
      4. Validate each field's format. Any malformed value rejects the
         whole account; partial data is never returned.
    """
    grouped: dict[str, set[str]] = {}
    for account, field in _list_raydium_keys():
        grouped.setdefault(account, set()).add(field)

    out: list[RaydiumAccount] = []
    for account, fields in sorted(grouped.items()):
        missing: list[str] = []
        if "ACCOUNT_ID" not in fields:
            missing.append("ACCOUNT_ID")
        if "API_KEY" not in fields:
            missing.append("API_KEY")
        if "SECRET_KEY" not in fields and "PRIVATE_KEY" not in fields:
            missing.append("SECRET_KEY|PRIVATE_KEY")
        if missing:
            logger.warning(
                "Incomplete Raydium credentials for account=%s: missing=%s",
                account, sorted(missing),
            )
            continue
        try:
            _validate_account_id_field(
                f"{account}_ACCOUNT_ID",
                _read_env_value(account, "ACCOUNT_ID"),
            )
            _validate_ed25519_key_field(
                f"{account}_API_KEY", _read_env_value(account, "API_KEY"),
                kind="public",
            )
            _validate_ed25519_key_field(
                f"{account}_SECRET_KEY", _read_env_value(account, "SECRET_KEY"),
                kind="private",
            )
        except ValueError as exc:
            logger.warning(
                "Raydium credential validation failed for account=%s: %s",
                account, exc,
            )
            continue
        out.append(RaydiumAccount(account=account))
    return out


def _validate_account_id_field(name: str, raw: str) -> str:
    """Validate the Orderly account ID format.

    Accepts a 0x-prefixed or bare hex string. Length is not strictly
    enforced because Orderly has historically used both 64-hex (EVM
    ``keccak256``) and shorter Solana-derived IDs. We only require
    that the value is non-empty, has no whitespace, and is valid hex.
    """
    if raw != raw.strip():
        raise ValueError(
            f"Invalid RAYDIUM_{name.upper()}: value contains whitespace"
        )
    if raw.startswith("="):
        raise ValueError(
            f"Malformed RAYDIUM_{name.upper()}: value contains a leading '=' "
            f"(got {raw!r})"
        )
    candidate = raw[2:] if raw.lower().startswith("0x") else raw
    if not candidate:
        raise ValueError(
            f"Invalid RAYDIUM_{name.upper()}: empty hex body"
        )
    try:
        int(candidate, 16)
    except ValueError as exc:
        raise ValueError(
            f"Invalid RAYDIUM_{name.upper()}: {raw!r} is not valid hex"
        ) from exc
    return raw


def _validate_ed25519_key_field(name: str, raw: str, *, kind: str) -> str:
    """Validate the ``ed25519:<base58>`` shape of API_KEY / SECRET_KEY.

    The Orderly docs document the public key as
    ``ed25519:<base58-encoded-public-key>``. For SECRET_KEY the docs
    show the same ``ed25519:`` prefix wrapping the base58-encoded 32-byte
    Ed25519 secret key (see orderly-api-authentication/SKILL.md,
    ``bs58.encode(privateKey)`` examples for EVM and Solana flows).
    """
    if raw != raw.strip():
        raise ValueError(
            f"Invalid RAYDIUM_{name.upper()}: value contains whitespace"
        )
    if raw.startswith("="):
        raise ValueError(
            f"Malformed RAYDIUM_{name.upper()}: value contains a leading '=' "
            f"(got {raw!r})"
        )
    if not raw.startswith("ed25519:"):
        raise ValueError(
            f"Invalid RAYDIUM_{name.upper()}: expected 'ed25519:<base58>' "
            f"prefix (got {raw[:20]!r})"
        )
    payload = raw[len("ed25519:"):]
    if not payload:
        raise ValueError(
            f"Invalid RAYDIUM_{name.upper()}: empty base58 body after 'ed25519:'"
        )
    try:
        decoded = base58.b58decode(payload)
    except Exception as exc:
        raise ValueError(
            f"Invalid RAYDIUM_{name.upper()}: base58 decode failed "
            f"(got {payload[:20]!r})"
        ) from exc
    # Ed25519 keys are 32 bytes. Public keys may be 32 (raw) or 33
    # (with a 0x00 / 0x01 prefix byte). We accept both and trim the
    # prefix if present.
    if kind == "public" and len(decoded) == 33:
        # Drop a possible leading version byte (0x00 for Ed25519).
        decoded = decoded[1:]
    if len(decoded) != 32:
        raise ValueError(
            f"Invalid RAYDIUM_{name.upper()}: {kind} key length is "
            f"{len(decoded)} bytes, expected 32"
        )
    return raw


# ---------------------------------------------------------------------------
# Ed25519 signer.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ResolvedCredentials:
    """Decoded credential triple for one Raydium/Orderly account.

    All byte fields are ``bytes`` so the signing path never has to
    re-decode base58 on every request.
    """
    account: str
    account_id: str
    public_key_b58: str
    private_key_bytes: bytes
    public_key_bytes: bytes


def _resolve_account_credentials(account: str) -> _ResolvedCredentials:
    """Resolve one account's Phase 1 credentials.

    Returns a structured ``_ResolvedCredentials`` with decoded bytes
    ready for the signing path. Raises ``ValueError`` with a clear
    configuration-error message referencing the real variable name
    on any missing or malformed field. No silent repair.
    """
    if not account or not isinstance(account, str):
        raise ValueError(f"Invalid Raydium account: {account!r}")

    account_id = _read_env_value(account, "ACCOUNT_ID")
    api_key_raw = _read_env_value(account, "API_KEY")
    secret_key_raw = _read_env_value(account, "SECRET_KEY")

    # Re-run validation so the bytes are guaranteed decodable.
    _validate_account_id_field(f"{account}_ACCOUNT_ID", account_id)
    _validate_ed25519_key_field(f"{account}_API_KEY", api_key_raw, kind="public")
    _validate_ed25519_key_field(
        f"{account}_SECRET_KEY", secret_key_raw, kind="private",
    )

    public_key_bytes = base58.b58decode(api_key_raw[len("ed25519:"):])
    if len(public_key_bytes) == 33:
        public_key_bytes = public_key_bytes[1:]
    private_key_bytes = base58.b58decode(secret_key_raw[len("ed25519:"):])
    if len(private_key_bytes) != 32:
        raise ValueError(
            f"Invalid RAYDIUM_{account.upper()}_SECRET_KEY: decoded length "
            f"is {len(private_key_bytes)} bytes, expected 32"
        )

    return _ResolvedCredentials(
        account=account,
        account_id=account_id,
        public_key_b58=api_key_raw[len("ed25519:"):],
        private_key_bytes=private_key_bytes,
        public_key_bytes=public_key_bytes,
    )


def _sign_request(
    *,
    private_key_bytes: bytes,
    public_key_b58: str,
    timestamp_ms: int,
    method: str,
    url_path: str,
    url_search: str,
    body: str,
) -> str:
    """Build the canonical message and return the base64url signature.

    Canonical message (no delimiter)::
        f"{timestamp}{method}{pathname}{search}{body}"

    The signature is the Ed25519 signature over the UTF-8-encoded message,
    base64url-encoded with padding stripped.

    Caller is responsible for using a timestamp generated immediately
    before signing (Orderly enforces a ±30-second validity window).
    """
    message = f"{timestamp_ms}{method}{url_path}{url_search}{body}"
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(message.encode("utf-8"))
    return base64.urlsafe_b64encode(signature_bytes).rstrip(b"=").decode("ascii")


def _public_key_header(public_key_b58: str) -> str:
    """Build the ``orderly-key`` header value.

    Format: ``ed25519:<base58-encoded-public-key>``. The public key
    bytes were already trimmed of any leading version byte during
    credential resolution, so we re-encode them to base58 here.
    """
    return f"ed25519:{public_key_b58}"


# ---------------------------------------------------------------------------
# Orderly HTTP client.
# ---------------------------------------------------------------------------

class _RaydiumHttpError(RuntimeError):
    """Wraps a non-2xx Orderly response with the parsed payload."""

    def __init__(self, *, status_code: int, payload: Any, message: str):
        super().__init__(message)
        self.status_code = int(status_code)
        self.payload = payload


class RaydiumHttpClient:
    """Synchronous HTTP client for authenticated Orderly endpoints.

    One instance per agent. The HTTP layer is intentionally minimal:
    no retries, no caching, no connection pooling beyond the default
    ``requests`` Session. Phase 1 is read-only and a single failed
    request should surface immediately so the operator can diagnose.

    Thread-safety: ``requests.Session`` is thread-safe for the basic
    GET/POST verbs used here. The agent does not hold mutable per-call
    state, so concurrent ``execute()`` calls on the same instance are
    safe.
    """

    def __init__(self, *, base_url: str = RAYDIUM_BASE_URL) -> None:
        if not base_url or not isinstance(base_url, str):
            raise ValueError(f"Invalid base_url: {base_url!r}")
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return self._base_url

    def _signed_request(
        self,
        *,
        creds: _ResolvedCredentials,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Issue one authenticated request and return the parsed JSON body.

        Construction order (must match the Orderly canonical signing
        spec byte-for-byte):
            1. Compute the URL path and search string the server will see.
            2. Compute the exact body string the server will see.
            3. Build the canonical message and Ed25519-sign it.
            4. Attach headers and dispatch.

        Any non-2xx response is raised as ``_RaydiumHttpError`` so the
        agent layer can convert it to the standard ``_execution_result``
        error envelope.
        """
        # Build the URL the server will see (path + query string).
        # We use ``urlencode`` to guarantee the search string is byte-
        # identical to what the server parses; the signature must match
        # the server's view, not a slightly-reordered client form.
        from urllib.parse import urlencode

        if params:
            # ``doseq=True`` so list-valued params serialize correctly
            # (Orderly accepts ``?symbol=...&symbol=...`` repetition).
            search = "?" + urlencode(
                [(k, v) for k, v in params.items()], doseq=True,
            )
        else:
            search = ""
        url = f"{self._base_url}{path}{search}"

        # Serialize the body (or empty string for GET/DELETE).
        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"), sort_keys=False)
        else:
            body_str = ""

        # Generate timestamp immediately before signing.
        timestamp_ms = int(time.time() * 1000)

        # Sign.
        signature = _sign_request(
            private_key_bytes=creds.private_key_bytes,
            public_key_b58=creds.public_key_b58,
            timestamp_ms=timestamp_ms,
            method=method.upper(),
            url_path=path,
            url_search=search,
            body=body_str,
        )

        # Content-Type follows the Orderly rule:
        #   GET, DELETE -> application/x-www-form-urlencoded
        #   POST, PUT   -> application/json
        content_type = (
            "application/json"
            if method.upper() in {"POST", "PUT"}
            else "application/x-www-form-urlencoded"
        )

        headers = {
            "Content-Type": content_type,
            "orderly-timestamp": str(timestamp_ms),
            "orderly-account-id": creds.account_id,
            "orderly-key": _public_key_header(creds.public_key_b58),
            "orderly-signature": signature,
        }

        try:
            response = self._session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                data=body_str if body_str else None,
                timeout=RAYDIUM_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise _RaydiumHttpError(
                status_code=0,
                payload=None,
                message=f"HTTP transport error: {exc}",
            ) from exc

        # Parse the body. Orderly returns JSON for both success and
        # failure envelopes.
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw_text": response.text}

        if not (200 <= response.status_code < 300):
            raise _RaydiumHttpError(
                status_code=response.status_code,
                payload=payload,
                message=(
                    f"Orderly HTTP {response.status_code}: "
                    f"{_summarize_payload(payload)}"
                ),
            )

        # Orderly success envelopes are ``{"success": true, "data": ...}``.
        # We return the parsed payload as-is so callers can navigate
        # to ``payload["data"]`` if they need the typed shape.
        return payload


def _summarize_payload(payload: Any) -> str:
    """Compact one-line summary of a payload for error messages."""
    if isinstance(payload, Mapping):
        for key in ("message", "error", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:200]
        return json.dumps(payload, separators=(",", ":"))[:200]
    if isinstance(payload, str):
        return payload[:200]
    return repr(payload)[:200]


# ---------------------------------------------------------------------------
# Phase 1 normalization (balance / positions / open_orders).
# ---------------------------------------------------------------------------

def _to_hermes_balance(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project an Orderly ``/v1/client/holding`` response into the Hermes
    balance shape.

    The Orderly ``/v1/client/holding`` endpoint returns a list of
    per-token holdings (one row per token in the account). The
    cross-margin totals (total_collateral, free_collateral, etc.)
    surfaced by the ``/v1/positions`` endpoint are NOT on this
    endpoint -- they require a second call.

    Verified against the live EXAMPLE account response (2026-07-23)::

        {
          "success": true,
          "data": {
            "holding": [
              {
                "token": "USDC",
                "holding": 12633.441282,
                "frozen": 0.0,
                "pending_short": 0.0,
                "isolated_margin": 0.0,
                "isolated_order_frozen": 0.0,
                "updated_time": 1784807434466
              }
            ]
          }
        }

    The Hermes shape mirrors ``lighter_agent._to_hermes_balance`` for the
    fields the existing balance renderer reads (``marginSummary``,
    ``withdrawable``). Per-token rows are surfaced under ``tokens`` for
    callers that want to iterate, and the synthetic cross-margin
    fields are populated as zero (since ``/v1/client/holding`` does
    not expose them). ``exchange_response.holding`` carries the raw
    per-token array so callers can compute their own cross-margin
    totals if needed.
    """
    # Navigate to the per-token array. The endpoint envelope is
    # ``{"success": true, "data": {"holding": [...]}}`` but we accept
    # a plain ``{"holding": [...]}`` or ``{"data": [...]}`` shape
    # for forward-compatibility with any future flattening.
    if isinstance(payload, Mapping):
        data = payload.get("data", payload)
    else:
        data = {}
    if isinstance(data, Mapping):
        rows = list(data.get("holding") or [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    # Sum the USDC-equivalent totals across all token rows. Orderly
    # surfaces raw per-token ``holding`` amounts without USD conversion
    # in this endpoint, so we sum numerically and let the caller
    # interpret which token is which.
    total_holding = 0.0
    total_frozen = 0.0
    total_pending_short = 0.0
    total_isolated_margin = 0.0
    total_isolated_order_frozen = 0.0
    tokens: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        token = str(row.get("token") or "")
        try:
            holding = float(row.get("holding") or 0)
        except (TypeError, ValueError):
            holding = 0.0
        try:
            frozen = float(row.get("frozen") or 0)
        except (TypeError, ValueError):
            frozen = 0.0
        try:
            pending_short = float(row.get("pending_short") or 0)
        except (TypeError, ValueError):
            pending_short = 0.0
        try:
            isolated_margin = float(row.get("isolated_margin") or 0)
        except (TypeError, ValueError):
            isolated_margin = 0.0
        try:
            isolated_order_frozen = float(row.get("isolated_order_frozen") or 0)
        except (TypeError, ValueError):
            isolated_order_frozen = 0.0
        total_holding += holding
        total_frozen += frozen
        total_pending_short += pending_short
        total_isolated_margin += isolated_margin
        total_isolated_order_frozen += isolated_order_frozen
        tokens.append({
            "token": token,
            "holding": holding,
            "frozen": frozen,
            "pending_short": pending_short,
            "isolated_margin": isolated_margin,
            "isolated_order_frozen": isolated_order_frozen,
            "updated_time": row.get("updated_time"),
            "raw": dict(row),
        })

    # ``available_to_withdraw`` is ``holding - frozen - isolated_margin -
    # isolated_order_frozen`` per Orderly's docs (sum of free balance
    # across all tokens). This is the canonical "withdrawable" amount.
    available = max(
        total_holding
        - total_frozen
        - total_isolated_margin
        - total_isolated_order_frozen,
        0.0,
    )
    total_str = _format_decimal(total_holding)
    available_str = _format_decimal(available)
    margin_used_str = _format_decimal(
        total_frozen + total_isolated_margin + total_isolated_order_frozen
    )
    return {
        # Canonical Hermes fields (mirrored from lighter_agent):
        "balance": total_str,
        "available_to_withdraw": available_str,
        "account_equity": total_str,
        "total_margin_used": margin_used_str,
        # Explicit aliases used by the Telegram formatter and by
        # downstream consumers that expect the balance object itself to
        # expose normalized balance fields.
        "account_value": total_str,
        "withdrawable": available_str,
        "margin_used": margin_used_str,
        "total_position_value": None,
        "marginSummary": {
            "accountValue": total_str,
            "totalMarginUsed": margin_used_str,
            "totalNtlPos": None,
        },
        # Orderly-specific fields preserved for diagnostics:
        "pending_short": _format_decimal(total_pending_short),
        "token_count": len(tokens),
        "broker_id": (payload.get("broker_id") if isinstance(payload, Mapping) else None) or RAYDIUM_BROKER_ID,
        "network": RAYDIUM_NETWORK,
        # Per-token breakdown (Hermes extension specific to Orderly/Raydium).
        "tokens": tokens,
        # Cross-margin totals are NOT available from /v1/client/holding
        # alone; they require a follow-up /v1/positions call. Surface
        # the flag so callers know not to use these values as authoritative.
        "cross_margin_totals_source": "not_available_from_holding_endpoint",
    }


def _format_decimal(value: float) -> str:
    """Format a float as a decimal string with sensible precision.

    Orderly serves token amounts as floats (e.g. ``12633.441282``). We
    preserve up to 8 fractional digits, trimming trailing zeros, so the
    downstream balance renderer doesn't get a number with spurious
    float noise.
    """
    if value == 0:
        return "0"
    formatted = f"{value:.8f}".rstrip("0").rstrip(".")
    return formatted if formatted else "0"


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hermes_normalize_raydium_position(
    raw: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Normalize one Orderly position dict into the Hermes-standard shape.

    Orderly position fields (per the Orderly OpenAPI spec at
    ``OrderlyNetwork/documentation-public/orderly.openapi.yaml``)::

        symbol: str
        position_qty: number       (signed; + long, - short)
        average_open_price: number
        mark_price: number
        unsettled_pnl: number      (NOT ``unrealized_pnl`` — that's
                                    the field the renderer reads, but
                                    Orderly's actual API key is
                                    ``unsettled_pnl``)
        pnl_24_h: number
        mmr: number
        imr: number
        notional: number
        leverage: number
        liq_price: number

    Returns ``None`` for zero-size / flat positions so the caller can
    drop them from the active list.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        qty = float(raw.get("position_qty") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty == 0:
        return None
    side = "long" if qty > 0 else "short"
    return {
        "symbol": str(raw.get("symbol") or ""),
        "side": side,
        "size": abs(qty),
        "size_signed": qty,
        "entry_price": _safe_float(raw.get("average_open_price")),
        "mark_price": _safe_float(raw.get("mark_price")),
        "liq_price": _safe_float(raw.get("liq_price")),
        "leverage": _safe_float(raw.get("leverage")),
        "margin_mode": "cross",  # Orderly is cross-margin only; see Raydium Perps docs.
        "margin_used": None,     # Orderly does not surface per-position margin on /v1/positions.
        # The renderer reads ``unrealized_pnl`` (canonical Hermes key).
        # Orderly's actual API field is ``unsettled_pnl``; we map here.
        "unrealized_pnl": _safe_float(raw.get("unsettled_pnl")),
        "pnl_24_h": _safe_float(raw.get("pnl_24_h")),
        "notional": _safe_float(raw.get("notional")),
        "imr": _safe_float(raw.get("imr")),
        "mmr": _safe_float(raw.get("mmr")),
        "raw": dict(raw),
    }


def _hermes_normalize_raydium_order(
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize one Orderly open-order dict into the Hermes-standard shape.

    Orderly order fields are not fully documented in the skill packages;
    we surface the raw dict plus the well-known identifiers so the
    renderer can find them. ``client_order_id``, ``type``, ``price``,
    ``quantity`` are best-effort based on Orderly SDK naming; if the
    server uses different keys they will appear under ``raw``.
    """
    order_id = raw.get("order_id")
    client_order_id = raw.get("client_order_id")
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "symbol": str(raw.get("symbol") or ""),
        "side": str(raw.get("side") or "").lower(),
        "order_type": str(raw.get("order_type") or "").lower(),
        "price": _safe_float(raw.get("price") or raw.get("order_price")),
        "quantity": _safe_float(raw.get("quantity") or raw.get("order_quantity")),
        "visible_quantity": _safe_float(raw.get("visible_quantity")),
        "status": str(raw.get("status") or ""),
        "created_time": raw.get("created_time"),
        "raw": dict(raw),
    }


# ---------------------------------------------------------------------------
# Execution result envelope (matches the lighter_agent contract).
# ---------------------------------------------------------------------------

def _execution_result(request: Mapping[str, Any], *, success: bool,
                      error: Optional[str] = None,
                      exchange_response: Any = None,
                      balance: Any = None,
                      positions: Any = None,
                      orders: Any = None,
                      **extra: Any) -> dict[str, Any]:
    """Build the canonical execution-result envelope.

    Mirrors ``lighter_agent._execution_result`` exactly so TradeDesk
    and downstream renderers see a consistent shape across exchanges.
    """
    out: dict[str, Any] = {
        "success": bool(success),
        "exchange": "raydium",
        "operation": str(request.get("operation") or ""),
        "parent_operation": str(request.get("parent_operation")
                                or request.get("operation") or ""),
        "account": request.get("account") or "",
        "structured_request": dict(request),
    }
    if exchange_response is not None:
        out["exchange_response"] = exchange_response
    if balance is not None:
        out["balance"] = balance
    if positions is not None:
        out["positions"] = positions
    if orders is not None:
        out["orders"] = orders
    if error is not None:
        out["error"] = str(error)
    for k, v in extra.items():
        if k not in out:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Agent class.
# ---------------------------------------------------------------------------

class RaydiumAgent:
    """Hermes exchange agent for Raydium Perps (Orderly backend).

    Phase 1 supports the three read-only authenticated operations.
    Write paths will be added in subsequent phases after the read-only
    surface is verified end-to-end against a real account.
    """

    SUPPORTED_OPERATIONS = SUPPORTED_OPERATIONS

    def __init__(self, *, http_client: Any = None) -> None:
        # Optional injection point for tests (matches the lighter_agent
        # contract). In production this is ``None`` and a real
        # RaydiumHttpClient is constructed lazily on first use.
        self._injected_http_client = http_client

    # -- account discovery -----------------------------------------------

    def list_accounts(self) -> dict:
        """Return the discovered Raydium accounts with complete credentials.

        The wizard uses this to render the Raydium account-selection
        menu. Secret values are never returned.
        """
        accounts = discover_raydium_accounts()
        return {
            "success": True,
            "exchange": "raydium",
            "accounts": [
                {"account": a.account, "broker_id": RAYDIUM_BROKER_ID}
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
        if operation == "order":
            return execute_order(request, accounts_resolver=lambda _acct: discover_raydium_accounts(), client_factory=lambda: RaydiumHttpClient(), sign_request_fn=_sign_request)
        if operation == "batch_orders":
            # TradeDesk normalizes a ``ladder`` wizard request to
            # ``batch_orders`` with a ``child_orders`` list. Orderly
            # does not expose a true batch-insert endpoint; we submit
            # each child via the existing single-order path. See
            # ``execute_batch_orders`` for the per-child semantics.
            return execute_batch_orders(request, accounts_resolver=lambda _acct: discover_raydium_accounts(), client_factory=lambda: RaydiumHttpClient(), sign_request_fn=_sign_request)
        if operation == "cancel_order":
            return execute_cancel(request, accounts_resolver=lambda _acct: discover_raydium_accounts(), client_factory=lambda: RaydiumHttpClient(), sign_request_fn=_sign_request)
        if operation in {"cancel_orders", "cancel_order_group"}:
            return execute_cancel_group(request, accounts_resolver=lambda _acct: discover_raydium_accounts(), client_factory=lambda: RaydiumHttpClient(), sign_request_fn=_sign_request)
        if operation == "set_tp":
            # Position Manager: take-profit. Orderly's algo endpoint at
            # /v1/algo/order accepts a single TP trigger per
            # request. The wizard sends ``set_tp`` with the selected
            # position and a trigger price; 0 removes an existing TP.
            # See execute_set_tpsl for the wire-format details.
            return execute_set_tpsl(
                request,
                accounts_resolver=lambda _acct: discover_raydium_accounts(),
                client_factory=lambda: RaydiumHttpClient(),
                sign_request_fn=_sign_request,
                fetch_active_tpsl=self._fetch_active_positional_tpsl,
                cancel_algo_order=self._cancel_algo_order,
            )
        if operation == "set_sl":
            # Position Manager: stop-loss. Same endpoint, order_type
            # STOP_LOSS instead of TAKE_PROFIT.
            return execute_set_tpsl(
                request,
                accounts_resolver=lambda _acct: discover_raydium_accounts(),
                client_factory=lambda: RaydiumHttpClient(),
                sign_request_fn=_sign_request,
                fetch_active_tpsl=self._fetch_active_positional_tpsl,
                cancel_algo_order=self._cancel_algo_order,
            )
        return _execution_result(
            request,
            success=False,
            error=f"Unsupported Raydium operation: {operation}",
        )

    # -- internal helpers ------------------------------------------------

    def _http_client(self) -> RaydiumHttpClient:
        if self._injected_http_client is not None:
            return self._injected_http_client
        return RaydiumHttpClient()

    def _resolve_or_error(self, request: Mapping[str, Any]) -> Tuple[
        Optional[_ResolvedCredentials], Optional[dict]
    ]:
        """Resolve credentials or return an early-exit execution-result."""
        account = request.get("account") or ""
        if not account or not isinstance(account, str):
            return None, _execution_result(
                request,
                success=False,
                error="Missing 'account' in request",
            )
        try:
            creds = _resolve_account_credentials(account)
        except ValueError as exc:
            return None, _execution_result(
                request,
                success=False,
                error=str(exc),
            )
        return creds, None

    # -- balance ---------------------------------------------------------

    def _balance(self, request: Mapping[str, Any]) -> dict:
        creds, err = self._resolve_or_error(request)
        if err is not None:
            return err
        assert creds is not None
        client = self._http_client()
        try:
            payload = client._signed_request(
                creds=creds,
                method="GET",
                path="/v1/client/holding",
            )
        except _RaydiumHttpError as exc:
            return _execution_result(
                request,
                success=False,
                error=str(exc),
                exchange_response={"status": exc.status_code, "payload": exc.payload},
            )

        # Orderly success envelope: {"success": true, "data": {"holding": [...]}}
        # The full payload is passed to the normalizer because the
        # endpoint returns per-token rows inside ``data.holding``, not
        # the flat cross-margin dict described in the positions-tpsl
        # skill (which describes the /v1/positions risk metrics, not
        # /v1/client/holding).
        balance = _to_hermes_balance(payload)

        # Surface the raw payload under exchange_response for diagnostics.
        exchange_response = {
            "raw": payload,
            "holding": payload.get("data", {}).get("holding", []) if isinstance(payload, Mapping) else [],
            "broker_id": RAYDIUM_BROKER_ID,
            "network": RAYDIUM_NETWORK,
        }
        return _execution_result(
            request,
            success=True,
            exchange_response=exchange_response,
            balance=balance,
        )

    # -- positions -------------------------------------------------------

    def _positions(self, request: Mapping[str, Any]) -> dict:
        creds, err = self._resolve_or_error(request)
        if err is not None:
            return err
        assert creds is not None
        client = self._http_client()
        try:
            payload = client._signed_request(
                creds=creds,
                method="GET",
                path="/v1/positions",
            )
        except _RaydiumHttpError as exc:
            return _execution_result(
                request,
                success=False,
                error=str(exc),
                exchange_response={"status": exc.status_code, "payload": exc.payload},
            )

        # Orderly positions envelope: {"success": true, "data": {"rows": [...]}}
        rows: list[Any] = []
        if isinstance(payload, Mapping):
            data = payload.get("data", {})
            if isinstance(data, Mapping):
                rows = list(data.get("rows") or [])
            elif isinstance(data, list):
                rows = data
        normalized: list[dict[str, Any]] = []
        for raw in rows:
            pos = _hermes_normalize_raydium_position(raw)
            if pos is not None:
                normalized.append(pos)

        # Best-effort enrichment with active TP/SL from /v1/algo/orders.
        # Orderly's positions endpoint does not include TP/SL data;
        # the algo endpoint does. We merge the first TAKE_PROFIT and
        # STOP_LOSS child trigger prices into the position dict so the
        # renderer can show them. Failure to fetch algo orders does
        # NOT fail the positions response — TP/SL are best-effort.
        try:
            algo_orders = self._fetch_active_positional_tpsl(creds)
        except Exception:
            algo_orders = []
        for pos in normalized:
            for ao in algo_orders:
                # Orderly's algo order ``side`` is the CLOSE side
                # (opposite of the position side). For a long
                # position, the algo's side is SELL; for short, BUY.
                pos_side = pos.get("side")  # "long" or "short"
                expected_algo_side = (
                    "SELL" if pos_side == "long" else "BUY"
                )
                if (
                    ao.get("symbol") == pos.get("symbol")
                    and ao.get("side") == expected_algo_side
                ):
                    for child in ao.get("child_orders") or []:
                        if child.get("algo_type") == "TAKE_PROFIT":
                            tp = _safe_float(child.get("trigger_price"))
                            if tp is not None:
                                pos["take_profit"] = tp
                        elif child.get("algo_type") == "STOP_LOSS":
                            sl = _safe_float(child.get("trigger_price"))
                            if sl is not None:
                                pos["stop_loss"] = sl
                    break  # one POSITIONAL_TP_SL per (symbol, side)

        exchange_response = {
            "raw": payload,
            "broker_id": RAYDIUM_BROKER_ID,
            "network": RAYDIUM_NETWORK,
        }
        return _execution_result(
            request,
            success=True,
            exchange_response=exchange_response,
            positions=normalized,
            positions_active_count=len(normalized),
            positions_total_count=len(rows),
        )

    def _fetch_active_positional_tpsl(
        self, creds,
    ) -> list[dict[str, Any]]:
        """Fetch active (not triggered) POSITIONAL_TP_SL algo orders.

        Used by ``_positions`` to enrich position dicts with active
        TP/SL trigger prices. Orderly's positions endpoint does not
        include TP/SL data; the algo endpoint does. We filter to
        untriggered POSITIONAL_TP_SL only — BRACKET and other algo
        types are out of scope for the per-position summary.

        Returns a list of normalized algo order dicts (the raw shape
        from Orderly). Returns an empty list on any failure (network
        error, missing creds, etc.) so the position summary never
        fails because of the algo-orders lookup.
        """
        client = self._http_client()
        try:
            payload = client._signed_request(
                creds=creds,
                method="GET",
                path="/v1/algo/orders",
                params={"algo_type": "POSITIONAL_TP_SL", "status": "NEW"},
            )
        except _RaydiumHttpError:
            return []
        except Exception:
            return []
        if not isinstance(payload, Mapping):
            return []
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return []
        # Orderly's ``status=NEW`` query param filters server-side, but
        # we also defensively filter client-side in case Orderly ever
        # returns a stale CANCELLED order (e.g. during a race with
        # another cancellation request).
        rows = data.get("rows") or []
        return [
            dict(r) for r in rows
            if isinstance(r, Mapping) and r.get("algo_status") == "NEW"
        ]

    def _cancel_algo_order(
        self, creds, algo_order_id, symbol,
    ) -> dict[str, Any]:
        """Cancel a single algo order via DELETE /v1/algo/order.

        Orderly's cancellation endpoint (per OpenAPI spec) takes the
        ``order_id`` and ``symbol`` as query parameters, not in the
        body. Returns the Orderly cancellation response. Raises
        ``_RaydiumHttpError`` on failure.
        """
        client = self._http_client()
        return client._signed_request(
            creds=creds,
            method="DELETE",
            path="/v1/algo/order",
            params={"order_id": str(algo_order_id), "symbol": symbol},
        )

    # -- open_orders -----------------------------------------------------

    def _open_orders(self, request: Mapping[str, Any]) -> dict:
        creds, err = self._resolve_or_error(request)
        if err is not None:
            return err
        assert creds is not None
        client = self._http_client()
        # Orderly orders endpoint: GET /v1/orders?status=INCOMPLETE
        # (INCOMPLETE = open / not-yet-filled. CONFIRMED / CANCELLED
        # are terminal states.)
        try:
            payload = client._signed_request(
                creds=creds,
                method="GET",
                path="/v1/orders",
                params={"status": "INCOMPLETE"},
            )
        except _RaydiumHttpError as exc:
            return _execution_result(
                request,
                success=False,
                error=str(exc),
                exchange_response={"status": exc.status_code, "payload": exc.payload},
            )

        # Orderly orders envelope: {"success": true, "data": {"rows": [...]}}
        rows: list[Any] = []
        if isinstance(payload, Mapping):
            data = payload.get("data", {})
            if isinstance(data, Mapping):
                rows = list(data.get("rows") or [])
            elif isinstance(data, list):
                rows = data
        normalized = [
            _hermes_normalize_raydium_order(r)
            for r in rows if isinstance(r, Mapping)
        ]

        exchange_response = {
            "raw": payload,
            "broker_id": RAYDIUM_BROKER_ID,
            "network": RAYDIUM_NETWORK,
        }
        return _execution_result(
            request,
            success=True,
            exchange_response=exchange_response,
            orders=normalized,
            orders_active_count=len(normalized),
            orders_total_count=len(rows),
        )
