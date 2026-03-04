"""
builder_auth.py — Polymarket Builder Program authentication.

Uses the official py-builder-signing-sdk (PyPI v0.0.2):
  https://github.com/Polymarket/py-builder-signing-sdk

  from py_builder_signing_sdk import BuilderConfig, BuilderApiKeyCreds

Correct header names (BuilderHeaderPayload from sdk_types.py):
  POLY_BUILDER_API_KEY
  POLY_BUILDER_TIMESTAMP
  POLY_BUILDER_PASSPHRASE
  POLY_BUILDER_SIGNATURE

Signing spec (py_builder_signing_sdk/signing/hmac.py):
  base64_secret = base64.urlsafe_b64decode(secret)   ← urlsafe, NOT standard
  message = timestamp + method + path [+ body.replace("'", '"')]
  sig = hmac.new(base64_secret, message.encode("utf-8"), sha256).digest()
  return base64.urlsafe_b64encode(sig).decode("utf-8")

Environment variables:
  POLY_BUILDER_API_KEY    — builder API key
  POLY_BUILDER_SECRET     — HMAC signing secret (URL-safe base64-encoded)
  POLY_BUILDER_PASSPHRASE — API passphrase
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import os
import time
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Official SDK import ───────────────────────────────────────────────────────
# PyPI: pip install py-builder-signing-sdk==0.0.2
# The package __init__.py re-exports BuilderConfig and BuilderApiKeyCreds at
# the top level per the official README usage:
#   from py_builder_signing_sdk import BuilderConfig, BuilderApiKeyCreds
try:
    from py_builder_signing_sdk import BuilderConfig, BuilderApiKeyCreds  # type: ignore
    _SDK_AVAILABLE = True
except ImportError:
    BuilderConfig = None  # type: ignore
    BuilderApiKeyCreds = None  # type: ignore
    _SDK_AVAILABLE = False

# Module-level cached BuilderConfig — reset via _reset_builder_config() in tests
_builder_config: Optional[object] = None


# ── Credentials dataclass ─────────────────────────────────────────────────────

@dataclass
class BuilderCreds:
    """
    Builder API credentials loaded from environment.

    Only one auth flow: key/secret/passphrase HMAC signing.
    There is NO "simple static signature" mode — that was a fabrication.

    Reference: https://github.com/Polymarket/py-builder-signing-sdk
    """
    key: str = ""
    secret: str = ""
    passphrase: str = ""

    @classmethod
    def from_env(cls) -> "BuilderCreds":
        return cls(
            key=os.environ.get("POLY_BUILDER_API_KEY", ""),
            secret=os.environ.get("POLY_BUILDER_SECRET", ""),
            passphrase=os.environ.get("POLY_BUILDER_PASSPHRASE", ""),
        )

    @property
    def configured(self) -> bool:
        """True if all three HMAC credentials are set."""
        return bool(self.key and self.secret and self.passphrase)


# ── Public API ────────────────────────────────────────────────────────────────

def is_builder_enabled() -> bool:
    """Return True if builder credentials are fully configured in the environment."""
    return BuilderCreds.from_env().configured


def load_builder_config(creds: Optional[BuilderCreds] = None) -> Optional[object]:
    """
    Load and cache a BuilderConfig from the official SDK.

    Returns None if SDK not installed or credentials not configured.
    Tests reset the cache with: import builder_auth; builder_auth._builder_config = None
    """
    global _builder_config
    if _builder_config is not None:
        return _builder_config

    c = creds or BuilderCreds.from_env()
    if not c.configured:
        return None

    if not _SDK_AVAILABLE:
        print(
            "[builder_auth] WARNING: py-builder-signing-sdk not installed.\n"
            "  Run: pip install py-builder-signing-sdk==0.0.2\n"
            "  Falling back to built-in HMAC signing."
        )
        return None

    try:
        sdk_creds = BuilderApiKeyCreds(
            key=c.key,
            secret=c.secret,
            passphrase=c.passphrase,
        )
        _builder_config = BuilderConfig(local_builder_creds=sdk_creds)
        return _builder_config
    except Exception as e:
        print(f"[builder_auth] Failed to create BuilderConfig: {e}")
        return None


def _reset_builder_config() -> None:
    """Reset cached config singleton — used in tests."""
    global _builder_config
    _builder_config = None


def generate_builder_headers(
    config_or_creds,
    method: str = "POST",
    path: str = "/order",
    body: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> dict:
    """
    Generate official builder authentication headers for Polymarket CLOB.

    Accepts:
      - BuilderConfig (SDK object from load_builder_config) → uses SDK signing
      - BuilderCreds dataclass → built-in fallback HMAC (SDK not installed)
      - None → returns empty dict

    Correct header names (BuilderHeaderPayload):
      POLY_BUILDER_API_KEY, POLY_BUILDER_TIMESTAMP,
      POLY_BUILDER_PASSPHRASE, POLY_BUILDER_SIGNATURE

    NOT the old broken headers: X-Builder-Id, X-Builder-Signature,
    POLY_ADDRESS, POLY_SIGNATURE — none of these are recognised by Polymarket.
    """
    if config_or_creds is None:
        return {}

    ts = timestamp if timestamp is not None else int(time.time())

    # Path 1: Official SDK BuilderConfig.generate_builder_headers()
    if _SDK_AVAILABLE and BuilderConfig is not None and isinstance(config_or_creds, BuilderConfig):
        try:
            payload = config_or_creds.generate_builder_headers(method, path, body, timestamp=ts)
            if payload is None:
                return {}
            return {
                "POLY_BUILDER_API_KEY": payload.POLY_BUILDER_API_KEY,
                "POLY_BUILDER_TIMESTAMP": payload.POLY_BUILDER_TIMESTAMP,
                "POLY_BUILDER_PASSPHRASE": payload.POLY_BUILDER_PASSPHRASE,
                "POLY_BUILDER_SIGNATURE": payload.POLY_BUILDER_SIGNATURE,
            }
        except Exception as e:
            print(f"[builder_auth] SDK header generation failed: {e} — falling back to built-in")

    # Path 2: BuilderCreds with built-in HMAC (fallback when SDK unavailable)
    if isinstance(config_or_creds, BuilderCreds):
        creds = config_or_creds
        if not creds.configured:
            return {}
        signature = _fallback_sign(creds.secret, str(ts), method, path, body)
        return {
            "POLY_BUILDER_API_KEY": creds.key,
            "POLY_BUILDER_TIMESTAMP": str(ts),
            "POLY_BUILDER_PASSPHRASE": creds.passphrase,
            "POLY_BUILDER_SIGNATURE": signature,
        }

    return {}


def _fallback_sign(
    secret: str,
    timestamp: str,
    method: str,
    path: str,
    body: Optional[str] = None,
) -> str:
    """
    Built-in HMAC-SHA256 — mirrors py_builder_signing_sdk/signing/hmac.py exactly.

    Three correctness requirements vs the old broken implementation:
      1. base64.urlsafe_b64decode (NOT standard b64decode) — handles '-' and '_'
      2. body quote replacement: str(body).replace("'", '"') before appending
      3. base64.urlsafe_b64encode on output (NOT standard b64encode)
    """
    try:
        secret_bytes = base64.urlsafe_b64decode(secret)
    except Exception:
        secret_bytes = secret.encode("utf-8")

    message = str(timestamp) + str(method) + str(path)
    if body:
        # Single-quote → double-quote normalisation matches Go and TypeScript SDKs
        message += str(body).replace("'", '"')

    sig = _hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode("utf-8")


# ── Backward-compat alias (used by quoter.py) ─────────────────────────────────

def build_builder_headers(
    creds: BuilderCreds,
    method: str = "POST",
    path: str = "/order",
    body: Optional[str] = None,
) -> dict:
    """Alias for generate_builder_headers(creds, ...) — kept for quoter.py compat."""
    return generate_builder_headers(creds, method=method, path=path, body=body)


# ── CLOB client initialisation ────────────────────────────────────────────────

def init_clob_client_with_builder(config, creds: Optional[BuilderCreds] = None):
    """
    Initialise py-clob-client and patch it with builder headers if creds are set.

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

        if creds and creds.configured:
            _patch_clob_session(client, creds)
            print(f"[builder_auth] CLOB client + builder headers initialised ✓  key={creds.key[:12]}…")
            return client, True
        else:
            print("[builder_auth] Builder creds not set — standard CLOB client")
            return client, False

    except ImportError:
        print("[builder_auth] py-clob-client not installed — dry-run mode")
        return None, False
    except Exception as e:
        print(f"[builder_auth] CLOB client init failed: {e} — dry-run mode")
        return None, False


def init_relayer_client(config, creds: Optional[BuilderCreds] = None):
    """
    Return a relayer client for gasless Polymarket transactions, or None.

    IMPORTANT: The Polymarket Relayer is NOT a CLOB endpoint.
    It handles ERC-4337 smart wallet deployment, USDC approvals, CTF operations.
    Do NOT point a ClobClient at the relayer URL — that was the old broken approach
    (a ClobClient at relayer.polymarket.com fails on every call).

    The dedicated relayer client is not yet in the public py-clob-client API.
    Returns None gracefully; standard gas transactions are used as fallback.

    Reference: https://docs.polymarket.com/builders/relayer
    """
    if not creds or not creds.configured:
        return None

    try:
        from py_clob_client.relayer import RelayerClient  # type: ignore
        relayer = RelayerClient(
            host="https://relayer.polymarket.com",
            key=config.private_key,
            chain_id=config.chain_id,
        )
        print("[builder_auth] Relayer client initialised ✓ (gasless transactions enabled)")
        return relayer
    except ImportError:
        print("[builder_auth] Relayer client not yet in py-clob-client — using standard gas txns")
        return None
    except Exception as e:
        print(f"[builder_auth] Relayer client init failed: {e} — using standard gas txns")
        return None


# ── Session patch ─────────────────────────────────────────────────────────────

def _patch_clob_session(client, creds: BuilderCreds) -> None:
    """
    Monkey-patch the CLOB client's requests.Session so builder headers are
    injected on every outbound request automatically.

    Headers injected per-request:
      POLY_BUILDER_API_KEY, POLY_BUILDER_TIMESTAMP,
      POLY_BUILDER_PASSPHRASE, POLY_BUILDER_SIGNATURE
    """
    try:
        session = client.session
        original_send = session.send

        def _send_with_builder_headers(request, **kwargs):
            from urllib.parse import urlparse
            path = urlparse(request.url).path
            body = request.body
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            headers = generate_builder_headers(
                creds, method=request.method, path=path, body=body or None
            )
            request.headers.update(headers)
            return original_send(request, **kwargs)

        session.send = _send_with_builder_headers
        print("[builder_auth] Builder headers patched into CLOB session ✓")

    except AttributeError:
        print("[builder_auth] Could not patch session — headers will be set per-request")
