"""Rise (RISEx) exchange agent.

Isolated implementation. Owns:
  - dynamic RISE_/Rise_/rise_ account discovery
  - credential validation (wallet + apisignerprivate per alias)
  - EIP-712 signing against the RISEx Authorization domain
  - market metadata fetch + caching (5 minute TTL matches official spec)
  - balance, open-orders, and place-order operations
  - sanitized redaction for credentials, signatures, and signed payloads

Frozen-discipline contract:
  - This module imports nothing from other exchange agents
  - TradeDesk only knows about Rise via the agent interface
    (list_accounts, execute)
  - No withdrawal, transfer, leverage, margin-mode, or
    position-closing actions are exposed
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (sourced from official RISEx docs and live api.rise.trade probes)
# ---------------------------------------------------------------------------

RISE_BASE_URL = "https://api.rise.trade"
RISE_CHAIN_ID = 4153
RISE_EIP712_NAME = "RISEx"
RISE_EIP712_VERSION = "1"
RISE_DEFAULT_DEADLINE_SECONDS = 60 * 60  # 1 hour

# Time-in-force enum (uint32) confirmed against official risex-client SDK source.
# See risex-client/src/signing/types.ts (TimeInForce).
RISE_TIF_GTC = 0   # GoodTillCancelled
RISE_TIF_GTT = 1   # GoodTillTime; requires ttl_units > 0
RISE_TIF_FOK = 2   # FillOrKill
RISE_TIF_IOC = 3   # ImmediateOrCancel

# order_type enum (uint32) confirmed against official risex-client SDK source.
# See risex-client/src/signing/types.ts (OrderType).
RISE_ORDER_TYPE_MARKET = 0
RISE_ORDER_TYPE_LIMIT = 1

# side enum (uint32) confirmed against official risex-client SDK source.
# See risex-client/src/signing/types.ts (Side).
RISE_SIDE_BUY = 0   # Long
RISE_SIDE_SELL = 1  # Short

# stp_mode (uint32). SDK defines 0=ExpireMaker, 1=ExpireTaker, 2=ExpireBoth,
# 3=None — but the live API only accepts 0..2 (the SDK's `None` is a
# client-side placeholder, not a server-accepted value).
RISE_STP_DEFAULT = 0  # ExpireMaker (matches SDK default for limit orders)
RISE_DEFAULT_SYMBOL = "ETH"


# ---------------------------------------------------------------------------
# Sanitized error helpers
# ---------------------------------------------------------------------------

_RISE_REDACT_KEYS = (
    "apisignerprivate",
    "signer_private_key",
    "private_key",
    "signature",
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
)


# Rise returns numerical fields (size, avg_entry_price, quote_amount,
# leverage, unsettled_funding, last_funding_payment) in 1e18 wei units
# rather than as decimal strings. Divide by this constant to recover
# human-readable values (e.g. ``5,810,696,000,000,000,000 / 1e18``
# → ``5.810696`` BTC; ``25,000,000,000,000,000,000 / 1e18`` → ``25``).
_RISE_WEI_SCALE = Decimal(10) ** 18


def _rise_sanitize_error(exc: Exception) -> dict[str, Any]:
    text = str(exc)
    for key in _RISE_REDACT_KEYS:
        text = re.sub(
            rf"(?i)({re.escape(key)}\s*[=:]\s*)[^\s,;}}\]]+",
            rf"\1[REDACTED]",
            text,
        )
    if len(text) > 300:
        text = text[:300] + "…"
    return {"error_type": exc.__class__.__name__, "error": text}


def _rise_sanitize_response(payload: Any) -> Any:
    """Recursively redact sensitive material in dict/list/str payloads."""
    if isinstance(payload, Mapping):
        out = {}
        for k, v in payload.items():
            lk = str(k).lower()
            if lk in _RISE_REDACT_KEYS or "private" in lk or "signature" in lk:
                out[k] = "[REDACTED]"
            else:
                out[k] = _rise_sanitize_response(v)
        return out
    if isinstance(payload, list):
        return [_rise_sanitize_response(x) for x in payload]
    if isinstance(payload, str):
        text = payload
        for key in _RISE_REDACT_KEYS:
            text = re.sub(
                rf"(?i)({re.escape(key)}\s*[=:]\s*)[^\s,;}}\]]+",
                rf"\1[REDACTED]",
                text,
            )
        return text
    return payload


# ---------------------------------------------------------------------------
# Decimal helpers (no binary float math anywhere in the order path)
# ---------------------------------------------------------------------------

def _rise_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return d


def _rise_decimal_to_canonical(value: Decimal) -> str:
    return format(value, "f")


def _rise_quantize_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


# ---------------------------------------------------------------------------
# Account discovery (case-insensitive RISE_/Rise_/rise_ prefix)
# ---------------------------------------------------------------------------

_RISE_PREFIX_RE = re.compile(r"^(?:RISE|Rise|rise)_")
_RISE_FIELD_RE = re.compile(r"^(wallet|apisignerprivate)$", re.IGNORECASE)


@dataclass(frozen=True)
class RiseCredential:
    variable_name: str
    field: str  # "wallet" | "apisignerprivate"
    present: bool
    length: int


@dataclass(frozen=True)
class RiseAccount:
    alias: str
    wallet: RiseCredential
    apisignerprivate: RiseCredential

    def is_complete(self) -> bool:
        return self.wallet.present and self.apisignerprivate.present


def _rise_parse_env_name(name: str) -> Optional[tuple[str, str]]:
    """Return (alias, field) for an env-var name, or None if not a Rise var."""
    if not _RISE_PREFIX_RE.match(name):
        return None
    body = name[_RISE_PREFIX_RE.match(name).end():]
    # Last underscore-separated token is the field, rest is alias
    parts = body.rsplit("_", 1)
    if len(parts) != 2:
        return None
    alias_raw, field = parts[0], parts[1]
    if not _RISE_FIELD_RE.match(field):
        return None
    if not alias_raw:
        return None
    return alias_raw.strip("_").lower(), field.lower()


def _rise_load_credentials(env: Mapping[str, str]) -> dict[str, dict[str, RiseCredential]]:
    """Group env vars by alias. Never returns secret values."""
    grouped: dict[str, dict[str, RiseCredential]] = {}
    for name, value in env.items():
        parsed = _rise_parse_env_name(name)
        if parsed is None:
            continue
        alias, field = parsed
        present = bool(value)
        length = len(value) if present else 0
        bucket = grouped.setdefault(alias, {})
        # Duplicate normalized (alias, field) → keep the first occurrence but
        # raise if values differ. We compare lengths only (NEVER values) — this
        # catches a real divergence only if env legitimately stores different
        # values for the same logical credential. A length mismatch is a strong
        # signal of an ambiguity that warrants user review.
        existing = bucket.get(field)
        if existing is not None:
            if existing.length != length:
                raise ValueError(
                    f"Rise credential ambiguity: variable {name!r} clashes with "
                    f"{existing.variable_name!r} for alias {alias!r} field "
                    f"{field!r}; lengths differ ({existing.length} vs {length})."
                )
            continue
        bucket[field] = RiseCredential(
            variable_name=name,
            field=field,
            present=present,
            length=length,
        )
    return grouped


def _rise_filter_complete(creds: dict[str, dict[str, RiseCredential]]) -> list[RiseAccount]:
    accounts: list[RiseAccount] = []
    for alias in sorted(creds):
        bucket = creds[alias]
        wallet = bucket.get("wallet")
        signer = bucket.get("apisignerprivate")
        if wallet is None or signer is None:
            continue
        account = RiseAccount(alias=alias, wallet=wallet, apisignerprivate=signer)
        if account.is_complete():
            accounts.append(account)
    return accounts


# ---------------------------------------------------------------------------
# HTTP client interface — exposed as a tiny adapter for tests
# ---------------------------------------------------------------------------

class _RiseHTTPError(Exception):
    def __init__(self, status: int, body: str, path: str):
        self.status = status
        self.body = body
        self.path = path
        super().__init__(f"HTTP {status} on {path}: {body[:200]}")


class _RiseHTTPClient:
    """Minimal stdlib HTTP client.

    Replaced by tests via dependency injection. Exposes the same
    `get(path, params)` / `post(path, payload)` contract used by the
    project's other exchange agents.
    """

    def __init__(
        self,
        base_url: str = RISE_BASE_URL,
        timeout: float = 15.0,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Default headers. Cloudflare's bot-protection (error code
        # 1010) blocks the default Python-urllib User-Agent, so we
        # send a real UA plus Accept: application/json. Callers can
        # override via the ``default_headers`` argument.
        try:
            from hermes_cli import __version__ as _HERMES_VERSION
        except Exception:  # pragma: no cover - defensive fallback only
            _HERMES_VERSION = "0"
        base_headers = {
            "Accept": "application/json",
            "User-Agent": f"Hermes-RiseClient/{_HERMES_VERSION}",
        }
        if default_headers:
            for key, value in default_headers.items():
                if value is not None:
                    base_headers[str(key)] = str(value)
        self.default_headers = dict(base_headers)

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> dict:
        import urllib.parse
        import urllib.request

        url = self.base_url + path
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            if qs:
                url = url + ("?" + qs)
        req = urllib.request.Request(
            url, method="GET", headers=dict(self.default_headers)
        )
        return self._dispatch(req, path)

    def post(self, path: str, payload: Mapping[str, Any]) -> dict:
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={**self.default_headers, "Content-Type": "application/json"},
        )
        return self._dispatch(req, path)

    def _dispatch(self, req, path: str) -> dict:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            raise _RiseHTTPError(e.code, raw.decode("utf-8", "replace"), path)
        except Exception as e:
            raise _RiseHTTPError(0, str(e), path)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise _RiseHTTPError(200, raw.decode("utf-8", "replace"), path) from e


# ---------------------------------------------------------------------------
# EIP-712 signing
# ---------------------------------------------------------------------------

# VerifyWitness(address account,address target,bytes32 hash,uint48 nonceAnchor,uint8 nonceBitmap,uint32 deadline)
VERIFY_WITNESS_TYPEHASH = hashlib.sha256(
    b"VerifyWitness(address account,address target,bytes32 hash,uint48 nonceAnchor,uint8 nonceBitmap,uint32 deadline)"
).digest()


def _rise_keccak256(data: bytes) -> bytes:
    """Use pysha3 / pycryptodome if available, else hashlib via sha3_256."""
    try:
        from Crypto.Hash import keccak  # type: ignore

        h = keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()
    except Exception:
        pass
    try:
        import sha3  # type: ignore

        return sha3.keccak_256(data).digest()
    except Exception:
        pass
    # Fallback: pure-python sha3 is unavailable; raise clearly so we never
    # silently produce wrong signatures.
    raise RuntimeError(
        "keccak256 unavailable: install pycryptodome or pysha3 for EIP-712 signing"
    )


def _rise_sign_eip712_verify_witness(
    *,
    signer_private_key: bytes,
    domain_separator: bytes,
    account: str,
    target: str,
    action_hash: bytes,
    nonce_anchor: int,
    nonce_bitmap_index: int,
    deadline: int,
) -> bytes:
    """Sign an EIP-712 VerifyWitness message. Returns 65-byte (r||s||v) signature."""
    # Imports here so module is importable on systems without eth-keys.
    try:
        from eth_account import Account  # type: ignore
        from eth_account.messages import encode_typed_data  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "eth-account is required for Rise EIP-712 signing; install eth-account"
        ) from exc

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "VerifyWitness": [
                {"name": "account", "type": "address"},
                {"name": "target", "type": "address"},
                {"name": "hash", "type": "bytes32"},
                {"name": "nonceAnchor", "type": "uint48"},
                {"name": "nonceBitmap", "type": "uint8"},
                {"name": "deadline", "type": "uint32"},
            ],
        },
        "primaryType": "VerifyWitness",
        "domain": {
            "name": RISE_EIP712_NAME,
            "version": RISE_EIP712_VERSION,
            "chainId": RISE_CHAIN_ID,
            "verifyingContract": _rise_to_checksum_address(_rise_eip712_domain_verifying_contract()),
        },
        "message": {
            "account": _rise_to_checksum_address(account),
            "target": _rise_to_checksum_address(target),
            "hash": "0x" + action_hash.hex(),
            "nonceAnchor": int(nonce_anchor),
            "nonceBitmap": int(nonce_bitmap_index),
            "deadline": int(deadline),
        },
    }
    signable = encode_typed_data(full_message=typed)
    signed = Account.sign_message(signable, signer_private_key)
    return signed.signature  # 65 bytes


def _rise_eip712_domain_verifying_contract() -> str:
    """Verifying contract for the VerifyWitness domain.

    Per live /v1/system/config: Authorization contract at
    0x0d919daa3f12ae715744eb648c00066c5dbd66f0 (matches /v1/auth/eip712-domain).
    """
    return "0x0d919daa3f12ae715744eb648c00066c5dbd66f0"


def _rise_router_address() -> str:
    """RISExUniversalRouter per live /v1/system/config."""
    return "0xaadde0cea454f2bcb26f46ed54c5709b7bb34a7e"


def _rise_to_checksum_address(addr: str) -> str:
    """EIP-55 checksum. Returns lowercase if ethers is unavailable."""
    try:
        from eth_utils import to_checksum_address  # type: ignore
        return to_checksum_address(addr)
    except Exception:
        return addr.lower()


def _rise_decode_signer_private_key(raw: str) -> bytes:
    """Decode the apisignerprivate env value into 32 raw bytes.

    Two supported forms, tried in order:
      1) Hex with 0x prefix (e.g. "0x" + 64 hex chars)
      2) Base58 (e.g. 32 raw bytes base58-encoded, no 0x prefix)

    Anything else raises a sanitized error.
    """
    text = raw.strip()
    if text.startswith(("0x", "0X")):
        try:
            decoded = bytes.fromhex(text[2:])
        except ValueError as exc:
            raise ValueError("apisignerprivate hex decode failed") from exc
        if len(decoded) != 32:
            raise ValueError(
                f"apisignerprivate hex decoded to {len(decoded)} bytes, expected 32"
            )
        return decoded
    # base58 decode (Bitcoin alphabet)
    alphabet = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in text.encode("ascii"):
        idx = alphabet.find(bytes([ch]))
        if idx < 0:
            raise ValueError(f"apisignerprivate contains non-base58 char: {chr(ch)}")
        n = n * 58 + idx
    # Determine byte length
    body = text.encode("ascii")
    leading_ones = 0
    for b in body:
        if b == ord("1"):
            leading_ones += 1
        else:
            break
    decoded_bytes = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    decoded_bytes = b"\x00" * leading_ones + decoded_bytes
    if len(decoded_bytes) != 32:
        raise ValueError(
            f"apisignerprivate base58 decoded to {len(decoded_bytes)} bytes, expected 32"
        )
    return decoded_bytes


# ---------------------------------------------------------------------------
# Market metadata cache (TTL matches official spec: 5 minutes)
# ---------------------------------------------------------------------------

@dataclass
class _RiseMarketRow:
    market_id: int
    name: str
    step_size: Decimal
    step_price: Decimal
    min_order_size: Decimal
    max_leverage: Optional[int]
    post_only: bool


class _RiseMarketCache:
    def __init__(self, http: _RiseHTTPClient, ttl_seconds: float = 300.0) -> None:
        self.http = http
        self.ttl_seconds = ttl_seconds
        self._by_id: dict[int, _RiseMarketRow] = {}
        self._by_symbol: dict[str, _RiseMarketRow] = {}
        self._fetched_at: float = 0.0

    def _refresh(self) -> None:
        raw = self.http.get("/v1/markets")
        data = raw.get("data") if isinstance(raw, Mapping) else None
        markets = data.get("markets") if isinstance(data, Mapping) else None
        if not isinstance(markets, list):
            raise RuntimeError("invalid /v1/markets response")
        by_id: dict[int, _RiseMarketRow] = {}
        by_symbol: dict[str, _RiseMarketRow] = {}
        for entry in markets:
            if not isinstance(entry, Mapping):
                continue
            cfg = entry.get("config")
            if not isinstance(cfg, Mapping):
                continue
            market_id = int(entry.get("market_id") or cfg.get("market_id") or 0)
            name = str(cfg.get("name") or "").strip()
            step_size = _rise_decimal(cfg.get("step_size"))
            step_price = _rise_decimal(cfg.get("step_price"))
            min_order_size = _rise_decimal(cfg.get("min_order_size"))
            if not (step_size and step_price and min_order_size):
                continue
            row = _RiseMarketRow(
                market_id=market_id,
                name=name,
                step_size=step_size,
                step_price=step_price,
                min_order_size=min_order_size,
                max_leverage=int(cfg["max_leverage"]) if cfg.get("max_leverage") else None,
                post_only=bool(cfg.get("post_only") or False),
            )
            by_id[market_id] = row
            _MARKET_STEP_CACHE[market_id] = {
                "step_size": step_size,
                "step_price": step_price,
                "min_order_size": min_order_size,
            }
            base = name.split("/")[0].upper() if "/" in name else name.upper()
            by_symbol[base] = row
            by_symbol[name.upper()] = row
        self._by_id = by_id
        self._by_symbol = by_symbol
        self._fetched_at = time.time()

    def get_by_symbol(self, symbol: str) -> _RiseMarketRow:
        if not self._by_symbol or (time.time() - self._fetched_at) > self.ttl_seconds:
            self._refresh()
        sym = symbol.upper()
        row = self._by_symbol.get(sym)
        if row is None:
            # Try splitting on "-"
            row = self._by_symbol.get(sym.split("-")[0])
        if row is None:
            raise KeyError(f"Rise market not found for symbol {symbol!r}")
        return row

    def get_by_id(self, market_id: int) -> _RiseMarketRow:
        if not self._by_id or (time.time() - self._fetched_at) > self.ttl_seconds:
            self._refresh()
        row = self._by_id.get(int(market_id))
        if row is None:
            raise KeyError(f"Rise market not found for market_id {market_id!r}")
        return row


# ---------------------------------------------------------------------------
# Public agent
# ---------------------------------------------------------------------------

class RiseAgent:
    """Rise (RISEx) trade agent.

    Initial operation set:
      - balance
      - open_orders
      - place_order (single ETH limit only in this phase)
    """

    SUPPORTED_OPERATIONS = frozenset({
        "balance", "open_orders", "positions", "orders", "place_order",
        "set_tp", "set_sl", "take_profit", "stop_loss",
    })

    def __init__(
        self,
        *,
        http_client: Optional[Any] = None,
        env: Optional[Mapping[str, str]] = None,
        now_seconds: Optional[Any] = None,
    ) -> None:
        self._http = http_client or _RiseHTTPClient()
        self._env = dict(env if env is not None else os.environ)
        self._now_seconds = now_seconds or time.time
        self._market_cache = _RiseMarketCache(self._http)
        # Cache of the most recent margin summary, populated by
        # ``_positions`` and consumed by ``_balance`` so the
        # TradeDesk balance renderer shows withdrawable / margin_used.
        self._last_margin_summary: Optional[dict] = None
        # Cache of the most recent normalized positions. Populated by
        # ``_balance`` (inline fetch) and ``_positions``.
        self._last_positions: list = []  # type: ignore[attr-defined]  # list of dict

    # ------------------------------------------------------------------
    # Account discovery
    # ------------------------------------------------------------------

    def list_accounts(self) -> dict:
        try:
            grouped = _rise_load_credentials(self._env)
            accounts = _rise_filter_complete(grouped)
        except ValueError as exc:
            return _rise_error_response(
                operation="list_accounts",
                error=str(exc),
            )
        return {
            "success": True,
            "exchange": "rise",
            "accounts": [acc.alias for acc in accounts],
            "complete_aliases": [acc.alias for acc in accounts],
            "ambiguous_or_incomplete": [
                alias
                for alias, bucket in grouped.items()
                if not (
                    bucket.get("wallet") and bucket["wallet"].present
                    and bucket.get("apisignerprivate") and bucket["apisignerprivate"].present
                )
            ],
        }

    # ------------------------------------------------------------------
    # Execute dispatcher
    # ------------------------------------------------------------------

    def execute(self, request: Mapping[str, Any]) -> dict:
        if not isinstance(request, Mapping):
            return _rise_error_response(operation=None, error="request must be a mapping")
        operation = str(request.get("operation") or "")
        # TradeDesk wraps place_order into a normalized envelope with
        # operation="order" and parent_operation="place_order". Accept both.
        if operation == "order" and str(request.get("parent_operation") or "") == "place_order":
            operation = "place_order"
        if operation not in self.SUPPORTED_OPERATIONS:
            return _rise_error_response(
                operation=operation,
                error=f"unsupported Rise operation: {operation!r}",
            )
        account = request.get("account")
        if not isinstance(account, str) or not account:
            return _rise_error_response(
                operation=operation,
                error="missing account",
            )
        try:
            account_obj = self._resolve_account(account)
        except ValueError as exc:
            return _rise_error_response(
                operation=operation,
                error=str(exc),
                account=account,
            )
        if operation == "balance":
            return self._balance(account_obj)
        if operation == "open_orders":
            return self._open_orders(account_obj)
        if operation == "positions":
            return self._positions(account_obj)
        if operation == "orders":
            return self._orders(account_obj)
        if operation == "place_order":
            return self._place_order(account_obj, request)
        if operation in {"set_tp", "set_sl", "take_profit", "stop_loss"}:
            stop_type = "TAKE_PROFIT" if operation in {
                "set_tp", "take_profit",
            } else "STOP_LOSS"
            return self._set_tpsl_or_stop_loss(
                account_obj, request, stop_type=stop_type,
                operation_name=operation,
            )
        return _rise_error_response(operation=operation, error="unhandled operation")

    # ------------------------------------------------------------------
    # Internal: credential resolver
    # ------------------------------------------------------------------

    def _resolve_account(self, alias: str) -> RiseAccount:
        normalized = alias.strip().lower()
        grouped = _rise_load_credentials(self._env)
        bucket = grouped.get(normalized)
        if bucket is None:
            raise ValueError(f"unknown Rise account: {alias!r}")
        wallet = bucket.get("wallet")
        signer = bucket.get("apisignerprivate")
        if wallet is None or signer is None or not wallet.present or not signer.present:
            raise ValueError(f"Rise account {alias!r} is incomplete")
        return RiseAccount(alias=normalized, wallet=wallet, apisignerprivate=signer)

    def _read_wallet(self, account: RiseAccount) -> str:
        # Pull the raw env value here, since credential objects never store it
        return self._env.get(account.wallet.variable_name, "")

    def _read_signer(self, account: RiseAccount) -> bytes:
        raw = self._env.get(account.apisignerprivate.variable_name, "")
        try:
            return _rise_decode_signer_private_key(raw)
        except ValueError as exc:
            raise ValueError(
                f"Rise account {account.alias!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    def _balance(self, account: RiseAccount) -> dict:
        wallet = self._read_wallet(account)
        try:
            raw = self._http.get(
                "/v1/account/balance",
                {"account": wallet, "token": _rise_usdc_token_address()},
            )
        except _RiseHTTPError as exc:
            return _rise_error_response(
                operation="balance",
                account=account.alias,
                error=f"HTTP {exc.status} on {exc.path}: {exc.body[:120]}",
            )
        except Exception as exc:
            return _rise_error_response(
                operation="balance",
                account=account.alias,
                **_rise_sanitize_error(exc),
            )
        # If margin summary cache is empty (e.g. wizard calls balance
        # BEFORE positions), compute the margin inline by fetching
        # /v1/positions now. This ensures the wizard's balance view
        # always shows real margin/position data on first call.
        if self._last_margin_summary is None:
            try:
                positions_raw = self._http.get(
                    "/v1/positions", {"account": wallet},
                )
                # Compute margin summary from raw rows.
                margin_used_d = Decimal(0)
                total_pv_d = Decimal(0)
                p_rows = (
                    positions_raw.get("data", {}).get("positions", [])
                    if isinstance(positions_raw, Mapping) else []
                )
                # Compute per-position unrealized PnL via orderbook
                # so the TradeDesk renderer's "Open Positions" block
                # shows real numbers, not $0.00.
                pnl_by_id: dict[int, Decimal] = {}
                for row in p_rows:
                    if not isinstance(row, Mapping):
                        continue
                    quote_amount = _rise_decimal(row.get("quote_amount"))
                    if quote_amount is not None:
                        margin_used_d += abs(quote_amount) / _RISE_WEI_SCALE
                        total_pv_d += abs(quote_amount) / _RISE_WEI_SCALE
                    try:
                        mid_raw = row.get("market_id")
                        if mid_raw is None:
                            continue
                        pnl_value = self._rise_compute_unrealized_pnl(row)
                        if pnl_value is not None:
                            pnl_by_id[int(mid_raw)] = pnl_value
                    except Exception:
                        # PnL is best-effort; skip a row if OB fetch fails.
                        continue
                self._last_margin_summary = {
                    "margin_used": (
                        _rise_decimal_to_canonical(margin_used_d)
                        if margin_used_d > 0 else "0"
                    ),
                    "total_position_value": (
                        _rise_decimal_to_canonical(total_pv_d)
                        if total_pv_d > 0 else "0"
                    ),
                }
                # Build positions + push into raw_response["positions"]
                # so the TradeDesk renderer can find them. Also stash at
                # top-level for direct callers.
                self._last_positions = _rise_normalize_positions(
                    account, positions_raw,
                    market_cache=self._market_cache,
                    unrealized_pnl_by_id=pnl_by_id,
                )
            except Exception:
                # Best-effort: if positions fetch fails, fall back
                # to zero margin. Don't break balance.
                self._last_positions = []
                self._last_margin_summary = {
                    "margin_used": "0",
                    "total_position_value": "0",
                }
        result = _rise_normalize_balance(
            account, raw, margin_summary=self._last_margin_summary,
        )
        # Surface positions in the envelope so the renderer can show
        # them under "Open Positions". The renderer's
        # ``_format_balance_message`` reads positions from inside
        # ``raw_response``.
        positions = getattr(self, "_last_positions", None) or []
        result["positions"] = positions
        # Ensure raw_response exists (the renderer reads from it).
        if not isinstance(result.get("raw_response"), Mapping):
            result["raw_response"] = {}
        # Place a copy inside raw_response so the renderer finds it.
        if isinstance(result.get("raw"), Mapping):
            result["raw"]["positions"] = positions
        result["raw_response"]["positions"] = positions
        return result

    # ------------------------------------------------------------------
    # Open orders
    # ------------------------------------------------------------------

    def _open_orders(self, account: RiseAccount) -> dict:
        wallet = self._read_wallet(account)
        try:
            raw = self._http.get(
                "/v1/orders/open",
                {"account": wallet},
            )
        except _RiseHTTPError as exc:
            return _rise_error_response(
                operation="open_orders",
                account=account.alias,
                error=f"HTTP {exc.status} on {exc.path}: {exc.body[:120]}",
            )
        except Exception as exc:
            return _rise_error_response(
                operation="open_orders",
                account=account.alias,
                **_rise_sanitize_error(exc),
            )
        return _rise_normalize_open_orders(account, raw, market_cache=self._market_cache)

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def _positions(self, account: RiseAccount) -> dict:
        wallet = self._read_wallet(account)
        try:
            raw = self._http.get(
                "/v1/positions", {"account": wallet},
            )
        except _RiseHTTPError as exc:
            return _rise_error_response(
                operation="positions",
                account=account.alias,
                error=f"HTTP {exc.status} on {exc.path}: {exc.body[:120]}",
            )
        except Exception as exc:
            return _rise_error_response(
                operation="positions",
                account=account.alias,
                **_rise_sanitize_error(exc),
            )
        # Compute unrealized PnL per position by reading the orderbook
        # mid-price. Failures are isolated per-position — a stale OB
        # for one market doesn't break the others.
        raw_rows = (
            raw.get("data", {}).get("positions", [])
            if isinstance(raw, Mapping) else []
        )
        pnl_by_id: dict[int, Decimal] = {}
        margin_used = Decimal(0)
        total_position_value = Decimal(0)
        for row in raw_rows:
            if not isinstance(row, Mapping):
                continue
            pnl = self._rise_compute_unrealized_pnl(row)
            try:
                market_id_int = int(row.get("market_id"))
            except (TypeError, ValueError):
                market_id_int = None
            if pnl is not None and market_id_int is not None:
                pnl_by_id[market_id_int] = pnl
            # Margin: rise returns quote_amount in 1e18 wei; convert.
            notional = _rise_decimal(row.get("quote_amount"))
            if notional is not None:
                margin_used += abs(notional) / _RISE_WEI_SCALE
                total_position_value += abs(notional) / _RISE_WEI_SCALE
        margin_summary = {
            "margin_used": _rise_decimal_to_canonical(margin_used) if margin_used > 0 else "0",
            "total_position_value": _rise_decimal_to_canonical(total_position_value) if total_position_value > 0 else "0",
        }
        self._last_margin_summary = margin_summary
        positions = _rise_normalize_positions(
            account, raw,
            market_cache=self._market_cache,
            unrealized_pnl_by_id=pnl_by_id,
        )
        # Mirror positions into the cache so subsequent ``_balance()``
        # calls return them in the envelope (renderer needs them).
        self._last_positions = positions
        # Fetch active TP/SL orders and merge into positions. Best-effort:
        # if the TP/SL endpoint fails, positions still work and TP/SL fields
        # remain unset (wizard shows —).
        try:
            tpsl_orders = _rise_list_tpsl_orders(
                self._http, account=wallet, market_id=None,
                status="TPSL_ORDER_STATUS_ACCEPTED",
            )
            positions = _enrich_positions_with_tpsl(positions, tpsl_orders)
        except Exception:
            # Swallow TP/SL enrichment errors — don't break positions.
            pass
        return {
            "success": True,
            "exchange": "rise",
            "operation": "positions",
            "account": account.alias,
            "positions": positions,
            "margin_used": margin_summary["margin_used"],
            "total_position_value": margin_summary["total_position_value"],
            "raw": _rise_sanitize_response(raw),
        }

    def _rise_compute_unrealized_pnl(
        self, position_row: Mapping[str, Any]
    ) -> Optional[Decimal]:
        """Compute unrealized PnL (USD) for one position row.

        PnL = (mark - entry) × size for longs (``side='BUY'``),
              (entry - mark) × size for shorts (``side='SELL'``).

        Mark price comes from the orderbook mid (best bid + best ask)/2.
        All values are converted from 1e18 wei to human units before
        arithmetic. Returns ``None`` if any input is missing or the
        orderbook is unreachable — the wizard will then show ``0.00``
        rather than a wrong number.
        """
        try:
            market_id = int(position_row.get("market_id"))
        except (TypeError, ValueError):
            return None
        raw_size = _rise_decimal(position_row.get("size"))
        raw_entry = _rise_decimal(position_row.get("avg_entry_price"))
        side = str(position_row.get("side") or "").upper()
        if raw_size is None or raw_entry is None or side not in {"BUY", "SELL"}:
            return None
        size = raw_size / _RISE_WEI_SCALE
        entry = raw_entry / _RISE_WEI_SCALE
        if size == 0:
            return None
        try:
            ob = self._http.get(
                "/v1/orderbook", {"market_id": market_id, "limit": 1},
            )
        except Exception:
            return None
        ob_data = ob.get("data") if isinstance(ob, Mapping) else None
        if not isinstance(ob_data, Mapping):
            return None
        bids = ob_data.get("bids") or []
        asks = ob_data.get("asks") or []
        if not bids or not asks:
            return None
        if not isinstance(bids[0], Mapping) or not isinstance(asks[0], Mapping):
            return None
        best_bid = _rise_decimal(bids[0].get("price"))
        best_ask = _rise_decimal(asks[0].get("price"))
        if best_bid is None or best_ask is None:
            return None
        mark = (best_bid + best_ask) / 2
        if side == "BUY":
            return (mark - entry) * size
        return (entry - mark) * size

    # ------------------------------------------------------------------
    # TP/SL place/cancel dispatcher
    # ------------------------------------------------------------------

    def _read_signer_for_account(self, account: RiseAccount) -> str:
        """Read the EIP-712 signer private key for ``account`` (hex string).

        The credentials are stored in ``self._env`` (dict-like) as
        ``RISE_<ALIAS>_APISIGNERPRIVATE`` (hex, optionally ``0x``-prefixed)
        or ``Rise_<alias>_apisignerprivate`` etc. We resolve the alias and
        return the raw value; the empty string means 'no key set'.
        """
        env = self._env or {}
        alias_upper = account.alias.upper()
        for prefix in ("RISE", "Rise", "rise"):
            candidate = f"{prefix}_{alias_upper}_APISIGNERPRIVATE"
            raw = env.get(candidate)
            if raw:
                return str(raw)
        # Bare lowercase form (Rise load_credentials path also accepts it).
        alias_lower = account.alias.lower()
        for prefix in ("RISE", "Rise", "rise"):
            candidate = f"{prefix}_{alias_lower}_apisignerprivate"
            raw = env.get(candidate)
            if raw:
                return str(raw)
        # As a last resort, scan env for any key matching the alias+apisignerprivate pattern.
        for k, v in env.items():
            if not v:
                continue
            kl = k.lower().replace("rise_", "")
            if kl == f"{alias_lower}_apisignerprivate" or kl.endswith(f"_{alias_lower}_apisignerprivate"):
                return str(v)
        return ""

    def _resolve_position_for_tpsl(
        self, account: RiseAccount, symbol: str, side_hint: str,
    ) -> Optional[Mapping[str, Any]]:
        """Find the open position matching the requested ``symbol``.

        Reads ``/v1/positions`` directly. Returns the normalized
        dict (with the wei-to-decimal scaling applied) or None.
        Side matching is based on the position's ``side`` field
        (long/short), with a fallback to sign-of-size for backward-compat.
        """
        wallet = self._read_wallet(account)
        try:
            raw = self._http.get("/v1/positions", {"account": wallet})
        except Exception:
            return None
        payload = raw.get("data") if isinstance(raw, Mapping) else None
        rows = payload.get("positions") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            return None
        # Look up the symbol on the market cache.
        try:
            market_row = self._market_cache.get_by_symbol(str(symbol).upper())
            target_market_id = str(market_row.market_id)
        except Exception:
            return None
        target_side = str(side_hint or "").lower()
        if target_side in {"buy", "long"}:
            target_side = "long"
        elif target_side in {"sell", "short"}:
            target_side = "short"
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("market_id")) != target_market_id:
                continue
            try:
                size_raw = _rise_decimal(row.get("size"))
            except Exception:
                continue
            if size_raw is None or size_raw == 0:
                continue
            pos_side = str(row.get("side") or "").lower()
            if pos_side in {"long", "short"} and target_side:
                if pos_side != target_side:
                    continue
            else:
                # Fallback: long = size > 0
                computed = "long" if size_raw > 0 else "short"
                if target_side and computed != target_side:
                    continue
            # Normalize using existing helpers.
            positions = _rise_normalize_positions(
                account, {"data": {"positions": [row]}},
                market_cache=self._market_cache,
            )
            return positions[0] if positions else None
        return None

    def _fetch_mark_price(self, market_id: int) -> Optional[Decimal]:
        """Fetch the orderbook mid price for SL direction sanity checks."""
        try:
            ob = self._http.get(
                "/v1/orderbook", {"market_id": market_id, "limit": 1},
            )
        except Exception:
            return None
        ob_data = ob.get("data") if isinstance(ob, Mapping) else None
        if not isinstance(ob_data, Mapping):
            return None
        bids = ob_data.get("bids") or []
        asks = ob_data.get("asks") or []
        if not bids or not asks:
            return None
        if not isinstance(bids[0], Mapping) or not isinstance(asks[0], Mapping):
            return None
        try:
            best_bid = _rise_decimal(bids[0].get("price"))
            best_ask = _rise_decimal(asks[0].get("price"))
        except Exception:
            return None
        if best_bid is None or best_ask is None:
            return None
        return (best_bid + best_ask) / 2

    def _verify_tpsl_order(
        self, account: RiseAccount, *, order_id: Any, market_id: int,
        side: str, stop_type: str, stop_price: str,
    ) -> bool:
        """Re-list /v1/orders/tpsl and match. Returns True if a matching
        order is found, False otherwise.

        Match logic (per spec): order_id matches, OR (market_id, side,
        stop_type, stop_price) all match.
        """
        wallet = self._read_wallet(account)
        try:
            orders = _rise_list_tpsl_orders(
                self._http, account=wallet, market_id=str(market_id),
                status="TPSL_ORDER_STATUS_ACCEPTED",
            )
        except Exception:
            return False
        for o in orders:
            if not isinstance(o, Mapping):
                continue
            if order_id is not None and str(o.get("order_id")) == str(order_id):
                return True
            if (
                str(o.get("market_id")) == str(market_id)
                and o.get("side") == side
                and o.get("stop_type") == stop_type
                and str(o.get("stop_price")) == stop_price
            ):
                return True
        return False

    def _cancel_active_tpsl(
        self, account: RiseAccount, symbol: str,
        *, stop_type: str, operation_name: str,
    ) -> dict:
        """Find active TP/SL orders for (symbol, side) of the given
        ``stop_type`` and cancel them. Idempotent: returns no-op success
        if no matching orders exist.

        Strategy: re-list /v1/orders/tpsl?statuses=ACCEPTED, filter by
        stop_type and the close side for the position, sign one
        CancelTpslOrder per match.
        """
        pos = self._resolve_position_for_tpsl(account, symbol, "")
        market_id = pos["market_id"] if pos else None
        pos_side = str(pos.get("side") or "").lower() if pos else ""
        close_side = (
            "SELL" if pos_side == "long"
            else "BUY" if pos_side == "short"
            else None
        )
        wallet = self._read_wallet(account)
        try:
            orders = _rise_list_tpsl_orders(
                self._http, account=wallet,
                market_id=str(market_id) if market_id is not None else None,
                status="TPSL_ORDER_STATUS_ACCEPTED",
            )
        except Exception as exc:
            return _rise_error_response(
                operation=operation_name, account=account.alias,
                error=f"RISE_CANCEL_LIST_FAILED: {exc}",
            )
        signer_dict = {
            "wallet": wallet,
            "apisignerprivate": self._read_signer_for_account(account),
        }
        cancelled: list = []
        for o in orders:
            if not isinstance(o, Mapping):
                continue
            if o.get("stop_type") != stop_type:
                continue
            if close_side is not None and o.get("side") != close_side:
                continue
            cancel_result = _rise_cancel_tpsl_order(
                signer_dict, self._http, order_id=str(o.get("order_id")),
            )
            if cancel_result.get("success"):
                cancelled.append(o.get("order_id"))
        return {
            "success": True,
            "operation": operation_name,
            "action": "cancel",
            "account": account.alias,
            "symbol": symbol,
            "market_id": int(market_id) if market_id is not None else None,
            "stop_type": stop_type,
            "cancelled_count": len(cancelled),
            "cancelled_order_ids": cancelled,
            "verified": True,
            "independently_verified": True,
            "verification_status": "confirmed_resting",
            "capability": (
                "take_profit" if stop_type == "TAKE_PROFIT" else "stop_loss"
            ),
            "remaining_count": 0,
        }

    def _set_tpsl_or_stop_loss(
        self, account: RiseAccount, request: Mapping[str, Any],
        *, stop_type: str, operation_name: str,
    ) -> dict:
        """Handle set_tp/set_sl/take_profit/stop_loss.

        - ``trigger_price`` (or ``price``) == 0 → cancel existing TP/SL.
        - Otherwise → sign EIP-712 PlaceTpslOrder and POST.
        - Validates SL direction vs mark_price for LONG/SHORT.

        Returns the TradeDesk-bound result envelope with
        ``verification_status`` so the renderer can produce the right
        Telegram message.

        The wizard sends a plain request; TradeDesk.normalize() then
        wraps that into a struct with the original keys under
        ``structured_request``. We accept both shapes here so the
        agent works whether called directly or via TradeDesk.
        """
        # Unwrap if the caller passed a TradeDesk-normalized struct.
        inner = (
            request.get("structured_request")
            if isinstance(request.get("structured_request"), Mapping)
            else None
        )
        lookup_chain: list[Mapping[str, Any]] = []
        if inner is not None:
            lookup_chain.append(inner)
            position_block = inner.get("position")
            if isinstance(position_block, Mapping):
                lookup_chain.append(position_block)
        else:
            position_block = request.get("position")
            if isinstance(position_block, Mapping):
                lookup_chain.append(position_block)
        # Use inner's `position` first, then outer-level position.
        position_block = (
            lookup_chain[0].get("position")
            if lookup_chain and isinstance(lookup_chain[0], Mapping)
            and isinstance(lookup_chain[0].get("position"), Mapping)
            else position_block
        )

        def _first(mapping_list: list[Mapping[str, Any]], key: str) -> Any:
            for m in mapping_list:
                v = m.get(key)
                if v is not None:
                    return v
            # Also check top-level request itself.
            return request.get(key)

        source_list: list[Mapping[str, Any]] = [r for r in lookup_chain if r is not None]
        # Populate position_block carefully if it's a Mapping.
        symbol = _first(source_list, "symbol") or ""
        side = (
            (position_block.get("side") if isinstance(position_block, Mapping) else None)
            or _first(source_list, "side") or ""
        )
        trigger_raw = _first(source_list, "trigger_price")
        if trigger_raw is None:
            trigger_raw = _first(source_list, "price")
        if trigger_raw is None:
            return _rise_error_response(
                operation=operation_name, account=account.alias,
                error="RISE_TP_MISSING_FIELDS",
            )
        try:
            trigger_price = Decimal(str(trigger_raw))
        except Exception:
            return _rise_error_response(
                operation=operation_name, account=account.alias,
                error="RISE_INVALID_TRIGGER_PRICE",
            )
        if trigger_price < 0:
            return _rise_error_response(
                operation=operation_name, account=account.alias,
                error="RISE_INVALID_TRIGGER_PRICE",
            )

        # 0 → cancel path.
        if trigger_price == 0:
            return self._cancel_active_tpsl(
                account, str(symbol),
                stop_type=stop_type, operation_name=operation_name,
            )

        # Place path: need the active position + (for SL) mark price.
        pos = self._resolve_position_for_tpsl(account, str(symbol), str(side))
        if not pos:
            return _rise_error_response(
                operation=operation_name, account=account.alias,
                error="RISE_POSITION_FLAT",
            )
        try:
            size_dec = _rise_decimal(pos.get("size"))
        except Exception:
            size_dec = None
        if size_dec is None or size_dec == 0:
            return _rise_error_response(
                operation=operation_name, account=account.alias,
                error="RISE_POSITION_FLAT",
            )
        market_id = pos.get("market_id")
        if market_id is None:
            return _rise_error_response(
                operation=operation_name, account=account.alias,
                error="RISE_MARKET_ID_MISSING",
            )
        pos_size_text = format(Decimal(str(pos["size"])), "f")
        pos_side = str(pos.get("side") or "").lower()

        # SL direction sanity check (TP doesn't need it).
        if stop_type == "STOP_LOSS":
            mark_price = self._fetch_mark_price(int(market_id))
            if mark_price is None or mark_price == 0:
                return _rise_error_response(
                    operation=operation_name, account=account.alias,
                    error="RISE_MARK_PRICE_MISSING",
                )
            if pos_side == "long" and trigger_price >= mark_price:
                return _rise_error_response(
                    operation=operation_name, account=account.alias,
                    error="RISE_INVALID_STOP_PRICE",
                )
            if pos_side == "short" and trigger_price <= mark_price:
                return _rise_error_response(
                    operation=operation_name, account=account.alias,
                    error="RISE_INVALID_STOP_PRICE",
                )

        # Build signer dict from the agent's credential lookup.
        wallet = self._read_wallet(account)
        signer_dict = {
            "wallet": wallet,
            "apisignerprivate": self._read_signer_for_account(account),
        }
        placement = _rise_place_tpsl_order(
            signer_dict, self._http,
            market_id=int(market_id),
            position_side=pos_side,
            size=pos_size_text,
            stop_type=stop_type,
            stop_price=str(trigger_price),
        )
        if not placement.get("success"):
            return _rise_error_response(
                operation=operation_name, account=account.alias,
                error=placement.get("error") or "RISE_TP_PLACE_FAILED",
                placement=placement,
            )

        # Independent verification via re-listing /v1/orders/tpsl.
        close_side = "SELL" if pos_side == "long" else (
            "BUY" if pos_side == "short" else None
        )
        verified = self._verify_tpsl_order(
            account,
            order_id=placement.get("order_id"),
            market_id=int(market_id),
            side=close_side or "",
            stop_type=stop_type,
            stop_price=str(trigger_price),
        )
        return {
            "success": True,
            "operation": operation_name,
            "account": account.alias,
            "symbol": str(symbol),
            "order_id": placement.get("order_id"),
            "market_id": int(market_id),
            "side": ("sell" if pos_side == "long"
                     else "buy" if pos_side == "short" else None),
            "size": pos_size_text,
            "stop_price": str(trigger_price),
            "verified": bool(verified),
            "independently_verified": bool(verified),
            "verification_status": (
                "confirmed_resting" if verified else "unconfirmed_at_exchange"
            ),
            "endpoint": placement.get("endpoint"),
            "raw_response": placement.get("raw_response"),
        }

    # ------------------------------------------------------------------
    # Orders (order history)
    # ------------------------------------------------------------------

    def _orders(self, account: RiseAccount) -> dict:
        """Fetch the recent order history, paginating as needed.

        Cap at 10 pages (1000 orders) to avoid runaway requests. Each
        page is 100 orders; we paginate only when the response signals
        more rows than the returned batch.
        """
        wallet = self._read_wallet(account)
        MAX_PAGES = 10
        PAGE_SIZE = 100
        all_orders: list[dict] = []
        raw_full: Optional[dict] = None
        try:
            for page in range(1, MAX_PAGES + 1):
                raw = self._http.get(
                    "/v1/orders",
                    {"account": wallet, "limit": PAGE_SIZE, "page": page},
                )
                if raw_full is None:
                    raw_full = raw
                data = raw.get("data") if isinstance(raw, Mapping) else None
                rows = data.get("orders") if isinstance(data, Mapping) else None
                if not isinstance(rows, list) or not rows:
                    break
                all_orders.extend(rows)
                # If we got fewer than a full page, we've reached the end.
                if len(rows) < PAGE_SIZE:
                    break
            if raw_full is not None and all_orders:
                merged = dict(raw_full)
                if isinstance(merged.get("data"), dict):
                    merged["data"] = {**merged["data"], "orders": all_orders}
                raw = merged
            else:
                raw = {"data": {"orders": []}, "request_id": None}
            orders = _rise_normalize_orders(
                account, raw, market_cache=self._market_cache,
            )
            return {
                "success": True,
                "exchange": "rise",
                "operation": "orders",
                "account": account.alias,
                "orders": orders,
                "raw": _rise_sanitize_response(raw),
            }
        except _RiseHTTPError as exc:
            return _rise_error_response(
                operation="orders",
                account=account.alias,
                error=f"HTTP {exc.status} on {exc.path}: {exc.body[:120]}",
            )
        except Exception as exc:
            return _rise_error_response(
                operation="orders",
                account=account.alias,
                **_rise_sanitize_error(exc),
            )

    # ------------------------------------------------------------------
    # Place order
    # ------------------------------------------------------------------

    def _place_order(self, account: RiseAccount, request: Mapping[str, Any]) -> dict:
        # Accept either the project-normalized child_order envelope
        # or a flat Rise-shaped request.
        if "child_order" in request and isinstance(request["child_order"], Mapping):
            child = request["child_order"]
            symbol = child.get("symbol")
            side = child.get("side")
            order_type = child.get("order_type") or "limit"
            price = child.get("price")
            size = child.get("size")
            reduce_only = bool(child.get("reduce_only", False))
            time_in_force = child.get("time_in_force") or "GTC"
        else:
            symbol = request.get("symbol")
            side = request.get("side")
            order_type = request.get("order_type") or "limit"
            price = request.get("price")
            size = request.get("size")
            reduce_only = bool(request.get("reduce_only", False))
            time_in_force = request.get("time_in_force") or "GTC"

        try:
            market = self._market_cache.get_by_symbol(str(symbol or RISE_DEFAULT_SYMBOL))
        except Exception as exc:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                **_rise_sanitize_error(exc),
            )

        side_int = _rise_normalize_side(side)
        if side_int is None:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                error=f"invalid side: {side!r}",
            )
        ot_int = _rise_normalize_order_type(order_type)
        if ot_int is None:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                error=f"invalid order_type: {order_type!r}",
            )
        tif_int = _rise_normalize_tif(time_in_force)
        if tif_int is None:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                error=f"invalid time_in_force: {time_in_force!r}",
            )

        price_d = _rise_decimal(price)
        size_d = _rise_decimal(size)
        if price_d is None or size_d is None:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                error="price and size must be finite Decimals",
            )
        if price_d <= 0 or size_d <= 0:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                error="price and size must be positive",
            )

        quantized_price = _rise_quantize_to_step(price_d, market.step_price)
        quantized_size = _rise_quantize_to_step(size_d, market.step_size)

        if quantized_size < market.min_order_size:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                error=(
                    f"quantized size {quantized_size} below min_order_size "
                    f"{market.min_order_size}"
                ),
            )

        # Convert decimal price/size to integer ticks/steps (matches
        # risex-client payload schema: price_ticks, size_steps are integers)
        price_ticks = int(quantized_price / market.step_price)
        size_steps = int(quantized_size / market.step_size)
        post_only = False  # explicitly off in this controlled order

        try:
            wallet = self._read_wallet(account)
            signer_key = self._read_signer(account)
            signer_address = _rise_signer_eth_address(signer_key)
            nonce_state = self._http.get(
                f"/v1/nonce-state/{wallet}"
            )
            ns_data = nonce_state.get("data") if isinstance(nonce_state, Mapping) else None
            nonce_anchor = int(ns_data.get("nonce_anchor") or 0) if isinstance(ns_data, Mapping) else 0
            nonce_bitmap_index = int(ns_data.get("current_bitmap_index") or 0) if isinstance(ns_data, Mapping) else 0

            action_hash = _rise_encode_order_action_hash(
                market_id=market.market_id,
                size_steps=size_steps,
                price_ticks=price_ticks,
                side=side_int,
                order_type=ot_int,
                time_in_force=tif_int,
                post_only=post_only,
                reduce_only=reduce_only,
                stp_mode=RISE_STP_DEFAULT,
            )

            deadline = int(self._now_seconds()) + RISE_DEFAULT_DEADLINE_SECONDS

            signature = _rise_sign_eip712_verify_witness(
                signer_private_key=signer_key,
                domain_separator=b"",
                account=wallet,
                target=_rise_router_address(),
                action_hash=action_hash,
                nonce_anchor=nonce_anchor,
                nonce_bitmap_index=nonce_bitmap_index,
                deadline=deadline,
            )
        except Exception as exc:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                **_rise_sanitize_error(exc),
            )

        permit = {
            "account": wallet,
            "signer": signer_address,
            "deadline": int(deadline),
            "nonce_anchor": str(nonce_anchor),
            "nonce_bitmap_index": int(nonce_bitmap_index),
            "signature": _rise_sig_to_base64(signature),
        }
        payload = {
            "market_id": market.market_id,
            "account": wallet,
            "side": side_int,
            "price_ticks": int(price_ticks),
            "size_steps": int(size_steps),
            "order_type": ot_int,
            "time_in_force": tif_int,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "stp_mode": RISE_STP_DEFAULT,
            "ttl_units": 0,
            "client_order_id": "0",
            "builder_id": 0,
            "permit": permit,
        }
        try:
            raw = self._http.post("/v1/orders/place", payload)
        except _RiseHTTPError as exc:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                error=f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}",
            )
        except Exception as exc:
            return _rise_error_response(
                operation="place_order",
                account=account.alias,
                **_rise_sanitize_error(exc),
            )
        return _rise_normalize_place_order(
            account=account,
            market=market,
            quantized_price=quantized_price,
            quantized_size=quantized_size,
            side_int=side_int,
            tif_int=tif_int,
            response=raw,
        )


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _rise_usdc_token_address() -> str:
    return "0xe436820ba0c69702c1d3e601d421c0ef38262739"


def _rise_normalize_balance(
    account: RiseAccount,
    raw: Mapping[str, Any],
    *,
    margin_summary: Optional[Mapping[str, Any]] = None,
) -> dict:
    payload = _rise_sanitize_response(raw)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    total = None
    if isinstance(data, Mapping):
        for key in ("amount", "balance", "total"):
            v = _rise_decimal(data.get(key))
            if v is not None:
                total = v
                break
    canonical_total = (
        _rise_decimal_to_canonical(total) if total is not None else None
    )
    margin_used_str = (margin_summary or {}).get("margin_used") or "0"
    total_pv_str = (margin_summary or {}).get("total_position_value") or "0"
    margin_used_dec = _rise_decimal(margin_used_str) or Decimal(0)
    total_pv_dec = _rise_decimal(total_pv_str) or Decimal(0)
    # Withdrawable = total - margin_used (free equity not backing positions).
    withdrawable_dec = (total or Decimal(0)) - margin_used_dec
    if withdrawable_dec < 0:
        withdrawable_dec = Decimal(0)
    withdrawable_str = _rise_decimal_to_canonical(withdrawable_dec)
    margin_used_str_canon = (
        _rise_decimal_to_canonical(margin_used_dec)
        if margin_used_dec > 0 else "0"
    )
    total_pv_str_canon = (
        _rise_decimal_to_canonical(total_pv_dec)
        if total_pv_dec > 0 else "0"
    )
    return {
        "success": True,
        "exchange": "rise",
        "operation": "balance",
        "account": account.alias,
        "balance": {
            "account_value": canonical_total,
            "account_equity": canonical_total,
            "withdrawable": withdrawable_str,
            "available_to_withdraw": withdrawable_str,
            "margin_used": margin_used_str_canon,
            "total_margin_used": margin_used_str_canon,
            "total_position_value": total_pv_str_canon,
            "totalNtlPos": total_pv_str_canon,
        },
        "balances": [
            {
                "asset": "USDC",
                "total": canonical_total,
                "available": withdrawable_str,
                "locked": margin_used_str_canon,
            }
        ]
        if total is not None
        else [],
        "account_value": canonical_total,
        "available_balance": withdrawable_str,
        "margin_used": margin_used_str_canon,
        "raw": payload,
    }


def _rise_normalize_open_orders(
    account: RiseAccount,
    raw: Mapping[str, Any],
    market_cache: Optional["_RiseMarketCache"] = None,
) -> dict:
    payload = _rise_sanitize_response(raw)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    orders_in = data.get("orders") if isinstance(data, Mapping) else None
    if not isinstance(orders_in, list):
        orders_in = []
    # Populate step cache for markets we don't already know about
    distinct_ids: set[int] = set()
    for entry in orders_in:
        if isinstance(entry, Mapping):
            try:
                distinct_ids.add(int(entry.get("market_id")))
            except Exception:
                pass
    for mid in distinct_ids:
        if mid in _MARKET_STEP_CACHE or market_cache is None:
            continue
        try:
            row = market_cache.get_by_id(mid)
            _MARKET_STEP_CACHE[mid] = {
                "step_size": row.step_size,
                "step_price": row.step_price,
                "min_order_size": row.min_order_size,
            }
        except Exception:
            pass
    orders_out: list[dict] = []
    for entry in orders_in:
        if not isinstance(entry, Mapping):
            continue
        market_id = entry.get("market_id")
        side_int = entry.get("side")
        side = "buy" if side_int == RISE_SIDE_BUY else "sell" if side_int == RISE_SIDE_SELL else None
        order_id = entry.get("order_id") or entry.get("id")
        client_order_id = entry.get("client_order_id") or entry.get("cl_ord_id")
        # Live API returns price/size as integer ticks/steps; decode to decimals
        price_ticks_raw = entry.get("price_ticks") or entry.get("price")
        size_steps_raw = entry.get("size_steps") or entry.get("size") or entry.get("qty")
        remaining_steps_raw = (
            entry.get("remaining_size_steps")
            or entry.get("size_steps")
            or entry.get("qty_remaining")
            or entry.get("remaining_size")
        )
        # Best-effort decode without market metadata
        price = None
        size = None
        remaining_size = None
        try:
            mid_int = int(market_id) if market_id is not None else None
            pt = int(price_ticks_raw) if price_ticks_raw is not None else None
            ss = int(size_steps_raw) if size_steps_raw is not None else None
            rs = int(remaining_steps_raw) if remaining_steps_raw is not None else None
            if pt is not None and mid_int in _MARKET_STEP_CACHE:
                price = _MARKET_STEP_CACHE[mid_int]["step_price"] * pt
            if ss is not None and mid_int in _MARKET_STEP_CACHE:
                size = _MARKET_STEP_CACHE[mid_int]["step_size"] * ss
            if rs is not None and mid_int in _MARKET_STEP_CACHE:
                remaining_size = _MARKET_STEP_CACHE[mid_int]["step_size"] * rs
        except Exception:
            pass
        orders_out.append(
            {
                "exchange": "rise",
                "account": account.alias,
                "symbol": _rise_symbol_for_market_id(market_id),
                "market": _rise_market_name_for_id(market_id),
                "market_id": market_id,
                "side": side,
                "price_ticks": price_ticks_raw,
                "size_steps": size_steps_raw,
                "price": _rise_decimal_to_canonical(price) if price is not None else None,
                "size": _rise_decimal_to_canonical(size) if size is not None else None,
                "remaining_size": _rise_decimal_to_canonical(remaining_size) if remaining_size is not None else None,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "status": entry.get("status") or "open",
                "order_type": entry.get("order_type"),
                "time_in_force": entry.get("time_in_force"),
                "reduce_only": bool(entry.get("reduce_only") or False),
                "post_only": bool(entry.get("post_only") or False),
                "raw": entry,
            }
        )
    return {
        "success": True,
        "exchange": "rise",
        "operation": "open_orders",
        "account": account.alias,
        "orders": orders_out,
        "open_order_count": len(orders_out),
        "raw": payload,
    }


def _rise_normalize_position_side(size_str: Any) -> Optional[str]:
    """Convert a signed-size string to ``long`` or ``short``.

    A position with positive size is long; negative is short. Zero or
    unparseable values return ``None`` (treated as a flat / closed
    position and filtered out by callers if needed).
    """
    d = _rise_decimal(size_str)
    if d is None or d == 0:
        return None
    return "long" if d > 0 else "short"


def _rise_normalize_positions(
    account: RiseAccount,
    raw: Mapping[str, Any],
    *,
    market_cache: Optional["_RiseMarketCache"] = None,
    unrealized_pnl_by_id: Optional[Mapping[int, Decimal]] = None,
) -> list[dict]:
    """Normalize the ``/v1/positions`` response into the Hermes-standard
    position shape used by TradeDesk renderers.

    Each Rise position is a flat dict keyed by ``market_id`` (numeric).
    Numerical fields are in 1e18 wei units — we divide by ``_RISE_WEI_SCALE``
    to produce human-readable values. We convert ``size`` to
    ``long``/``short`` via the signed value, resolve ``symbol`` via
    ``market_cache`` (market_id → name like "BTC/USDC"), and merge
    ``unrealized_pnl`` (Decimal, USD) computed by the agent's
    orderbook-based PnL helper when available.
    """
    payload = _rise_sanitize_response(raw)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    rows = data.get("positions") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        # Resolve symbol via market_cache.
        symbol = None
        market_id_raw = row.get("market_id")
        market_id_int: Optional[int] = None
        if market_cache is not None and market_id_raw is not None:
            try:
                market_id_int = int(market_id_raw)
                market_row = market_cache.get_by_id(market_id_int)
                symbol = market_row.name
            except (KeyError, ValueError, TypeError):
                symbol = None

        # Scale wei fields to human units (see _RISE_WEI_SCALE).
        size_raw = _rise_decimal(row.get("size"))
        notional_raw = _rise_decimal(row.get("quote_amount"))
        entry_raw = _rise_decimal(row.get("avg_entry_price"))
        leverage_raw = _rise_decimal(row.get("leverage"))
        unsettled_raw = _rise_decimal(row.get("unsettled_funding"))
        last_funding_raw = _rise_decimal(row.get("last_funding_payment"))

        size_d = size_raw / _RISE_WEI_SCALE if size_raw is not None else None
        notional_d = (
            notional_raw / _RISE_WEI_SCALE
            if notional_raw is not None else None
        )
        entry_d = (
            entry_raw / _RISE_WEI_SCALE
            if entry_raw is not None else None
        )
        leverage_d = (
            leverage_raw / _RISE_WEI_SCALE
            if leverage_raw is not None else None
        )
        unsettled_d = (
            unsettled_raw / _RISE_WEI_SCALE
            if unsettled_raw is not None else None
        )
        last_funding_d = (
            last_funding_raw / _RISE_WEI_SCALE
            if last_funding_raw is not None else None
        )

        # Look up precomputed unrealized_pnl (from _positions).
        pnl: Optional[Decimal] = None
        if unrealized_pnl_by_id is not None and market_id_int is not None:
            pnl = unrealized_pnl_by_id.get(market_id_int)

        out.append({
            "exchange": "rise",
            "account": account.alias,
            "symbol": symbol,
            "market_id": (
                str(market_id_raw) if market_id_raw is not None else None
            ),
            "side": _rise_normalize_position_side(row.get("size")),
            "size": (
                _rise_decimal_to_canonical(size_d)
                if size_d is not None else None
            ),
            "notional": (
                _rise_decimal_to_canonical(notional_d)
                if notional_d is not None else None
            ),
            "entry_price": (
                _rise_decimal_to_canonical(entry_d)
                if entry_d is not None else None
            ),
            "mark_price": None,  # filled by PnL helper if available
            "leverage": (
                _rise_decimal_to_canonical(leverage_d)
                if leverage_d is not None else None
            ),
            "isolated_margin": row.get("isolated_usdc_balance"),
            "unrealized_funding": (
                _rise_decimal_to_canonical(unsettled_d)
                if unsettled_d is not None else None
            ),
            "last_funding_payment": (
                _rise_decimal_to_canonical(last_funding_d)
                if last_funding_d is not None else None
            ),
            "unrealized_pnl": (
                _rise_decimal_to_canonical(pnl)
                if pnl is not None else None
            ),
            "margin_mode": row.get("margin_mode"),
            "raw": dict(row),
        })
    return out


def _rise_normalize_orders(
    account: RiseAccount,
    raw: Mapping[str, Any],
    *,
    market_cache: Optional["_RiseMarketCache"] = None,
) -> list[dict]:
    """Normalize the ``/v1/orders`` response (order history) into the
    Hermes-standard order shape. Optionally resolves ``symbol`` via
    market_cache (market_id → name).
    """
    payload = _rise_sanitize_response(raw)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    rows = data.get("orders") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        price_d = _rise_decimal(row.get("price"))
        size_d = _rise_decimal(row.get("size"))
        symbol = None
        market_id_raw = row.get("market_id")
        if market_cache is not None and market_id_raw is not None:
            try:
                market_row = market_cache.get_by_id(int(market_id_raw))
                symbol = market_row.name
            except (KeyError, ValueError, TypeError):
                symbol = None
        out.append({
            "exchange": "rise",
            "account": account.alias,
            "order_id": row.get("id") or row.get("order_id"),
            "symbol": symbol,
            "market_id": str(market_id_raw) if market_id_raw is not None else None,
            "side": str(row.get("side") or "").lower() or None,
            "order_type": str(row.get("type") or "").lower() or None,
            "price": _rise_decimal_to_canonical(price_d) if price_d is not None else None,
            "size": _rise_decimal_to_canonical(size_d) if size_d is not None else None,
            "status": str(row.get("status") or "").lower() or None,
            "raw": dict(row),
        })
    return out


def _rise_normalize_place_order(
    *,
    account: RiseAccount,
    market: _RiseMarketRow,
    quantized_price: Decimal,
    quantized_size: Decimal,
    side_int: int,
    tif_int: int,
    response: Mapping[str, Any],
) -> dict:
    payload = _rise_sanitize_response(response)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    order_id = None
    if isinstance(data, Mapping):
        order_id = (
            data.get("order_id")
            or data.get("id")
            or (data.get("order") if isinstance(data.get("order"), Mapping) else None)
        )
    if isinstance(data, Mapping) and isinstance(data.get("order"), Mapping):
        order_id = data["order"].get("order_id") or data["order"].get("id") or order_id
    side = "buy" if side_int == RISE_SIDE_BUY else "sell"
    accepted = bool(order_id)
    return {
        "success": accepted,
        "exchange": "rise",
        "operation": "place_order",
        "account": account.alias,
        "symbol": market.name.split("/")[0],
        "market": market.name,
        "market_id": market.market_id,
        "side": side,
        "price": _rise_decimal_to_canonical(quantized_price),
        "size": _rise_decimal_to_canonical(quantized_size),
        "order_id": order_id,
        "client_order_id": None,
        "status": "submitted" if accepted else "ambiguous",
        "order_type": "limit",
        "time_in_force": _rise_tif_to_string(tif_int),
        "reduce_only": False,
        "raw": payload,
        **({} if accepted else {"error": "ambiguous placement response: no order_id returned"}),
    }


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

_MARKET_STEP_CACHE: dict[Any, dict[str, Decimal]] = {}
_MARKET_SYMBOL_BY_ID = {
    1: "BTC",
    2: "ETH",
    3: "BNB",
    4: "SOL",
    5: "HYPE",
    6: "XRP",
    7: "TAO",
    8: "ZEC",
    9: "ONDO",
    10: "NEAR",
    11: "VVV",
    12: "LIT",
    13: "DOGE",
    14: "DOGE",
    15: "AERO",
    16: "AAVE",
    17: "XAU",
    18: "XAG",
}


def _rise_symbol_for_market_id(market_id: Any) -> Optional[str]:
    try:
        return _MARKET_SYMBOL_BY_ID.get(int(market_id))
    except Exception:
        return None


def _rise_market_name_for_id(market_id: Any) -> Optional[str]:
    sym = _rise_symbol_for_market_id(market_id)
    return f"{sym}/USDC" if sym else None


def _rise_normalize_side(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return RISE_SIDE_BUY if value else RISE_SIDE_SELL
    if isinstance(value, (int, float)):
        iv = int(value)
        if iv in (RISE_SIDE_BUY, RISE_SIDE_SELL):
            return iv
        return None
    text = str(value).strip().lower()
    if text in {"buy", "bid", "b", "long", "0"}:
        return RISE_SIDE_BUY
    if text in {"sell", "ask", "a", "short", "1"}:
        return RISE_SIDE_SELL
    return None


def _rise_normalize_order_type(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return RISE_ORDER_TYPE_LIMIT if value else RISE_ORDER_TYPE_MARKET
    if isinstance(value, (int, float)):
        iv = int(value)
        if iv in (RISE_ORDER_TYPE_MARKET, RISE_ORDER_TYPE_LIMIT):
            return iv
        return None
    text = str(value or "").strip().lower()
    if text in {"market", "0"}:
        return RISE_ORDER_TYPE_MARKET
    if text in {"limit", "1"}:
        return RISE_ORDER_TYPE_LIMIT
    return None


def _rise_normalize_tif(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return RISE_TIF_GTC if value else None
    if isinstance(value, (int, float)):
        iv = int(value)
        if iv in {RISE_TIF_GTC, RISE_TIF_GTT, RISE_TIF_FOK, RISE_TIF_IOC}:
            return iv
        return None
    text = str(value or "").strip().lower()
    if text in {"gtc", "good_til_cancel", "good-til-cancel", ""}:
        return RISE_TIF_GTC
    if text in {"gtt", "good_til_time", "good-til-time"}:
        return RISE_TIF_GTT
    if text in {"fok", "fill_or_kill", "fill-or-kill"}:
        return RISE_TIF_FOK
    if text in {"ioc", "immediate_or_cancel", "immediate-or-cancel"}:
        return RISE_TIF_IOC
    if text in {"alo", "post_only", "post-only"}:
        # Post-only is encoded via post_only=true (boolean flag), not a TIF value.
        return None
    return None


def _rise_tif_to_string(tif_int: int) -> str:
    return {
        RISE_TIF_GTC: "GTC",
        RISE_TIF_GTT: "GTT",
        RISE_TIF_FOK: "FOK",
        RISE_TIF_IOC: "IOC",
    }.get(tif_int, str(tif_int))


# ---------------------------------------------------------------------------
# Bit-packed order encoder (matches risex-client/src/signing/encoder.ts)
# ---------------------------------------------------------------------------

ACTION_PLACE_ORDER = "RISE_PERPS_PLACE_ORDER_V1"
RISE_HEADER_VERSION = 1


def _rise_bytes_to_int(value: bytes) -> int:
    return int.from_bytes(value, "big")


def _rise_encode_order_action_hash(
    *,
    market_id: int,
    size_steps: int,
    price_ticks: int,
    side: int,
    order_type: int,
    time_in_force: int,
    post_only: bool,
    reduce_only: bool,
    stp_mode: int,
) -> bytes:
    """Compute the action hash bound to order parameters, per the official
    risex-client SDK encoder. Returns bytes32 keccak256 hash.

    Order-data encoding (from risex-client/src/signing/encoder.ts
    `encodeOrderData`):
      bits 70..85  market_id     (uint16)
      bits 38..70  size_steps    (uint32)
      bits 14..38  price_ticks   (uint24)
      bits  6..14  order_flags   (uint8)
      bits  1..6   header_version (uint5)
      bit   0      unused
    """
    try:
        from Crypto.Hash import keccak as _keccak  # type: ignore

        def _k(data: bytes) -> bytes:
            h = _keccak.new(digest_bits=256)
            h.update(data)
            return h.digest()
    except Exception:
        try:
            import sha3  # type: ignore

            def _k(data: bytes) -> bytes:
                return sha3.keccak_256(data).digest()
        except Exception:
            raise RuntimeError(
                "keccak256 unavailable: install pycryptodome or pysha3"
            )

    action_hash_id = _k(ACTION_PLACE_ORDER.encode("utf-8"))
    order_flags = 0
    if side & 1:
        order_flags |= 1
    if post_only:
        order_flags |= 2
    if reduce_only:
        order_flags |= 4
    order_flags |= (stp_mode & 3) << 3
    order_flags |= (order_type & 1) << 5
    order_flags |= (time_in_force & 3) << 6
    header_version = RISE_HEADER_VERSION
    data = 0
    data |= (market_id & 0xFFFF) << 70
    data |= (size_steps & 0xFFFFFFFF) << 38
    data |= (price_ticks & 0xFFFFFF) << 14
    data |= (order_flags & 0xFF) << 6
    data |= (header_version & 0x1F) << 1
    try:
        from eth_abi import encode as abi_encode  # type: ignore

        encoded = abi_encode(
            ["bytes32", "uint8", "uint256", "uint16", "uint64", "uint16"],
            [
                action_hash_id,
                1,  # header_flags: V3_FLAG_PERMIT (no builder_id, no client_order_id, no ttl)
                data,
                0,  # builder_id
                0,  # client_order_id
                0,  # ttl_units
            ],
        )
    except Exception:
        # eth_abi unavailable: manual abi-encode equivalent (uint256 big-endian 32 bytes)
        encoded = (
            action_hash_id
            + bytes([1])
            + data.to_bytes(32, "big")
            + (0).to_bytes(32, "big")
            + (0).to_bytes(8, "big")
            + (0).to_bytes(2, "big")
        )
    return _k(encoded)



def _rise_sig_to_base64(sig_bytes: bytes) -> str:
    """Encode signature as base64 (matches risex-client hexToBase64)."""
    import base64

    return base64.b64encode(sig_bytes).decode("ascii")


def _rise_signer_eth_address(signer_key_bytes: bytes) -> str:
    """Derive the EVM address corresponding to a secp256k1 private key."""
    try:
        from eth_account import Account  # type: ignore

        return Account.from_key(signer_key_bytes).address
    except Exception as exc:
        raise RuntimeError(
            "eth-account required to derive signer EVM address"
        ) from exc


# ---------------------------------------------------------------------------
# TP/SL (TakeProfit/StopLoss) support via EIP-712 typed-data.
# ---------------------------------------------------------------------------
#
# Rise exposes TP/SL via:
#   GET  /v1/auth/eip712-domain    — fetch the EIP-712 signing domain (cached)
#   GET  /v1/orders/tpsl            — list existing TP/SL orders
#   POST /v1/orders/tpsl            — place a new TP/SL (PlaceTpslOrder typed)
#   POST /v1/orders/tpsl/cancel     — cancel an active TP/SL (CancelTpslOrder)
#
# Status filter enum is "TPSL_ORDER_STATUS_ACCEPTED" (full prefix, not just "ACCEPTED").
# Field values for stop_type are "TAKE_PROFIT" / "STOP_LOSS" (strings).
# Field values for side are "BUY" / "SELL" (strings). Numeric enums (0=BUY, 1=SELL)
# are used only inside the EIP-712 typed message; the wire JSON uses strings
# for side/stop_type and integers for the matching numeric enums on the
# POST /v1/orders/tpsl body (0=TP, 1=SL).

# Process-local cache for the EIP-712 domain.
_RISE_DOMAIN_CACHE: Optional[dict] = None
_RISE_DOMAIN_CACHE_EXPIRES_AT: float = 0.0
_RISE_DOMAIN_TTL_SECONDS: float = 300.0


def _rise_reset_eip712_domain_cache() -> None:
    """Clear the cached EIP-712 domain (test helper)."""
    global _RISE_DOMAIN_CACHE, _RISE_DOMAIN_CACHE_EXPIRES_AT
    _RISE_DOMAIN_CACHE = None
    _RISE_DOMAIN_CACHE_EXPIRES_AT = 0.0


def _rise_get_eip712_domain(http: "Any") -> dict:
    """Fetch (and cache) the EIP-712 signing domain.

    Returns ``{"name", "version", "chain_id", "verifying_contract"}``.
    Caches per-process for 5 minutes; subsequent calls within the TTL
    reuse the cached dict without an extra HTTP request.
    """
    global _RISE_DOMAIN_CACHE, _RISE_DOMAIN_CACHE_EXPIRES_AT
    now = time.time()
    if _RISE_DOMAIN_CACHE is not None and now < _RISE_DOMAIN_CACHE_EXPIRES_AT:
        return _RISE_DOMAIN_CACHE
    raw = http.get("/v1/auth/eip712-domain")
    data = raw.get("data") if isinstance(raw, Mapping) else None
    if not isinstance(data, Mapping):
        raise RuntimeError("invalid /v1/auth/eip712-domain response")
    domain = {
        "name": str(data.get("name") or "RISEx"),
        "version": str(data.get("version") or "1"),
        "chain_id": str(data.get("chain_id") or data.get("chainId") or "0"),
        "verifying_contract": str(
            data.get("verifying_contract") or data.get("verifyingContract") or ""
        ),
    }
    _RISE_DOMAIN_CACHE = domain
    _RISE_DOMAIN_CACHE_EXPIRES_AT = now + _RISE_DOMAIN_TTL_SECONDS
    return _RISE_DOMAIN_CACHE


def _rise_sign_place_tpsl_order(
    signer_private_key: bytes,
    domain: dict,
    message: dict,
) -> bytes:
    """Sign an EIP-712 PlaceTpslOrder typed message. Returns raw bytes."""
    from eth_account import Account  # type: ignore
    from eth_account.messages import encode_typed_data  # type: ignore

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "PlaceTpslOrder": [
                {"name": "account", "type": "address"},
                {"name": "marketId", "type": "uint64"},
                {"name": "side", "type": "uint8"},
                {"name": "size", "type": "string"},
                {"name": "stopType", "type": "uint8"},
                {"name": "stopPrice", "type": "string"},
                {"name": "limitPrice", "type": "string"},
                {"name": "orderType", "type": "uint8"},
                {"name": "stopPriceOption", "type": "uint8"},
                {"name": "tif", "type": "uint8"},
                {"name": "deadline", "type": "uint32"},
                {"name": "sizePercentBps", "type": "uint32"},
            ],
        },
        "primaryType": "PlaceTpslOrder",
        "domain": {
            "name": domain["name"],
            "version": domain["version"],
            "chainId": int(domain["chain_id"]),
            "verifyingContract": domain["verifying_contract"],
        },
        "message": message,
    }
    signed = Account.sign_message(
        encode_typed_data(full_message=typed),
        signer_private_key,
    )
    return signed.signature


def _rise_sign_cancel_tpsl_order(
    signer_private_key: bytes,
    domain: dict,
    message: dict,
) -> bytes:
    """Sign an EIP-712 CancelTpslOrder typed message. Returns raw bytes."""
    from eth_account import Account  # type: ignore
    from eth_account.messages import encode_typed_data  # type: ignore

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "CancelTpslOrder": [
                {"name": "account", "type": "address"},
                {"name": "orderId", "type": "string"},
                {"name": "deadline", "type": "uint32"},
            ],
        },
        "primaryType": "CancelTpslOrder",
        "domain": {
            "name": domain["name"],
            "version": domain["version"],
            "chainId": int(domain["chain_id"]),
            "verifyingContract": domain["verifying_contract"],
        },
        "message": message,
    }
    signed = Account.sign_message(
        encode_typed_data(full_message=typed),
        signer_private_key,
    )
    return signed.signature


def _rise_resolve_signer_bytes(signer_obj: Any) -> bytes:
    """Resolve a signer private key from various input shapes.

    Accepts:
      - a dict with "signer_private_key" or "apisignerprivate" key
      - a RiseAccount-like with .apisignerprivate
    """
    if isinstance(signer_obj, Mapping):
        raw = (
            signer_obj.get("signer_private_key")
            or signer_obj.get("apisignerprivate")
        )
        if isinstance(raw, Mapping):
            raw = raw.get("value") or raw.get("present")
        return _rise_decode_signer_private_key(str(raw or ""))
    raw = getattr(signer_obj, "signer_private_key", None)
    if raw is None:
        apisignerprivate = getattr(signer_obj, "apisignerprivate", None)
        if apisignerprivate is not None:
            raw = getattr(apisignerprivate, "value", None)
    return _rise_decode_signer_private_key(str(raw or ""))


def _rise_resolve_wallet_address(signer_obj: Any) -> str:
    """Resolve the wallet (EOA) address from various input shapes."""
    if isinstance(signer_obj, Mapping):
        return str(signer_obj.get("wallet") or "").lower()
    wallet = getattr(signer_obj, "wallet", None)
    if wallet is None:
        wallet = getattr(signer_obj, "wallet_value", None)
    return str(wallet or "").lower()


def _rise_list_tpsl_orders(
    http: "Any",
    *,
    account: str,
    market_id: Optional[str] = None,
    status: Optional[str] = "TPSL_ORDER_STATUS_ACCEPTED",
) -> list:
    """List TP/SL orders via GET /v1/orders/tpsl.

    Args:
      http: the agent's _RiseHTTPClient.
      account: wallet address (lowercase).
      market_id: optional market_id filter (string).
      status: optional status enum (``TPSL_ORDER_STATUS_ACCEPTED``,
        ``TPSL_ORDER_STATUS_CANCELLED``, etc.). Live-verified: ``ACCEPTED``
        alone returns HTTP 400; the full prefix is required.
    """
    params: dict[str, Any] = {"account": account}
    if market_id is not None:
        params["market_id"] = str(market_id)
    if status:
        params["statuses"] = status
    raw = http.get("/v1/orders/tpsl", params)
    payload = raw.get("data") if isinstance(raw, Mapping) else None
    rows = payload.get("orders") if isinstance(payload, Mapping) else None
    return list(rows) if isinstance(rows, list) else []


def _rise_place_tpsl_order(
    signer_obj: Any,
    http: "Any",
    *,
    market_id: int,
    position_side: str,
    size: str,
    stop_type: str,
    stop_price: str,
    size_percent_bps: int = 10000,
    tif: int = 2,
    order_type: int = 0,
    limit_price: str = "0",
    stop_price_option: int = 1,
    deadline_seconds: int = 300,
) -> dict:
    """Place a TP/SL via EIP-712 PlaceTpslOrder + POST /v1/orders/tpsl.

    Required wiring (the read-only path of `_positions` does NOT call this):
      - signer_obj: a dict-like or RiseAccount exposing
        ``wallet`` + ``signer_private_key``/``apisignerprivate.value``.
      - http: the agent's _RiseHTTPClient.

    Live-verified wire format:
      - The POST body uses *integer* enums for side (0=BUY / 1=SELL),
        stop_type (0=TP / 1=SL), stop_price_option (1=MARK_PRICE),
        tif (2=FOK).
      - String fields: account (wallet, lowercase), size (decimal),
        stop_price (decimal), limit_price ("0" for market-trigger).
      - signer (EVM address) + signature (base64) + deadline (unix seconds).
    """
    if position_side not in {"long", "short"}:
        return {"success": False, "error": f"invalid position_side: {position_side!r}"}
    close_side_int = 1 if position_side == "long" else 0
    stop_type_upper = stop_type.upper()
    if stop_type_upper not in {"TAKE_PROFIT", "STOP_LOSS"}:
        return {"success": False, "error": f"invalid stop_type: {stop_type!r}"}
    stop_type_int = 0 if stop_type_upper == "TAKE_PROFIT" else 1

    wallet_addr = _rise_resolve_wallet_address(signer_obj)
    signer_bytes = _rise_resolve_signer_bytes(signer_obj)
    domain = _rise_get_eip712_domain(http)
    deadline = int(time.time()) + int(deadline_seconds)

    size_text = format(Decimal(str(size)), "f")
    stop_price_text = format(Decimal(str(stop_price)), "f")

    message = {
        "account": wallet_addr,
        "marketId": int(market_id),
        "side": close_side_int,
        "size": size_text,
        "stopType": stop_type_int,
        "stopPrice": stop_price_text,
        "limitPrice": str(limit_price),
        "orderType": int(order_type),
        "stopPriceOption": int(stop_price_option),
        "tif": int(tif),
        "deadline": deadline,
        "sizePercentBps": int(size_percent_bps),
    }
    signature_bytes = _rise_sign_place_tpsl_order(signer_bytes, domain, message)
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")
    signer_address = _rise_signer_eth_address(signer_bytes)

    body = {
        "account": wallet_addr,
        "market_id": int(market_id),
        "side": close_side_int,
        "size": size_text,
        "stop_type": stop_type_int,
        "order_type": int(order_type),
        "stop_price": stop_price_text,
        "limit_price": str(limit_price),
        "stop_price_option": int(stop_price_option),
        "tif": int(tif),
        "deadline": deadline,
        "size_percent_bps": int(size_percent_bps),
        "signer": signer_address,
        "signature": signature_b64,
    }
    resp = http.post("/v1/orders/tpsl", body)
    data = resp.get("data") if isinstance(resp, Mapping) else None
    order_id = None
    if isinstance(data, Mapping):
        order_id = data.get("order_id") or data.get("id")
    return {
        "success": bool(order_id),
        "operation": "take_profit" if stop_type_upper == "TAKE_PROFIT" else "stop_loss",
        "order_id": order_id,
        "market_id": int(market_id),
        "side": "sell" if close_side_int == 1 else "buy",
        "size": size_text,
        "stop_price": stop_price_text,
        "deadline": deadline,
        "signer": signer_address,
        "verified": False,
        "endpoint": "/v1/orders/tpsl",
        "raw_response": data,
    }


def _rise_cancel_tpsl_order(
    signer_obj: Any,
    http: "Any",
    order_id: str,
    deadline_seconds: int = 300,
) -> dict:
    """Cancel an active TP/SL via EIP-712 CancelTpslOrder + POST.

    Args:
      signer_obj: a dict-like with ``wallet`` + ``signer_private_key``.
      http: the agent's _RiseHTTPClient.
      order_id: the rise tpsl order_id (string) to cancel.

    Live-verified wire format (CancelTpslOrder typed-data):
      - account (wallet address, lowercase)
      - orderId (string)
      - deadline (unix seconds, ~5 min)
      - signer (EVM address)
      - signature (base64)
    """
    wallet_addr = _rise_resolve_wallet_address(signer_obj)
    signer_bytes = _rise_resolve_signer_bytes(signer_obj)
    domain = _rise_get_eip712_domain(http)
    deadline = int(time.time()) + int(deadline_seconds)

    message = {"account": wallet_addr, "orderId": order_id, "deadline": deadline}
    signature_bytes = _rise_sign_cancel_tpsl_order(signer_bytes, domain, message)
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")
    signer_address = _rise_signer_eth_address(signer_bytes)

    body = {
        "account": wallet_addr,
        "orderId": order_id,
        "signer": signer_address,
        "signature": signature_b64,
        "deadline": deadline,
    }
    resp = http.post("/v1/orders/tpsl/cancel", body)
    data = resp.get("data") if isinstance(resp, Mapping) else None
    return {
        "success": True,
        "operation": "cancel_tpsl",
        "order_id": order_id,
        "deadline": deadline,
        "signer": signer_address,
        "endpoint": "/v1/orders/tpsl/cancel",
        "raw_response": data,
    }


def _enrich_positions_with_tpsl(
    positions: list,
    tpsl_orders: list,
) -> list:
    """Merge active TP/SL orders into their corresponding positions.

    For each position with market_id X, match tpsl_orders by:
      - market_id == pos.market_id
      - side == close side of the position
        (LONG → close side = "SELL"; SHORT → "BUY")
      - status == "TPSL_ORDER_STATUS_ACCEPTED"

    Attach:
      - pos["take_profit"]            (Decimal string)
      - pos["take_profit_order_id"]   (string)
      - pos["stop_loss"]              (Decimal string)
      - pos["stop_loss_order_id"]     (string)

    Cancelled / expired / filled orders are NOT merged.
    """
    if not isinstance(positions, list):
        return positions
    enriched = []
    for pos in positions:
        if not isinstance(pos, Mapping):
            enriched.append(pos)
            continue
        new_pos = dict(pos)
        market_id = pos.get("market_id")
        position_side = pos.get("side")
        if market_id and position_side:
            close_side = "SELL" if position_side == "long" else (
                "BUY" if position_side == "short" else None
            )
            if close_side:
                for o in tpsl_orders:
                    if not isinstance(o, Mapping):
                        continue
                    if (
                        str(o.get("market_id")) == str(market_id)
                        and o.get("side") == close_side
                        and o.get("status") == "TPSL_ORDER_STATUS_ACCEPTED"
                    ):
                        st = o.get("stop_type")
                        sp = o.get("stop_price")
                        oid = o.get("order_id")
                        if st == "TAKE_PROFIT" and sp is not None:
                            new_pos["take_profit"] = str(sp)
                            new_pos["take_profit_order_id"] = oid
                        elif st == "STOP_LOSS" and sp is not None:
                            new_pos["stop_loss"] = str(sp)
                            new_pos["stop_loss_order_id"] = oid
        enriched.append(new_pos)
    return enriched


def _rise_error_response(
    *,
    operation: Optional[str],
    error: str,
    account: Optional[str] = None,
    **extra: Any,
) -> dict:
    response: dict[str, Any] = {
        "success": False,
        "exchange": "rise",
        "operation": operation,
        "error": error,
    }
    if account is not None:
        response["account"] = account
    response.update(extra)
    return response


__all__ = [
    "RiseAgent",
    "RiseAccount",
    "RiseCredential",
    "RISE_BASE_URL",
    "RISE_CHAIN_ID",
    "RISE_EIP712_NAME",
    "RISE_EIP712_VERSION",
    "RISE_DEFAULT_SYMBOL",
]
