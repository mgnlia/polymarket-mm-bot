"""
Builder Program authentication using the official py-builder-signing-sdk.

Replaces the previous hand-rolled builder_auth.py with the official SDK.
Headers generated: POLY_BUILDER_API_KEY, POLY_BUILDER_TIMESTAMP,
                   POLY_BUILDER_PASSPHRASE, POLY_BUILDER_SIGNATURE
Reference SDK: https://github.com/Polymarket/py-builder-signing-sdk
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from py_builder_signing_sdk import BuilderConfig, BuilderApiKeyCreds

logger = logging.getLogger(__name__)


def load_builder_config() -> Optional[BuilderConfig]:
    """
    Load builder credentials from environment variables and return a
    configured BuilderConfig, or None if credentials are absent.

    Required env vars:
        POLY_BUILDER_API_KEY      — builder API key
        POLY_BUILDER_SECRET       — base64url-encoded HMAC secret
        POLY_BUILDER_PASSPHRASE   — passphrase

    Returns:
        BuilderConfig if all three vars are set and non-empty, else None.
    """
    key = os.getenv("POLY_BUILDER_API_KEY", "").strip()
    secret = os.getenv("POLY_BUILDER_SECRET", "").strip()
    passphrase = os.getenv("POLY_BUILDER_PASSPHRASE", "").strip()

    if not (key and secret and passphrase):
        logger.info(
            "Builder Program credentials not configured — "
            "set POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE "
            "to enable order attribution."
        )
        return None

    try:
        creds = BuilderApiKeyCreds(key=key, secret=secret, passphrase=passphrase)
        config = BuilderConfig(local_builder_creds=creds)
        logger.info("Builder Program credentials loaded (key=%s…)", key[:8])
        return config
    except Exception as exc:
        logger.error("Failed to initialise BuilderConfig: %s", exc)
        return None


def generate_builder_headers(
    config: Optional[BuilderConfig],
    method: str,
    path: str,
    body: Optional[str] = None,
) -> Dict[str, str]:
    """
    Generate the four official builder headers for a CLOB request.

    Args:
        config:  BuilderConfig from load_builder_config(), or None.
        method:  HTTP method, e.g. "POST".
        path:    Request path, e.g. "/order".
        body:    Optional request body string.

    Returns:
        Dict with keys POLY_BUILDER_API_KEY, POLY_BUILDER_TIMESTAMP,
        POLY_BUILDER_PASSPHRASE, POLY_BUILDER_SIGNATURE,
        or an empty dict if config is None.
    """
    if config is None:
        return {}

    try:
        payload = config.generate_builder_headers(method, path, body)
        if payload is None:
            return {}
        return payload.to_dict()
    except Exception as exc:
        logger.error("Failed to generate builder headers: %s", exc)
        return {}


# ── Module-level singleton ────────────────────────────────────────────────────

_builder_config: Optional[BuilderConfig] = None


def get_builder_config() -> Optional[BuilderConfig]:
    """Return (and lazily initialise) the module-level BuilderConfig."""
    global _builder_config
    if _builder_config is None:
        _builder_config = load_builder_config()
    return _builder_config


def is_builder_enabled() -> bool:
    """Return True if builder credentials are configured."""
    return get_builder_config() is not None
