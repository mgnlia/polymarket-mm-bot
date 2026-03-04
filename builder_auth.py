"""
builder_auth.py — Polymarket Builder Program authentication.

Implements the official py-builder-signing-sdk signing spec:
  https://github.com/Polymarket/py-builder-signing-sdk

Headers generated (matching official SDK BuilderHeaderPayload):
  POLY_BUILDER_API_KEY   — builder API key
  POLY_BUILDER_TIMESTAMP — unix timestamp (string)
  POLY_BUILDER_PASSPHRASE — API passphrase
  POLY_BUILDER_SIGNATURE  — HMAC-SHA256 signature (urlsafe base64)

Signing spec (from py_builder_signing_sdk/signing/hmac.py):
  secret_bytes = base64.urlsafe_b64decode(secret)
  message = timestamp + method + path [+ body.replace("'", '"')]
  sig = hmac.new(secret_bytes, message.encode("utf-8"), sha256).digest()
  return base64.urlsafe_b64encode(sig).decode("utf-8")

Environment variables:
  POLY_BUILDER_API_KEY    — builder API key
  POLY_BUILDER_SECRET     — HMAC signing secret (URL-safe base64-encoded)
  POLY_BUILDER_PASSPHRASE — API passphrase
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass
class BuilderCreds:
    """Builder API credentials loaded from environment.

    Only one auth flow exists: key/secret/passphrase HMAC signing.
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
        """True if full HMAC credentials are available."""
        return bool(self.key and self.secret and self.passphrase)


def _sign_message(secret: str, timestamp: str, method: str, path: str, body: Optional[str] = None) -> str:
    """
    HMAC-SHA256 signature per official py-builder-signing-sdk spec.

    From py_builder_signing_sdk/signing/hmac.py:
      base64_secret = base64.urlsafe_b64decode(secret)
      message = timestamp + method + path [+ body.replace("'", '"')]
      sig = hmac.new(base64_secret, message.encode("utf-8"), sha256).digest()
      return base64.urlsafe_b64encode(sig).decode("utf-8")

    Key correctness points vs broken prior impl:
      1. urlsafe_b64decode (not standard b64decode) — handles '-' and '_' chars
      2. body quote replacement: replace("'", '"') before appending
      3. urlsafe_b64encode on output (not standard b64encode)
    """
    # URL-safe base64 decode (official spec)
    try:
        secret_bytes = base64.urlsafe_b64decode(secret)
    except Exception:
        # Fallback: treat as raw UTF-8 if not valid base64
        secret_bytes = secret.encode("utf-8")

    # Build message: timestamp + METHOD + path [+ body with quote replacement]
    message = str(timestamp) + str(method) + str(path)
    if body:
        # NOTE: Necessary to replace single quotes with double quotes
        # to generate the same HMAC message as Go and TypeScript SDKs
        message += str(body).replace("'", '"')

    sig = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    # URL-safe base64 encode output (official spec)
    return base64.urlsafe_b64encode(sig).decode("utf-8")


def build_builder_headers(
    creds: BuilderCreds,
    method: str = "POST",
    path: str = "/order",
    body: Optional[str] = None,
) -> dict[str, str]:
    """
    Generate the official builder authentication headers for Polymarket CLOB.

    Header names match official BuilderHeaderPayload from py-builder-signing-sdk:
      POLY_BUILDER_API_KEY   — builder API key
      POLY_BUILDER_TIMESTAMP — unix timestamp as string
      POLY_BUILDER_PASSPHRASE — API passphrase
      POLY_BUILDER_SIGNATURE  — HMAC-SHA256 (urlsafe base64)

    Returns empty dict if credentials are not configured.
    """
    if not creds.configured:
        return {}

    timestamp = str(int(time.time()))
    signature = _sign_message(creds.secret, timestamp, method, path, body)

    return {
        "POLY_BUILDER_API_KEY": creds.key,
        "POLY_BUILDER_TIMESTAMP": timestamp,
        "POLY_BUILDER_PASSPHRASE": creds.passphrase,
        "POLY_BUILDER_SIGNATURE": signature,
    }


def init_clob_client_with_builder(config, creds: Optional[BuilderCreds] = None):
    """
    Initialize py-clob-client with builder attribution baked in via session patch.

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
            _patch_client_with_builder_headers(client, creds)
            print(f"[builder_auth] CLOB client initialized with Builder attribution ✓")
            print(f"[builder_auth] Builder key: {creds.key[:12]}...")
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
    Monkey-patch the CLOB client's HTTP session to inject official builder headers
    on every outbound request automatically.

    Headers injected: POLY_BUILDER_API_KEY, POLY_BUILDER_TIMESTAMP,
                      POLY_BUILDER_PASSPHRASE, POLY_BUILDER_SIGNATURE
    """
    try:
        session = client.session  # requests.Session

        class BuilderHeaderAdapter:
            """Wraps the session's send() to inject builder headers per request."""
            def __init__(self, original_send, builder_creds: BuilderCreds):
                self._send = original_send
                self._creds = builder_creds

            def __call__(self, request, **kwargs):
                from urllib.parse import urlparse
                parsed = urlparse(request.url)
                path = parsed.path
                body = request.body
                if isinstance(body, bytes):
                    body = body.decode("utf-8")
                headers = build_builder_headers(
                    self._creds,
                    method=request.method,
                    path=path,
                    body=body or None,
                )
                request.headers.update(headers)
                return self._send(request, **kwargs)

        session.send = BuilderHeaderAdapter(session.send, creds)
        print(
            "[builder_auth] Official builder headers "
            "(POLY_BUILDER_API_KEY, POLY_BUILDER_SIGNATURE, ...) "
            "patched into CLOB session ✓"
        )

    except AttributeError:
        print("[builder_auth] Could not patch session — builder headers will be set per-request")
