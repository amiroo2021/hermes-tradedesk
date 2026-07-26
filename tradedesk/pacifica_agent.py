"""Pacifica exchange-specific agent for TradeDesk.

Telegram and TradeDesk stay normalized/exchange-agnostic; Pacifica-specific
env lookup, signing, REST payload translation, and response normalization live here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import urllib.parse
import urllib.request
import urllib.error
from decimal import Decimal, InvalidOperation
from email.message import Message as EmailMessage
from pathlib import Path
from typing import Any, Mapping, Optional

from .account_discovery import discover_accounts
from .pacifica_tpsl import PacificaTpslPayload, build_tpsl_payload
from .request_utils import _request_field

PACIFICA_MAINNET_API_URL = "https://api.pacifica.fi"
SUPPORTED_OPERATIONS = {"balance", "positions", "open_orders", "batch_orders", "order", "cancel_orders", "set_tp", "set_sl"}
PACIFICA_BATCH_LIMIT = 10
PACIFICA_EXPIRY_WINDOW_MS = 5_000
PACIFICA_MARKET_INFO_TTL_SECONDS = 300
logger = logging.getLogger(__name__)

# Keys whose values must never be echoed into logs, agent.log, or
# the sanitized response payload. Kept conservative on the include
# side; tolerates case-insensitive variants in either the key name
# or surrounding schema. The set is recursive — nested
# mappings/lists are walked.
_PACIFICA_REDACTED_KEYS = frozenset({
    "signature",
    "signedaction",
    "signed_action",
    "private_key",
    "privatekey",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "secret_key",
    "seed",
    "mnemonic",
    "vault_address",
    "vaultaddress",
    "password",
    "passphrase",
})
_PACIFICA_REDACTED_VALUE = "[REDACTED]"


def _is_pacifica_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return key.strip().lower() in _PACIFICA_REDACTED_KEYS


def _redact_pacifica_value(value: Any) -> Any:
    """Walk a Pacifica-decoded body (or any structured value) and
    replace every value whose key appears in
    ``_PACIFICA_REDACTED_KEYS`` with the redaction sentinel.
    Strings, numbers, and primitive values are returned unchanged.
    """
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for k, v in value.items():
            if _is_pacifica_sensitive_key(k):
                redacted[k] = _PACIFICA_REDACTED_VALUE
            else:
                redacted[k] = _redact_pacifica_value(v)
        return redacted
    if isinstance(value, list):
        return [_redact_pacifica_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_pacifica_value(item) for item in value)
    return value


def _redact_pacifica_body_text(text: str) -> str:
    """Best-effort substring redaction for plain-text bodies where the
    JSON parser failed. Recognises the JSON-ish key/value pattern and
    replaces every value that follows a sensitive key with the
    redaction sentinel. Honest about its limits: deeply nested
    opaque payloads that don't match the JSON shape will leak. Such
    cases also wouldn't survive the JSON parser so they are already
    treated as opaque bodies in the trade_menu / tradedesk
    rendering path.
    """
    if not text:
        return text
    sanitized = text
    for key in sorted(_PACIFICA_REDACTED_KEYS, key=len, reverse=True):
        pattern = re.compile(
            rf'(\b{re.escape(key)}\b\s*"\s*:\s*)(".*?(?<!\\)(\\\\)?"|"[^"\\]*(?:\\.[^"\\]*)*")',
            re.IGNORECASE | re.DOTALL,
        )
        sanitized = pattern.sub(rf"\1\"{_PACIFICA_REDACTED_VALUE}\"", sanitized)
    return sanitized


def _safe_extract_http_body(fp_obj: Any, default_charset: str = "utf-8") -> tuple[bytes, str]:
    """Read the response body stream from an HTTPError exactly once.
    Returns ``(raw_bytes, decoded_text)``. Always succeeds; never
    raises. The decoded text uses the charset declared in
    ``Content-Type`` when extractable, otherwise ``default_charset``,
    and falls back to ``errors='replace'`` so a malformed body never
    crashes the error-handling path.
    """
    if fp_obj is None:
        return b"", ""
    raw: bytes = b""
    try:
        # Pacifica HTTP responses fit well under a few KB; we cap at
        # 256 KiB so a pathological body cannot exhaust memory.
        raw = fp_obj.read(262144) if hasattr(fp_obj, "read") else b""
    except Exception:
        try:
            raw = fp_obj.read()
        except Exception:
            raw = b""
    charset = default_charset
    try:
        # urllib's HTTPError.fp is an http.client.HTTPResponse and
        # exposes .headers() as a Message; we sniff charset conservatively
        # without depending on email.parser internals.
        headers = getattr(fp_obj, "headers", None)
        if headers is not None:
            ctype = headers.get("Content-Type") if hasattr(headers, "get") else None
            if isinstance(ctype, str):
                m = re.search(r"charset\s*=\s*([\"']?)([A-Za-z0-9_\-]+)\1", ctype, re.IGNORECASE)
                if m and m.group(2):
                    charset = m.group(2)
    except Exception:
        charset = default_charset
    try:
        text = raw.decode(charset or default_charset, errors="replace")
    except Exception:
        text = raw.decode("utf-8", errors="replace")
    return raw, text


def _parse_response_json(text: str) -> Optional[Any]:
    """Try to parse a response body as JSON; never raises."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_user_facing_exchange_message(parsed: Any, raw_body: str) -> Optional[str]:
    """Pick the most useful user-facing string from common Pacifica
    JSON error shapes and from the raw body when JSON parsing fails.
    Returns the first non-empty match found in this priority order:
    1. ``parsed["error"]`` (str → return; mapping → step into 2.)
    2. ``parsed["message"]``
    3. ``parsed["detail"]``
    4. ``parsed["reason"]``
    5. nested: ``parsed["error"]["message"]`` / ``["message"]`` /
       ``["detail"]`` / ``["reason"]`` / ``[0]["message"]``
    6. nested: ``parsed["data"]["error"]`` / ``["message"]`` / ``["detail"]``
    7. trailing fallback to the raw body (stripped of leading/trailing
       whitespace)
    Returns ``None`` only when both parsed and raw_body are empty.
    """
    if isinstance(parsed, Mapping):
        for key in ("error", "message", "detail", "reason"):
            value = parsed.get(key)
            text = _coerce_error_string(value)
            if text:
                return text
        # nested: parsed["error"] is a mapping
        inner = parsed.get("error")
        if isinstance(inner, Mapping):
            for key in ("message", "detail", "reason", "error"):
                text = _coerce_error_string(inner.get(key))
                if text:
                    return text
        # nested: parsed["error"] is a list (sometimes array of error objects)
        if isinstance(inner, list) and inner:
            first = inner[0]
            if isinstance(first, Mapping):
                for key in ("message", "detail", "reason", "error"):
                    text = _coerce_error_string(first.get(key))
                    if text:
                        return text
        # nested: parsed["data"]["error"]
        data = parsed.get("data")
        if isinstance(data, Mapping):
            for key in ("error", "message", "detail", "reason"):
                text = _coerce_error_string(data.get(key))
                if text:
                    return text
            for path in (("error", "message"), ("error", "detail"), ("data", "error")):
                # best-effort for arbitrary nested shapes
                cur: Any = parsed
                ok = True
                for step in path:
                    if not isinstance(cur, Mapping) or step not in cur:
                        ok = False
                        break
                    cur = cur[step]
                if ok:
                    text = _coerce_error_string(cur)
                    if text:
                        return text
    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, Mapping):
            for key in ("message", "detail", "reason", "error"):
                text = _coerce_error_string(first.get(key))
                if text:
                    return text
    body = (raw_body or "").strip()
    if body:
        return body[:512]
    return None


def _coerce_error_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Reject literal "[REDACTED]" sentinels that may have leaked back
    # from a previous redaction pass.
    if text == _PACIFICA_REDACTED_VALUE:
        return None
    return text


def _capture_pacifica_http_error(http_err: urllib.error.HTTPError) -> dict[str, Any]:
    """Build the structured diagnostics dict that accompanies a
    Pacifica HTTPError. Pure observability: never mutates the input,
    never raises, and never logs raw bytes. The returned dict is
    safe to put into ``agent.log`` or a Telegram message because
    sensitive keys are recursively redacted.
    """
    status_code: Optional[int] = None
    http_reason: str = ""
    if isinstance(http_err, urllib.error.HTTPError):
        # urllib's HTTPError exposes .code (int), .reason (str, but
        # may be empty), .headers (Message).
        status_code = getattr(http_err, "code", None)
        reason = getattr(http_err, "reason", None)
        if isinstance(reason, bytes):
            try:
                reason = reason.decode("utf-8", errors="replace")
            except Exception:
                reason = ""
        if not isinstance(reason, str):
            reason = ""
        http_reason = reason
    headers_pair: list[tuple[str, str]] = []
    try:
        hdr_obj = getattr(http_err, "headers", None)
        if hdr_obj is not None:
            # Defensive copy: never share or mutate the underlying
            # http.client headers object.
            try:
                for k, v in hdr_obj.items():
                    headers_pair.append((str(k), str(v)))
            except Exception:
                pass
    except Exception:
        pass
    sanitized_headers = _redact_pacifica_value([dict(headers_pair) if headers_pair else {}])
    sanitized_headers_list = sanitized_headers[0].items() if sanitized_headers else []

    raw_body_bytes, decoded_body = _safe_extract_http_body(getattr(http_err, "fp", None))
    parsed_json = _parse_response_json(decoded_body)
    sanitized_json = _redact_pacifica_value(parsed_json) if parsed_json is not None else None

    content_type_header = ""
    for k, v in headers_pair:
        if k.lower() == "content-type":
            content_type_header = v
            break

    # Sanitized response body: when JSON parsed, we store the
    # redacted JSON body (so users see {nested:{signature:'[REDACTED]'},...});
    # when JSON parsing failed, we redact the literal substring matches
    # against the same key set so plain-text bodies also lose any
    # leaked credentials. Final fall-through: empty body stays empty.
    sanitized_body: str
    if sanitized_json is not None:
        sanitized_body = json.dumps(sanitized_json, ensure_ascii=False)
    elif decoded_body:
        sanitized_body = _redact_pacifica_body_text(decoded_body)
    else:
        sanitized_body = decoded_body  # empty string

    user_message = _extract_user_facing_exchange_message(parsed_json, decoded_body)
    if user_message is None:
        if decoded_body:
            user_message = f"Pacifica HTTP {status_code}: {http_reason or 'Bad Request'}"
        else:
            user_message = f"Pacifica HTTP {status_code or '?'}: {http_reason or 'Bad Request'}"

    return {
        "status_code": status_code,
        "http_reason": http_reason,
        "content_type": content_type_header,
        "headers": list(sanitized_headers_list),
        "response_body": sanitized_body,
        "response_body_raw_size": len(raw_body_bytes),
        "response_json": sanitized_json,
        "exchange_error": user_message,
        "captured": True,
    }


class PacificaHTTPError(urllib.error.HTTPError):
    """Subclass of ``urllib.error.HTTPError`` that carries the
    observability diagnostics built by
    ``_capture_pacifica_http_error``. ``str(self)`` returns the
    standard ``HTTP Error <code>: <reason>`` text so existing
    exception-handling code that only reads ``str(exc)`` keeps
    behaving; the rich ``.diagnostics`` attribute is the new surface
    the agent uses to populate its error responses.
    """

    def __init__(self, original: urllib.error.HTTPError, diagnostics: Mapping[str, Any]):
        # HTTPError is a fancy UrlPath container. Pass the captured
        # values through to its constructor; the body+headers stay
        # readable via the inherited .fp / .headers.
        headers = getattr(original, "headers", None)
        try:
            super().__init__(
                getattr(original, "url", ""),
                getattr(original, "code", None),
                getattr(original, "msg", "") or "",
                headers,
                getattr(original, "fp", None),
            )
        except Exception:
            # If the parent constructor refuses (rare), keep the
            # essentials in attributes via a different path.
            self.code = getattr(original, "code", None)
            self.reason = "PacificaHTTPError"
            self.headers = headers
        self.diagnostics: dict[str, Any] = dict(diagnostics)
        self._diagnostics_url = getattr(original, "url", "")

    def __str__(self) -> str:  # pragma: no cover - tiny
        code = getattr(self, "code", None)
        reason = getattr(self, "reason", "") or ""
        msg = self.diagnostics.get("exchange_error") if isinstance(self.diagnostics, Mapping) else None
        if isinstance(msg, str) and msg.strip():
            return msg
        if code is not None:
            return f"HTTP Error {code}: {reason}" if reason else f"HTTP Error {code}"
        return reason or "HTTP Error"




def _execution_result(request: Mapping[str, Any], *, success: bool, error: Optional[str] = None, **extra: Any) -> dict:
    result = {
        "success": success,
        "exchange": "pacifica",
        "operation": request.get("operation"),
        "parent_operation": request.get("parent_operation"),
        "account": request.get("account"),
    }
    if error:
        result["error"] = error
    result.update(extra)
    return result


def _hermes_env_path() -> Path:
    home = os.getenv("HERMES_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".hermes") / ".env"


def _strip_dotenv_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _dotenv_casefold_map() -> dict[str, tuple[str, str]]:
    path = _hermes_env_path()
    out: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return out
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        if key and value.strip():
            out[key.lower()] = (key, _strip_dotenv_value(value).strip())
    return out


def _combined_casefold_env() -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    for env_key, env_value in os.environ.items():
        if env_value and env_value.strip():
            out[env_key.lower()] = (env_key, env_value.strip(), "environment")
    for lower_key, (actual_key, value) in _dotenv_casefold_map().items():
        if lower_key not in out:
            out[lower_key] = (actual_key, value, "dotenv")
    return out


def _account_segment(account: Optional[str]) -> Optional[str]:
    raw = str(account or "").strip()
    if not raw:
        return None
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()
    return normalized or None


def _candidate_names(account: Optional[str], kind: str) -> list[str]:
    segment = _account_segment(account)
    if segment:
        return [f"PACIFICA_{segment}_{kind}"]
    return [f"PACIFICA_{kind}"]


def _lookup_case_insensitive(names: list[str]) -> tuple[Optional[str], Optional[str], list[str]]:
    available = _combined_casefold_env()
    for name in names:
        found = available.get(name.lower())
        if found:
            actual_key, value, _source = found
            return value, actual_key, names
    return None, None, names


def _resolve_credentials(account: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], list[str]]:
    """Resolve Pacifica credentials for a logical account name."""
    master_address, master_key_env, address_searched = _lookup_case_insensitive(
        _candidate_names(account, "ADDRESS") + _candidate_names(account, "ACCOUNT")
    )
    agent_wallet, agent_wallet_env, agent_wallet_searched = _lookup_case_insensitive(
        _candidate_names(account, "AGENT_WALLET")
    )
    agent_private_key, agent_key_env, agent_searched = _lookup_case_insensitive(
        _candidate_names(account, "AGENT_PRIVATE_KEY")
    )
    master_private_key, master_priv_env, master_priv_searched = _lookup_case_insensitive(
        _candidate_names(account, "PRIVATE_KEY")
    )

    signing_private = agent_private_key or master_private_key
    key_env = agent_key_env or master_priv_env

    return (
        master_address,
        signing_private,
        master_key_env,
        key_env,
        agent_wallet,
        master_private_key,
        address_searched + agent_wallet_searched + agent_searched + master_priv_searched,
    )


def _redact_pacifica_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, val in value.items():
            k = str(key).lower()
            if k in {"signature", "private_key", "agent_private_key"}:
                redacted[key] = "[REDACTED]"
            elif k in {"account", "agent_wallet"}:
                redacted[key] = _mask_public_value(val)
            else:
                redacted[key] = _redact_pacifica_sensitive(val)
        return redacted
    if isinstance(value, list):
        return [_redact_pacifica_sensitive(item) for item in value]
    return value


def _mask_public_value(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "[REDACTED]"
    return f"[REDACTED:{text[-4:]}]"


def _sort_json_keys(value: Any) -> Any:
    """Recursively sort JSON object keys exactly like Pacifica's SDK."""
    if isinstance(value, dict):
        return {key: _sort_json_keys(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_json_keys(item) for item in value]
    return value


class PacificaSigner:
    def sign(self, header: Mapping[str, Any], payload: Mapping[str, Any], private_key: str) -> str:
        try:
            import base58  # type: ignore
            from solders.keypair import Keypair  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Pacifica order signing requires Python packages: solders base58") from exc

        keypair = Keypair.from_base58_string(private_key)
        message = _sort_json_keys({**dict(header), "data": dict(payload)})
        message_bytes = json.dumps(message, separators=(",", ":")).encode("utf-8")
        signature = keypair.sign_message(message_bytes)
        return base58.b58encode(bytes(signature)).decode("ascii")


class PacificaAgent:
    def __init__(self, *, base_url: str = PACIFICA_MAINNET_API_URL, http_client: Any = None, signer: Any = None, now_ms: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client
        self.signer = signer or PacificaSigner()
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._market_info_cache: dict[str, dict[str, Any]] = {}
        self._market_info_expires_at: float = 0.0
        self._tick_size_by_symbol: dict[str, Decimal] = {}
        self._lot_size_by_symbol: dict[str, Decimal] = {}
        self._min_notional_by_symbol: dict[str, Decimal] = {}
        self._tick_size_fallback: Decimal = Decimal("0.00000001")
        self._lot_size_fallback: Decimal = Decimal("0.00000001")
        self._min_order_size_usd_fallback: Decimal = Decimal("0")
        self._market_info_initialized = False
        self._market_info_last_error: Optional[str] = None
        self._market_info_error_count = 0
        self._market_info_error_limit = 3
        self._market_info_disabled = False
        self._market_info_log_prefix = "Pacifica market info"
        self._market_info_ttl_seconds = PACIFICA_MARKET_INFO_TTL_SECONDS

    def list_accounts(self) -> dict:
        accounts = discover_accounts("pacifica")
        return {
            "success": True,
            "exchange": "pacifica",
            "accounts": accounts,
            "message": f"Found {len(accounts)} Pacifica configured account(s).",
        }

    def execute(self, request: Mapping[str, Any]) -> dict:
        operation = str(request.get("operation") or "").lower()
        if operation not in SUPPORTED_OPERATIONS:
            return _execution_result(request, success=False, error=f"Unsupported Pacifica operation: {operation or None}")
        try:
            if operation == "balance":
                return self._balance(request)
            if operation == "positions":
                return self._positions(request)
            if operation == "open_orders":
                return self._open_orders(request)
            if operation == "cancel_orders":
                return self._cancel_orders(request)
            if operation == "batch_orders":
                return self._batch_orders(request)
            if operation == "order":
                child_order = request.get("child_order") or (request.get("child_orders") or [{}])[0]
                return self._batch_orders({**dict(request), "operation": "batch_orders", "child_orders": [child_order]})
            if operation in {"set_tp", "set_sl"}:
                return self._set_tp_sl(request)
        except Exception as exc:
            logger.exception("Pacifica %s failed", operation)
            # Observability path: surface the captured Pacifica HTTP
            # diagnostics (status, body, parsed JSON, headers, etc.)
            # alongside the friendly exchange message so callers like
            # tradedesk.py and trade_menu can render the exact
            # exchange-side reason rather than only ``str(exc)``.
            if isinstance(exc, PacificaHTTPError):
                diag = getattr(exc, "diagnostics", None) or {}
                user_msg = diag.get("exchange_error") if isinstance(diag, Mapping) else None
                if not isinstance(user_msg, str) or not user_msg.strip():
                    user_msg = str(exc)
                # Also redact the full outbound request body in the
                # log entry: do not log full signed payload by default.
                try:
                    tail = json.dumps({"pacifica_http_error": diag}, default=str, ensure_ascii=False)
                except Exception:
                    tail = repr(diag)
                logger.info("Pacifica HTTP diagnostics: %s", tail)
                return _execution_result(
                    request,
                    success=False,
                    error=user_msg,
                    error_type=exc.__class__.__name__,
                    http_diagnostics=diag,
                    exchange=diag.get("exchange", "pacifica") if isinstance(diag, Mapping) else "pacifica",
                )
            return _execution_result(request, success=False, error=str(exc), error_type=exc.__class__.__name__)
        return _execution_result(request, success=False, error=f"Unsupported Pacifica operation: {operation}")

    def _require_address(self, request: Mapping[str, Any]) -> tuple[Optional[str], Optional[dict]]:
        address, _trade_key, _address_key, _trade_key_name, _agent_key, _private_key, searched = _resolve_credentials(request.get("account"))
        if not address:
            return None, _execution_result(
                request,
                success=False,
                error=f"Missing Pacifica account address for account '{request.get('account')}'. Searched: {', '.join(searched)}",
            )
        return address, None

    def _require_trading_credentials(
        self, request: Mapping[str, Any]
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[dict]]:
        """Return (main account address, agent wallet pubkey, signing private key, error).

        Pacifica order-placement agent-key auth uses:
        - account:       PACIFICA_<ACCOUNT>_ADDRESS (the main account that owns the position)
        - agent_wallet:  PACIFICA_<ACCOUNT>_AGENT_WALLET, OR derived from the configured
                         agent private key (consistent with _require_tpsl_credentials).
        - signer:        PACIFICA_<ACCOUNT>_AGENT_PRIVATE_KEY

        ``agent_wallet`` MUST be sent in the outbound request so Pacifica's verifier
        reconstructs the canonical-payload signature against the agent keypair rather
        than the main account keypair. Sending the signature without an ``agent_wallet``
        field causes Pacifica to verify against the main-account key, which produces
        ``Verification failed: signature does not match signer and canonical payload.``
        when the configured trading keypair is not the main account's own keypair.
        """
        account_name = request.get("account")
        master_address, trade_key, _master_key_env, _key_env, configured_agent_wallet, _master_private_key, searched = _resolve_credentials(account_name)
        if not master_address:
            return None, None, None, _execution_result(
                request,
                success=False,
                error=f"Missing Pacifica trading account address for account '{account_name}'. Searched: {', '.join(searched)}",
            )
        if not trade_key:
            return None, None, None, _execution_result(
                request,
                success=False,
                error=f"Missing Pacifica agent private key for account '{account_name}'. Searched: {', '.join(searched)}",
            )
        try:
            from solders.keypair import Keypair  # type: ignore
            derived_agent_wallet = str(Keypair.from_base58_string(trade_key).pubkey())
        except Exception as exc:
            return None, None, None, _execution_result(
                request,
                success=False,
                error=f"Invalid Pacifica agent private key for account '{account_name}': {exc}",
            )
        agent_wallet = configured_agent_wallet or derived_agent_wallet
        if agent_wallet != derived_agent_wallet:
            segment = _account_segment(account_name) or ""
            return None, None, None, _execution_result(
                request,
                success=False,
                error=(
                    "Pacifica agent wallet mismatch for account '{account}': "
                    "PACIFICA_{segment}_AGENT_WALLET does not match the public key derived from "
                    "PACIFICA_{segment}_AGENT_PRIVATE_KEY."
                ).format(account=account_name, segment=segment),
            )
        return master_address, agent_wallet, trade_key, None

    def _require_tpsl_credentials(self, request: Mapping[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[dict]]:
        """Return (main account address, agent wallet public key, agent private key, error).

        Pacifica TP/SL agent-key auth uses:
        - account: PACIFICA_<ACCOUNT>_ADDRESS (the account that owns positions)
        - agent_wallet: PACIFICA_<ACCOUNT>_AGENT_WALLET, or derived from agent private key
        - signer: PACIFICA_<ACCOUNT>_AGENT_PRIVATE_KEY
        """
        account_name = request.get("account")
        main_address, signing_key, _addr_env, key_env, configured_agent_wallet, _master_private_key, searched = _resolve_credentials(account_name)
        if not main_address:
            return None, None, None, _execution_result(
                request,
                success=False,
                error=f"Missing Pacifica account address for account '{account_name}'. Searched: {', '.join(searched)}",
            )
        if not signing_key:
            return None, None, None, _execution_result(
                request,
                success=False,
                error=f"Missing Pacifica agent private key for account '{account_name}'. Searched: {', '.join(searched)}",
            )
        try:
            from solders.keypair import Keypair  # type: ignore

            derived_agent_wallet = str(Keypair.from_base58_string(signing_key).pubkey())
        except Exception as exc:
            return None, None, None, _execution_result(
                request,
                success=False,
                error=f"Invalid Pacifica agent private key for account '{account_name}' ({key_env or 'unknown env'}): {exc}",
            )
        agent_wallet = configured_agent_wallet or derived_agent_wallet
        if agent_wallet != derived_agent_wallet:
            segment = _account_segment(account_name) or ""
            return None, None, None, _execution_result(
                request,
                success=False,
                error=(
                    "Pacifica agent wallet mismatch for account '{account}': "
                    "PACIFICA_{segment}_AGENT_WALLET does not match the public key derived from "
                    "PACIFICA_{segment}_AGENT_PRIVATE_KEY."
                ).format(account=account_name, segment=segment),
            )
        return main_address, agent_wallet, signing_key, None


    def _ensure_market_info(self) -> None:
        if self.http_client is not None:
            return
        now = time.time()
        if self._market_info_initialized and now < self._market_info_expires_at:
            return
        try:
            raw = self._get("/api/v1/info", {})
        except Exception as exc:  # pragma: no cover
            self._market_info_error_count += 1
            self._market_info_last_error = str(exc)
            logger.warning("%s fetch failed: %s", self._market_info_log_prefix, exc)
            if self._market_info_error_count >= self._market_info_error_limit:
                self._market_info_disabled = True
                logger.error("%s disabled after repeated failures", self._market_info_log_prefix)
            return
        if not isinstance(raw, Mapping) or not isinstance(raw.get("data"), list):
            self._market_info_error_count += 1
            self._market_info_last_error = "invalid /api/v1/info response"
            logger.warning("%s invalid response shape: %s", self._market_info_log_prefix, raw)
            return
        markets: dict[str, dict[str, Any]] = {}
        tick_sizes: dict[str, Decimal] = {}
        lot_sizes: dict[str, Decimal] = {}
        min_notionals: dict[str, Decimal] = {}
        for item in raw["data"]:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if not symbol:
                continue
            try:
                tick = Decimal(str(item.get("tick_size")))
            except Exception:
                tick = self._tick_size_fallback
            try:
                lot = Decimal(str(item.get("lot_size")))
            except Exception:
                lot = self._lot_size_fallback
            try:
                min_notional = Decimal(str(item.get("min_order_size")))
            except Exception:
                min_notional = self._min_order_size_usd_fallback
            markets[symbol] = dict(item)
            tick_sizes[symbol] = tick
            lot_sizes[symbol] = lot
            min_notionals[symbol] = min_notional
        self._market_info_cache = markets
        self._tick_size_by_symbol = tick_sizes
        self._lot_size_by_symbol = lot_sizes
        self._min_notional_by_symbol = min_notionals
        self._market_info_initialized = True
        self._market_info_expires_at = now + self._market_info_ttl_seconds
        logger.info("Pacifica market info loaded %s symbols (ttl=%ss)", len(markets), self._market_info_ttl_seconds)

    def _round_to_step(self, value: Any, step: Decimal, *, mode: str = "floor") -> Optional[Decimal]:
        try:
            if value is None or step <= 0:
                return None
            d = Decimal(str(value))
            if mode == "ceil":
                return (-((-d) // step)) * step
            return (d // step) * step
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _format_decimal(d: Decimal) -> str:
        try:
            s = format(d, "f")
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s or "0"
        except Exception:
            return str(d)

    def _normalize_order_for_symbol(self, symbol: str, price: Any, amount: Any, *, side: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        sym = str(symbol or "").upper()
        self._ensure_market_info()
        tick = self._tick_size_by_symbol.get(sym, self._tick_size_fallback)
        lot = self._lot_size_by_symbol.get(sym, self._lot_size_fallback)
        min_notional = self._min_notional_by_symbol.get(sym, self._min_order_size_usd_fallback)
        side_lower = str(side or "").lower()
        price_mode = "floor" if side_lower == "buy" else "ceil"
        rounded_price = self._round_to_step(price, tick, mode=price_mode)
        rounded_amount = self._round_to_step(amount, lot, mode="floor")
        if rounded_price is None or rounded_amount is None:
            return None, None, "invalid price or amount"
        notional = rounded_price * rounded_amount
        try:
            if min_notional > 0 and notional < min_notional:
                return None, None, f"notional below minimum: {self._format_decimal(notional)} < {self._format_decimal(min_notional)}"
        except Exception:
            pass
        return self._format_decimal(rounded_price), self._format_decimal(rounded_amount), None

    def _balance(self, request: Mapping[str, Any]) -> dict:
        address, error = self._require_address(request)
        if error:
            return error
        raw = self._get("/api/v1/account", {"account": address})
        data = raw.get("data") if isinstance(raw, Mapping) else None
        # Best-effort: also fetch open positions so the Balance menu
        # can render them. Positions are wrapped into Hyperliquid-style
        # envelopes ({"position": {coin, szi, entryPx, ...}}) so the
        # exchange-agnostic _format_balance_message renderer reads
        # them without changes.
        positions_wrapped = self._fetch_positions_for_balance(address)
        if positions_wrapped and isinstance(raw, Mapping):
            exchange_response = dict(raw)
            exchange_response["positions"] = positions_wrapped
            exchange_response["assetPositions"] = positions_wrapped
        else:
            exchange_response = raw
        return _execution_result(
            request,
            success=bool(raw.get("success", True)) if isinstance(raw, Mapping) else False,
            exchange_response=exchange_response,
            balance=data if isinstance(data, Mapping) else {},
            positions=positions_wrapped,
        )

    def _fetch_positions_for_balance(self, address: str) -> list:
        """Fetch Pacifica open positions and wrap them into
        Hyperliquid-style envelopes so the exchange-agnostic balance
        renderer can display them. Returns an empty list on any
        failure (best-effort; the balance display must still succeed
        even if positions cannot be fetched).
        """
        try:
            raw = self._get("/api/v1/positions", {"account": address})
            raw_positions = raw.get("data") if isinstance(raw, Mapping) else []
            positions = [
                self._normalize_position(item)
                for item in raw_positions
            ] if isinstance(raw_positions, list) else []
        except Exception as exc:
            logger.warning(
                "Pacifica positions fetch failed for balance display on %s: %s",
                address, exc,
            )
            return []
        out: list = []
        for pos in positions:
            wrapped = self._wrap_pacifica_position_for_balance(pos)
            if wrapped is not None:
                out.append(wrapped)
        return out

    @staticmethod
    def _wrap_pacifica_position_for_balance(pos: Mapping[str, Any]) -> Optional[dict]:
        """Convert a normalized Pacifica position (Rise-style field
        names) into a Hyperliquid-style ``{"position": {...}}``
        envelope that the exchange-agnostic ``_format_balance_message``
        renderer can read without any exchange-specific branches.
        Returns ``None`` if the position has zero size.
        """
        try:
            size = float(pos.get("size") or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            return None
        side = str(pos.get("side") or "").lower()
        szi = size if side != "short" else -size
        symbol = str(pos.get("symbol") or "").upper()
        return {
            "position": {
                "coin": symbol,
                "szi": str(szi),
                "entryPx": pos.get("entry_price"),
                "unrealizedPnl": pos.get("unrealized_pnl"),
                "liquidationPx": pos.get("liquidation_price"),
            }
        }

    def _positions(self, request: Mapping[str, Any]) -> dict:
        address, error = self._require_address(request)
        if error:
            return error
        raw = self._get("/api/v1/positions", {"account": address})
        raw_positions = raw.get("data") if isinstance(raw, Mapping) else []
        positions = [self._normalize_position(item) for item in raw_positions] if isinstance(raw_positions, list) else []

        pnl_diag = self._apply_pacifica_pnl_enrichment(positions)
        tpsl_diag = self._apply_pacifica_tpsl_enrichment(positions, address or "")

        return _execution_result(
            request,
            success=bool(raw.get("success", True)) if isinstance(raw, Mapping) else False,
            positions=positions,
            position_count=len(positions),
            pnl_enrichment=pnl_diag,
            tpsl_enrichment=tpsl_diag,
            exchange_response=raw,
        )

    def _open_orders(self, request: Mapping[str, Any]) -> dict:
        address, error = self._require_address(request)
        if error:
            return error
        raw = self._get("/api/v1/orders", {"account": address})
        raw_orders = raw.get("data") if isinstance(raw, Mapping) else []
        orders = [self._normalize_order(item) for item in raw_orders] if isinstance(raw_orders, list) else []
        return _execution_result(
            request,
            success=bool(raw.get("success", True)) if isinstance(raw, Mapping) else False,
            orders=orders,
            open_order_count=len(orders),
            order_summary=self._summarize_orders_by_symbol_side(orders),
            exchange_response=raw,
        )

    @staticmethod
    def _pacifica_sanitize_error(exc: Exception) -> dict[str, Any]:
        text = str(exc)
        sensitive_keys = set(_PACIFICA_REDACTED_KEYS) | {"account", "address", "agent_wallet"}
        for key in sensitive_keys:
            text = re.sub(rf"(?i)({re.escape(key)}\s*[=:]\s*)[^\s,;}}]+", rf"\1{_PACIFICA_REDACTED_VALUE}", text)
        if len(text) > 300:
            text = text[:300] + "…"
        return {"error_type": exc.__class__.__name__, "error": text}

    @staticmethod
    def _pacifica_decimal(value: Any) -> Optional[Decimal]:
        try:
            if value is None:
                return None
            d = Decimal(str(value).strip())
            if not d.is_finite():
                return None
            return d
        except Exception:
            return None

    @staticmethod
    def _pacifica_decimal_string(value: Decimal) -> str:
        return str(value)

    @staticmethod
    def _normalize_pacifica_position_side(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"bid", "buy", "b", "long"}:
            return "long"
        if raw in {"ask", "sell", "a", "short"}:
            return "short"
        return raw

    @staticmethod
    def _normalize_pacifica_order_side(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"bid", "buy", "b", "long"}:
            return "buy"
        if raw in {"ask", "sell", "a", "short"}:
            return "sell"
        return raw

    @staticmethod
    def _normalize_pacifica_order_type(value: Any) -> str:
        raw = str(value or "").strip().lower()
        return re.sub(r"[\s\-]+", "_", raw)

    @classmethod
    def _pacifica_tpsl_leg(cls, order_type: Any) -> Optional[str]:
        normalized = cls._normalize_pacifica_order_type(order_type)
        if normalized in {"take_profit_market", "take_profit_limit"}:
            return "take_profit"
        if normalized in {"stop_loss_market", "stop_loss_limit"}:
            return "stop_loss"
        return None

    @classmethod
    def _pacifica_tpsl_rank(cls, order: Mapping[str, Any]) -> tuple[Any, ...]:
        def as_int(value: Any) -> int:
            try:
                if value in (None, ""):
                    return -1
                return int(str(value))
            except Exception:
                return -1
        order_id = order.get("order_id")
        client_order_id = order.get("client_order_id")
        return (
            as_int(order.get("updated_at")),
            as_int(order.get("created_at")),
            as_int(order_id),
            as_int(client_order_id),
            str(order_id or client_order_id or ""),
        )

    @classmethod
    def _classify_active_pacifica_tpsl(cls, order: Any) -> Optional[dict[str, Any]]:
        if not isinstance(order, Mapping):
            return None
        leg = cls._pacifica_tpsl_leg(order.get("order_type"))
        if leg is None:
            return None
        if not bool(order.get("reduce_only")):
            return None
        stop_price = order.get("stop_price")
        if cls._pacifica_decimal(stop_price) in (None, Decimal("0")):
            return None
        symbol = str(order.get("symbol") or "").strip().upper()
        if not symbol:
            return None
        order_side = cls._normalize_pacifica_order_side(order.get("side"))
        if order_side not in {"buy", "sell"}:
            return None
        return {
            "leg": leg,
            "symbol": symbol,
            "order_side": order_side,
            "trigger_price": str(stop_price),
            "rank": cls._pacifica_tpsl_rank(order),
        }

    @staticmethod
    def _pacifica_closing_order_side(position_side: Any) -> Optional[str]:
        side = str(position_side or "").lower()
        if side == "long":
            return "sell"
        if side == "short":
            return "buy"
        return None

    def _pacifica_market_info_rows(self) -> list[Mapping[str, Any]]:
        now = time.time()
        if self._market_info_initialized and now < self._market_info_expires_at and self._market_info_cache:
            return [dict(row) for row in self._market_info_cache.values()]
        raw = self._get("/api/v1/info", {})
        data = raw.get("data") if isinstance(raw, Mapping) else None
        if not isinstance(data, list):
            raise RuntimeError("invalid /api/v1/info response")
        rows = [dict(row) for row in data if isinstance(row, Mapping)]
        markets: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                markets[symbol] = dict(row)
        self._market_info_cache = markets
        self._market_info_initialized = True
        self._market_info_expires_at = now + self._market_info_ttl_seconds
        return rows

    def _pacifica_price_rows(self) -> list[Mapping[str, Any]]:
        raw = self._get("/api/v1/info/prices", {})
        data = raw.get("data") if isinstance(raw, Mapping) else None
        if not isinstance(data, list):
            raise RuntimeError("invalid /api/v1/info/prices response")
        return [dict(row) for row in data if isinstance(row, Mapping)]

    def _apply_pacifica_pnl_enrichment(self, positions: list[dict]) -> dict[str, Any]:
        diag: dict[str, Any] = {
            "success": False,
            "source": "info/prices",
            "market_info_source": "info",
            "positions_seen": len(positions),
            "positions_enriched": 0,
            "prices_inspected": 0,
            "market_info_symbols": 0,
            "calculation_model": None,
            "warnings": [],
            "errors": [],
        }
        try:
            market_rows = self._pacifica_market_info_rows()
            market_by_symbol = {str(row.get("symbol") or "").upper(): row for row in market_rows if str(row.get("symbol") or "").strip()}
            diag["market_info_symbols"] = len(market_by_symbol)
        except Exception as exc:
            diag["errors"].append({"stage": "fetch_market_info", **self._pacifica_sanitize_error(exc)})
            return diag
        try:
            price_rows = self._pacifica_price_rows()
            prices_by_symbol = {str(row.get("symbol") or "").upper(): row for row in price_rows if str(row.get("symbol") or "").strip()}
            diag["prices_inspected"] = len(price_rows)
        except Exception as exc:
            diag["errors"].append({"stage": "fetch_prices", **self._pacifica_sanitize_error(exc)})
            return diag

        enriched = 0
        for position in positions:
            symbol = str(position.get("symbol") or "").upper()
            if not symbol:
                diag["warnings"].append({"symbol": None, "reason": "missing_symbol"})
                continue
            market = market_by_symbol.get(symbol)
            price = prices_by_symbol.get(symbol)
            if not isinstance(market, Mapping):
                diag["warnings"].append({"symbol": symbol, "reason": "missing_market_metadata"})
                continue
            if str(market.get("instrument_type") or "").strip().lower() != "perpetual":
                diag["warnings"].append({"symbol": symbol, "reason": "unsupported_instrument_type"})
                continue
            if not isinstance(price, Mapping):
                diag["warnings"].append({"symbol": symbol, "reason": "missing_price_row"})
                continue
            mark_raw = price.get("mark")
            mark = self._pacifica_decimal(mark_raw)
            if mark is None:
                diag["warnings"].append({"symbol": symbol, "reason": "invalid_mark"})
                continue
            position["mark_price"] = str(mark_raw)
            size = self._pacifica_decimal(position.get("size"))
            entry = self._pacifica_decimal(position.get("entry_price"))
            side = self._normalize_pacifica_position_side(position.get("side"))
            if size is None:
                position["position_value"] = None
                position["unrealized_pnl"] = None
                diag["warnings"].append({"symbol": symbol, "reason": "invalid_size"})
                continue
            if entry is None:
                position["position_value"] = None
                position["unrealized_pnl"] = None
                diag["warnings"].append({"symbol": symbol, "reason": "invalid_entry_price"})
                continue
            if side not in {"long", "short"}:
                position["position_value"] = None
                position["unrealized_pnl"] = None
                diag["warnings"].append({"symbol": symbol, "reason": "unknown_side"})
                continue
            abs_size = abs(size)
            position["position_value"] = self._pacifica_decimal_string(abs_size * mark)
            pnl = (mark - entry) * abs_size if side == "long" else (entry - mark) * abs_size
            position["unrealized_pnl"] = self._pacifica_decimal_string(pnl)
            enriched += 1
        diag["positions_enriched"] = enriched
        diag["success"] = not diag["errors"]
        if enriched:
            diag["calculation_model"] = "linear_usdc_perpetual"
        return diag

    def _apply_pacifica_tpsl_enrichment(self, positions: list[dict], account: str) -> dict[str, Any]:
        diag: dict[str, Any] = {
            "success": False,
            "source": "orders",
            "active_orders_inspected": 0,
            "eligible_trigger_count": 0,
            "matched_position_count": 0,
            "selected_trigger_count": 0,
            "warnings": [],
            "errors": [],
        }
        try:
            raw = self._get("/api/v1/orders", {"account": account})
            raw_orders = raw.get("data") if isinstance(raw, Mapping) else None
            if not isinstance(raw_orders, list):
                raise RuntimeError("invalid /api/v1/orders response")
        except Exception as exc:
            diag["errors"].append({"stage": "fetch_orders", **self._pacifica_sanitize_error(exc)})
            return diag
        diag["active_orders_inspected"] = len(raw_orders)
        selected: dict[tuple[str, str, str], dict[str, Any]] = {}
        eligible_count = 0
        for order in raw_orders:
            candidate = self._classify_active_pacifica_tpsl(order)
            if candidate is None:
                continue
            eligible_count += 1
            for position in positions:
                symbol = str(position.get("symbol") or "").upper()
                position_side = self._normalize_pacifica_position_side(position.get("side"))
                closing_side = self._pacifica_closing_order_side(position_side)
                if symbol != candidate["symbol"] or closing_side != candidate["order_side"]:
                    continue
                key = (symbol, position_side, candidate["leg"])
                current = selected.get(key)
                if current is None or candidate["rank"] > current["rank"]:
                    selected[key] = candidate
        diag["eligible_trigger_count"] = eligible_count
        matched_positions: set[tuple[str, str]] = set()
        for position in positions:
            symbol = str(position.get("symbol") or "").upper()
            position_side = self._normalize_pacifica_position_side(position.get("side"))
            for leg in ("take_profit", "stop_loss"):
                candidate = selected.get((symbol, position_side, leg))
                if candidate is not None:
                    position[leg] = candidate["trigger_price"]
                    matched_positions.add((symbol, position_side))
        diag["matched_position_count"] = len(matched_positions)
        diag["selected_trigger_count"] = len(selected)
        diag["success"] = True
        return diag

    def _cancel_orders(self, request: Mapping[str, Any]) -> dict:
        return _execution_result(request, success=False, error="Pacifica cancel_orders not yet implemented in this snapshot")

    def _batch_orders(self, request: Mapping[str, Any]) -> dict:
        main_address, agent_wallet, private_key, error = (
            self._require_trading_credentials(request)
        )
        if error:
            return error
        child_orders = request.get("child_orders") or []
        try:
            count = len(child_orders) if isinstance(child_orders, list) else 0
            first = child_orders[0] if count else None
            last = child_orders[-1] if count else None
            logger.info(
                "Pacifica batch_orders preflight account=%s parent_operation=%s child_count=%s first_child=%s last_child=%s",
                request.get("account"),
                request.get("parent_operation"),
                count,
                json.dumps(first, default=str, ensure_ascii=False) if first else None,
                json.dumps(last, default=str, ensure_ascii=False) if last else None,
            )
        except Exception:
            logger.exception("Pacifica batch_orders preflight logging failed")
        if not isinstance(child_orders, list) or not child_orders:
            return _execution_result(request, success=False, error="Pacifica batch_orders requires non-empty child_orders")

        child_results: list[dict[str, Any]] = []
        batch_responses: list[dict[str, Any]] = []
        chunks = [child_orders[i : i + PACIFICA_BATCH_LIMIT] for i in range(0, len(child_orders), PACIFICA_BATCH_LIMIT)]
        for chunk in chunks:
            actions: list[dict[str, Any]] = []
            for child in chunk:
                actions.append(self._create_order_action(
                                    request,
                                    child,
                                    main_address or "",
                                    agent_wallet or "",
                                    private_key or "",
                                ))
            raw = self._post("/api/v1/orders/batch", {"actions": actions})
            batch_responses.append(raw)
            results = self._extract_batch_results(raw)
            top_error = raw.get("error") if isinstance(raw, Mapping) else None
            for idx, child in enumerate(chunk):
                raw_child = results[idx] if idx < len(results) and isinstance(results[idx], Mapping) else {}
                success = bool(raw_child.get("success")) if raw_child else False
                error_text = raw_child.get("error") or top_error
                child_results.append(
                    {
                        "child_id": child.get("child_id") if isinstance(child, Mapping) else None,
                        "success": success,
                        "order_id": raw_child.get("order_id"),
                        "client_order_id": raw_child.get("client_order_id"),
                        "symbol": raw_child.get("symbol") or (child.get("symbol") if isinstance(child, Mapping) else None),
                        "side": child.get("side") if isinstance(child, Mapping) else None,
                        "size": child.get("size") if isinstance(child, Mapping) else None,
                        "price": child.get("price") if isinstance(child, Mapping) else None,
                        "error": error_text,
                    }
                )

        submitted_count = sum(1 for child in child_results if child.get("success"))
        failed = [child for child in child_results if not child.get("success")]
        return _execution_result(
            request,
            success=not failed and submitted_count == len(child_orders),
            error=(failed[0].get("error") or "One or more Pacifica child orders failed") if failed else None,
            child_results=child_results,
            submitted_count=submitted_count,
            exchange_response={"batches": batch_responses},
        )

    def _set_tp_sl(self, request: Mapping[str, Any]) -> dict:
        main_address, agent_wallet, private_key, error = self._require_tpsl_credentials(request)
        if error:
            return error
        payload, err = build_tpsl_payload(request)
        if err:
            return _execution_result(request, success=False, error=err)
        if payload is None:
            return _execution_result(request, success=False, error="Pacifica TP/SL payload was empty")

        position_error = self._verify_tpsl_position_exists(request, main_address or "", payload)
        if position_error:
            return position_error
        if self._is_zero_tpsl_payload(payload):
            return self._remove_tpsl_leg(request, main_address or "", agent_wallet or "", private_key or "", payload)

        timestamp = int(self.now_ms())
        header = {"timestamp": timestamp, "expiry_window": PACIFICA_EXPIRY_WINDOW_MS, "type": "set_position_tpsl"}

        # Critical Pacifica TP/SL agent-key signing rule:
        # Sign only the operation data inside the standard Pacifica signing
        # envelope {timestamp, expiry_window, type, data}. Do not include
        # account, agent_wallet, or signature inside signed data.
        sign_payload: dict[str, Any] = {"symbol": payload.symbol, "side": payload.side}
        if payload.take_profit is not None:
            sign_payload["take_profit"] = payload.take_profit
        if payload.stop_loss is not None:
            sign_payload["stop_loss"] = payload.stop_loss

        signature = self.signer.sign(header, sign_payload, private_key or "")
        body = {
            "account": main_address or "",
            "agent_wallet": agent_wallet or "",
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": PACIFICA_EXPIRY_WINDOW_MS,
            **sign_payload,
        }
        logger.info(
            "Pacifica TP/SL preflight account=%s agent_wallet=%s symbol=%s side=%s payload=%s",
            _mask_public_value(main_address),
            _mask_public_value(agent_wallet),
            payload.symbol,
            payload.side,
            json.dumps(_redact_pacifica_sensitive(body), ensure_ascii=False),
        )
        try:
            raw = self._post("/api/v1/positions/tpsl", body)
        except urllib.error.HTTPError as exc:  # pragma: no cover
            error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            sanitized_payload = json.dumps(_redact_pacifica_sensitive(body), ensure_ascii=False)
            logger.error(
                "Pacifica HTTPError path=/api/v1/positions/tpsl status=%s reason=%s payload=%s body=%s",
                exc.code,
                exc.reason,
                sanitized_payload,
                error_body,
            )
            return _execution_result(
                request,
                success=False,
                error=f"HTTP Error {exc.code}: {exc.reason}",
                exchange_response={"status": exc.code, "reason": exc.reason, "body": error_body, "payload": _redact_pacifica_sensitive(body)},
            )
        success = bool(raw.get("success", True)) if isinstance(raw, Mapping) else False
        return _execution_result(
            request,
            success=success,
            error=(raw.get("error") if isinstance(raw, Mapping) and not success else None),
            exchange_response=raw,
        )

    @staticmethod
    def _is_zero_tpsl_payload(payload: PacificaTpslPayload) -> bool:
        leg = payload.take_profit if payload.take_profit is not None else payload.stop_loss
        if not isinstance(leg, Mapping):
            return False
        try:
            return Decimal(str(leg.get("stop_price"))) == 0
        except Exception:
            return False

    def _remove_tpsl_leg(self, request: Mapping[str, Any], account: str, agent_wallet: str, private_key: str, payload: PacificaTpslPayload) -> dict:
        leg_name = "take_profit" if payload.take_profit is not None else "stop_loss"
        order_type_prefix = "take_profit" if leg_name == "take_profit" else "stop_loss"
        raw = self._get("/api/v1/orders", {"account": account})
        raw_orders = raw.get("data") if isinstance(raw, Mapping) else []
        if not isinstance(raw_orders, list):
            return _execution_result(request, success=False, error="Pacifica open-orders preflight returned invalid data")
        candidates: list[Mapping[str, Any]] = []
        for item in raw_orders:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("symbol") or "").upper() != payload.symbol.upper():
                continue
            if not bool(item.get("reduce_only")):
                continue
            order_type = str(item.get("order_type") or "").lower()
            if not order_type.startswith(order_type_prefix):
                continue
            candidates.append(item)
        if not candidates:
            logger.info(
                "Pacifica TP/SL remove no existing leg account=%s symbol=%s leg=%s",
                _mask_public_value(account),
                payload.symbol,
                leg_name,
            )
            return _execution_result(request, success=True, exchange_response={"success": True, "removed": 0, "reason": "no existing leg"})

        responses: list[Any] = []
        for item in candidates:
            order_id = item.get("order_id")
            timestamp = int(self.now_ms())
            header = {"timestamp": timestamp, "expiry_window": PACIFICA_EXPIRY_WINDOW_MS, "type": "cancel_stop_order"}
            sign_payload = {"symbol": payload.symbol, "order_id": order_id}
            signature = self.signer.sign(header, sign_payload, private_key)
            body = {
                "account": account,
                "agent_wallet": agent_wallet,
                "signature": signature,
                "timestamp": timestamp,
                "expiry_window": PACIFICA_EXPIRY_WINDOW_MS,
                **sign_payload,
            }
            logger.info(
                "Pacifica TP/SL remove preflight account=%s agent_wallet=%s symbol=%s order_id=%s payload=%s",
                _mask_public_value(account),
                _mask_public_value(agent_wallet),
                payload.symbol,
                order_id,
                json.dumps(_redact_pacifica_sensitive(body), ensure_ascii=False),
            )
            try:
                cancel_raw = self._post("/api/v1/orders/stop/cancel", body)
            except urllib.error.HTTPError as exc:  # pragma: no cover
                error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                logger.error(
                    "Pacifica HTTPError path=/api/v1/orders/stop/cancel status=%s reason=%s payload=%s body=%s",
                    exc.code,
                    exc.reason,
                    json.dumps(_redact_pacifica_sensitive(body), ensure_ascii=False),
                    error_body,
                )
                return _execution_result(
                    request,
                    success=False,
                    error=f"HTTP Error {exc.code}: {exc.reason}",
                    exchange_response={"status": exc.code, "reason": exc.reason, "body": error_body, "payload": _redact_pacifica_sensitive(body)},
                )
            responses.append(cancel_raw)
        failed = [r for r in responses if isinstance(r, Mapping) and r.get("success") is False]
        return _execution_result(
            request,
            success=not failed,
            error=(failed[0].get("error") if failed and isinstance(failed[0], Mapping) else None),
            exchange_response={"success": not failed, "removed": len(responses), "responses": responses},
        )

    def _verify_tpsl_position_exists(self, request: Mapping[str, Any], account: str, payload: PacificaTpslPayload) -> Optional[dict]:
        try:
            raw = self._get("/api/v1/positions", {"account": account})
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "Pacifica TP/SL position preflight failed account=%s symbol=%s side=%s error=%s",
                _mask_public_value(account),
                payload.symbol,
                payload.side,
                exc,
            )
            return None
        raw_positions = raw.get("data") if isinstance(raw, Mapping) else []
        if not isinstance(raw_positions, list):
            return _execution_result(request, success=False, error="Pacifica positions preflight returned invalid data")
        wanted_symbol = payload.symbol.upper()
        request_position = _request_field(request, "position")
        if not isinstance(request_position, Mapping):
            request_position = {}
        position_side_raw = (
            (request_position.get("side") if isinstance(request_position, Mapping) else None)
            or _request_field(request, "side")
        )
        wanted_position_side = self._normalize_side(position_side_raw)
        for item in raw_positions:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol") or "").upper()
            side = self._normalize_side(item.get("side"))
            if symbol == wanted_symbol and side == wanted_position_side:
                return None
        logger.warning(
            "Pacifica TP/SL position preflight no match account=%s symbol=%s position_side=%s order_side=%s position_count=%s",
            _mask_public_value(account),
            wanted_symbol,
            wanted_position_side,
            payload.side,
            len(raw_positions),
        )
        return _execution_result(
            request,
            success=False,
            error=f"Pacifica position not found before TP/SL submit: {wanted_symbol} {position_side_raw}",
            exchange_response={"preflight_positions_count": len(raw_positions)},
        )

    def _create_order_action(
        self,
        request: Mapping[str, Any],
        child: Mapping[str, Any],
        account: str,
        agent_wallet: str,
        private_key: str,
    ) -> dict:
        """Build a single Create / CreateMarket action with its signature.

        ``agent_wallet`` is the public key derived from the configured
        agent private key. Pacifica's verifier reconstructs the canonical
        signature against this key (because the request declared the
        agent as the signer); without it, the verifier falls back to
        the main account's keypair and rejects the request when the
        signing keypair differs from the main account keypair.

        The canonical payload that gets signed is ``{header, data: payload}``
        where ``payload`` is the order field set ONLY — ``agent_wallet``
        is NOT inside the signed region (per the official Pacifica SDK's
        ``api_agent_keys.py`` example, where ``agent_wallet`` is added
        to ``request_header`` AFTER signing). Pacifica picks the
        verification key from the outbound body's ``agent_wallet`` field,
        not from the signed payload.
        """
        order_type = str(child.get("order_type") or "limit").lower()
        timestamp = int(self.now_ms())
        side = "bid" if str(child.get("side") or "").lower() == "buy" else "ask"
        payload = {
            "symbol": str(child.get("symbol") or request.get("symbol") or "").upper(),
            "reduce_only": bool(child.get("reduce_only", False)),
            "amount": str(child.get("size")),
            "side": side,
        }
        action_type = "CreateMarket" if order_type == "market" else "Create"
        if action_type == "Create":
            payload["price"] = str(child.get("price"))
            payload["tif"] = str(child.get("tif") or "GTC").upper()
            payload["client_order_id"] = str(child.get("client_order_id") or uuid.uuid4())
            signing_type = "create_order"
        else:
            payload["slippage_percent"] = str(child.get("slippage_percent") or "0.5")
            payload["client_order_id"] = str(child.get("client_order_id") or uuid.uuid4())
            signing_type = "create_market_order"
        header = {"timestamp": timestamp, "expiry_window": PACIFICA_EXPIRY_WINDOW_MS, "type": signing_type}
        signature = self.signer.sign(header, payload, private_key or "")
        data = {
            "account": account,
            "agent_wallet": agent_wallet,  # declared so the verifier uses the agent keypair
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": PACIFICA_EXPIRY_WINDOW_MS,
            **payload,
        }
        return {"type": action_type, "data": data}

    def _get(self, path: str, params: Mapping[str, Any]) -> dict:
        if self.http_client is not None:
            return self.http_client.get(path, dict(params))
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
        request = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": "Hermes-TradeDesk/1.0"}, method="GET")
        with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
            body = response.read().decode("utf-8")
        return json.loads(body)

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict:
        if self.http_client is not None:
            return self.http_client.post(path, dict(payload))
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Accept": "*/*", "Content-Type": "application/json", "User-Agent": "Hermes-TradeDesk/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
                response_body = response.read().decode("utf-8")
            return json.loads(response_body)
        except urllib.error.HTTPError as exc:
            # Observability-only path: preserve the exchange response
            # body, headers, status, parsed JSON, and a redacted
            # user-facing message. We never mutate request semantics
            # here — amount, price, side, tif, endpoint, signature,
            # and expiry window remain exactly as produced by the
            # pre-observability implementation.
            diagnostics = _capture_pacifica_http_error(exc)
            # Replace the bare urllib HTTPError with our subclass that
            # carries the diagnostics, so caller exception-handlers
            # (``logger.exception("Pacifica %s failed", ...)`` plus
            # ``error=str(exc)``) still work and trade-menu / tradedesk
            # rendering can access ``.diagnostics`` for richer output.
            raise PacificaHTTPError(exc, diagnostics) from None

    @staticmethod
    def _extract_batch_results(raw: Any) -> list[Any]:
        if not isinstance(raw, Mapping):
            return []
        data = raw.get("data")
        if isinstance(data, Mapping):
            results = data.get("results")
            return results if isinstance(results, list) else []
        return []

    @staticmethod
    def _normalize_side(side: Any) -> str:
        raw = str(side or "").lower()
        if raw in {"bid", "buy", "b", "long"}:
            return "buy"
        if raw in {"ask", "sell", "a", "short"}:
            return "sell"
        return raw

    @classmethod
    def _normalize_position(cls, item: Any) -> dict:
        if not isinstance(item, Mapping):
            return {"raw": item}
        side = cls._normalize_side(item.get("side"))
        position_side = "long" if side == "buy" else "short" if side == "sell" else side
        return {
            "symbol": item.get("symbol"),
            "side": position_side,
            "size": item.get("amount"),
            "entry_price": item.get("entry_price"),
            "margin": item.get("margin"),
            "funding": item.get("funding"),
            "isolated": item.get("isolated"),
            "liquidation_price": item.get("liquidation_price"),
            "mark_price": None,
            "position_value": None,
            "unrealized_pnl": None,
            "take_profit": None,
            "stop_loss": None,
            "raw": dict(item),
        }

    @classmethod
    def _normalize_order(cls, item: Any) -> dict:
        if not isinstance(item, Mapping):
            return {"raw": item}
        side = cls._normalize_side(item.get("side"))
        return {
            "order_id": item.get("order_id"),
            "client_order_id": item.get("client_order_id"),
            "symbol": item.get("symbol"),
            "side": side,
            "price": item.get("price"),
            "size": item.get("initial_amount"),
            "filled_size": item.get("filled_amount"),
            "cancelled_size": item.get("cancelled_amount"),
            "order_type": item.get("order_type"),
            "reduce_only": item.get("reduce_only"),
            "raw": dict(item),
        }

    @staticmethod
    def _summarize_orders_by_symbol_side(orders: list[dict]) -> list[dict]:
        summary: dict[str, dict[str, Any]] = {}
        for order in orders:
            symbol = str(order.get("symbol") or "UNKNOWN").upper()
            item = summary.setdefault(symbol, {"symbol": symbol, "buy": 0, "sell": 0})
            side = str(order.get("side") or "").lower()
            if side == "buy":
                item["buy"] += 1
            elif side == "sell":
                item["sell"] += 1
        return sorted(summary.values(), key=lambda row: str(row.get("symbol") or ""))
