"""
builder_auth.py — Polymarket Builder Program authentication.

Handles:
- Builder API key credential storage (env vars: POLYMARKET_BUILDER_ID, POLYMARKET_BUILDER_SIGNATURE,
  and legacy POLY_BUILDER_API_KEY / POLY_BUILDER_SECRET / POLY_BUILDER_PASSPHRASE)
- Request signing (HMAC-SHA256 per Polymarket spec)
- Header injection for CLOB client:
    X-Builder-Id: <builder_id>
    X-Builder-Signature: <sig>
- Relayer client initialization for gasless transactions

Environment variables (choose one style):
  Simple (new):
    POLYMARKET_BUILDER_ID          — your builder wallet address / ID
    POLYMARKET_BUILDER_SIGNATURE   — pre-computed signature (static, from builder portal)

  Full HMAC (advanced):
    POLY_BUILDER_API_KEY           — builder API key
    POLY_BUILDER_SECRET            — HMAC signing secret (base64-encoded)
    POLY_BUILDER_PASSPHRASE        — API passphrase

Both styles are supported; POLYMARKET_BUILDER_ID takes priority for the X-Builder-Id header.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass
class BuilderCreds:
    """Builder API credentials loaded from environment.

    Supports two authentication styles:
      1. Simple: POLYMARKET_BUILDER_ID + POLYMARKET_BUILDER_SIGNATURE (static header)
      2. Full HMAC: POLY_BUILDER_API_KEY + POLY_BUILDER_SECRET + POLY_BUILDER_PASSPHRASE
    """
    # Simple env-var style (X-Builder-Id / X-Builder-Signature)
    builder_id: str = ""
    builder_signature: str = ""

    # Full HMAC style (legacy / advanced)
    key: str = ""
    secret: str = ""
    passphrase: str = ""

    @classmethod
    def from_env(cls) -> "BuilderCreds":
        # Simple style (new, preferred for basic attribution)
        builder_id = os.environ.get("POLYMARKET_BUILDER_ID", "")
        builder_sig = os.environ.get("POLYMARKET_BUILDER_SIGNATURE", "")

        # Full HMAC style
        key = os.environ.get("POLY_BUILDER_API_KEY", "")
        secret = os.environ.get("POLY_BUILDER_SECRET", "")
        passphrase = os.environ.get("POLY_BUILDER_PASSPHRASE", "")

        # If only full HMAC style is set, derive builder_id from key
        if not builder_id and key:
            builder_id = key

        return cls(
            builder_id=builder_id,
            builder_signature=builder_sig,
            key=key,
            secret=secret,
            passphrase=passphrase,
        )

    @property
    def configured(self) -> bool:
        """True if at least builder_id is set (minimum for attribution)."""
        return bool(self.builder_id)

    @property
    def hmac_configured(self) -> bool:
        """True if full HMAC credentials are available for dynamic signing."""
        return bool(self.key and self.secret and self.passphrase)


def _sign_message(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """
    HMAC-SHA256 signature per Polymarket builder signing spec.
    message = timestamp + method.upper() + path + body
    """
    message = timestamp + method.upper() + path + body
    try:
        secret_bytes = base64.b64decode(secret)
    except Exception:
        # If not valid base64, use raw bytes
        secret_bytes = secret.encode("utf-8")
    sig = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("utf-8")


def build_builder_headers(
    creds: BuilderCreds,
    method: str = "POST",
    path: str = "/order",
    body: str = "",
) -> dict[str, str]:
    """
    Generate the builder authentication headers required by Polymarket CLOB.

    Primary headers (always set when builder_id is available):
      X-Builder-Id:        <builder_id>   — your builder wallet address / profile ID
      X-Builder-Signature: <sig>          — static sig from env OR HMAC-computed

    Additional HMAC headers (set when full HMAC creds are available):
      POLY_ADDRESS:     <key>
      POLY_SIGNATURE:   <hmac_sig>
      POLY_TIMESTAMP:   <unix_ts>
      POLY_PASSPHRASE:  <passphrase>

    Returns headers dict to merge into any CLOB request.
    """
    if not creds.configured:
        return {}

    headers: dict[str, str] = {}

    # ── Primary builder attribution headers ───────────────────────────────────
    headers["X-Builder-Id"] = creds.builder_id

    if creds.builder_signature:
        # Static signature from env (simplest setup)
        headers["X-Builder-Signature"] = creds.builder_signature
    elif creds.hmac_configured:
        # Dynamically compute HMAC signature
        timestamp = str(int(time.time()))
        sig = _sign_message(creds.secret, timestamp, method, path, body)
        headers["X-Builder-Signature"] = sig

    # ── Full HMAC auth headers (for advanced CLOB auth) ───────────────────────
    if creds.hmac_configured:
        timestamp = str(int(time.time()))
        signature = _sign_message(creds.secret, timestamp, method, path, body)
        headers["POLY_ADDRESS"] = creds.key
        headers["POLY_SIGNATURE"] = signature
        headers["POLY_TIMESTAMP"] = timestamp
        headers["POLY_PASSPHRASE"] = creds.passphrase

    return headers


def init_clob_client_with_builder(config, creds: Optional[BuilderCreds] = None):
    """
    Initialize py-clob-client with builder attribution baked in.

    Falls back to non-builder client if builder creds not configured.
    Returns (client, is_builder) tuple.
    """
    try:
        from py_clob_client.client import ClobClient  # type: ignore

        client = ClobClient(
            host=config.clob_host,
            key=config.private_key,
            chain_id=config.chain_id,
            signature_type=config.signature_type,
            funder=config.funder_address,
        )
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)

        # Inject builder headers if configured
        if creds and creds.configured:
            _patch_client_with_builder_headers(client, creds)
            print(f"[builder_auth] CLOB client initialized with Builder attribution ✓")
            print(f"[builder_auth] Builder ID: {creds.builder_id[:12]}...")
            return client, True
        else:
            print("[builder_auth] Builder creds not set — using standard CLOB client")
            return client, False

    except ImportError:
        print("[builder_auth] py-clob-client not installed — dry-run mode")
        return None, False
    except Exception as e:
        print(f"[builder_auth] CLOB client init failed: {e} — dry-run mode")
        return None, False


def _patch_client_with_builder_headers(client, creds: BuilderCreds) -> None:
    """
    Monkey-patch the CLOB client's HTTP session to inject builder headers
    on every outbound request.

    Injects:
      X-Builder-Id:        <builder_id>
      X-Builder-Signature: <sig>
      (+ POLY_* HMAC headers if full creds are set)

    py-clob-client uses a requests.Session internally; we add a custom
    request hook to prepend builder auth headers automatically.
    """
    try:
        session = client.session  # requests.Session

        class BuilderHeaderAdapter:
            """Wraps the session's send() to inject builder headers."""
            def __init__(self, original_send, builder_creds: BuilderCreds):
                self._send = original_send
                self._creds = builder_creds

            def __call__(self, request, **kwargs):
                # Extract path from URL for signing
                from urllib.parse import urlparse
                parsed = urlparse(request.url)
                path = parsed.path
                body = request.body.decode() if isinstance(request.body, bytes) else (request.body or "")
                headers = build_builder_headers(
                    self._creds,
                    method=request.method,
                    path=path,
                    body=body,
                )
                request.headers.update(headers)
                return self._send(request, **kwargs)

        session.send = BuilderHeaderAdapter(session.send, creds)
        print("[builder_auth] Builder headers (X-Builder-Id, X-Builder-Signature) patched into CLOB session ✓")

    except AttributeError:
        # Newer versions may use httpx or different internals
        print("[builder_auth] Could not patch session — builder headers will be set per-request")


def init_relayer_client(config, creds: Optional[BuilderCreds] = None):
    """
    Initialize the Polymarket Relayer Client for gasless transactions.

    Relayer handles: wallet deployment, USDC approvals, CTF operations.
    Reference: https://docs.polymarket.com/builders/overview

    Returns relayer client or None if not available.
    """
    try:
        from py_clob_client.client import ClobClient  # type: ignore

        # Relayer endpoint (separate from CLOB)
        relayer_host = os.environ.get(
            "POLY_RELAYER_HOST", "https://relayer.polymarket.com"
        )

        # The relayer client shares credentials with the CLOB client
        # but targets the relayer endpoint for gasless ops
        relayer = ClobClient(
            host=relayer_host,
            key=config.private_key,
            chain_id=config.chain_id,
            signature_type=config.signature_type,
            funder=config.funder_address,
        )

        if creds and creds.configured:
            _patch_client_with_builder_headers(relayer, creds)

        print(f"[builder_auth] Relayer client initialized at {relayer_host} ✓")
        return relayer

    except ImportError:
        print("[builder_auth] Relayer client unavailable (py-clob-client not installed)")
        return None
    except Exception as e:
        print(f"[builder_auth] Relayer init failed: {e}")
        return None
