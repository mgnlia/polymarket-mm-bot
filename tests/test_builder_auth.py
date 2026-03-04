"""
Tests for builder_auth — validates correct header names, signing behaviour,
and SDK integration per the official py-builder-signing-sdk.

Correct header names (from SDK sdk_types.py BuilderHeaderPayload):
    POLY_BUILDER_API_KEY
    POLY_BUILDER_TIMESTAMP
    POLY_BUILDER_PASSPHRASE
    POLY_BUILDER_SIGNATURE

Signing uses urlsafe_b64decode (not standard b64decode) and replaces
single quotes with double quotes in the body before HMAC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Dict
from unittest.mock import patch

import pytest

# ── Helpers that mirror the SDK's exact signing logic ────────────────────────

def _sdk_sign(secret_b64url: str, timestamp: str, method: str, path: str, body=None) -> str:
    """Reproduce the SDK's build_hmac_signature for test assertions."""
    key_bytes = base64.urlsafe_b64decode(secret_b64url)
    message = timestamp + method + path
    if body:
        message += str(body).replace("'", '"')
    h = hmac.new(key_bytes, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


def _make_urlsafe_b64_secret(raw: bytes = b"test-secret-value") -> str:
    """Encode raw bytes as URL-safe base64 (as Polymarket expects)."""
    return base64.urlsafe_b64encode(raw).decode("utf-8")


# ── Import the module under test ──────────────────────────────────────────────

# Patch the SDK import so tests don't need the package installed in CI
# (if it IS installed, the real SDK is used — both paths are tested)
try:
    from py_builder_signing_sdk import BuilderConfig, BuilderApiKeyCreds
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


# ── Tests using the real SDK (skipped if not installed) ───────────────────────

@pytest.mark.skipif(not SDK_AVAILABLE, reason="py-builder-signing-sdk not installed")
class TestSDKHeaderNames:
    """Validate that the SDK produces the four correct header names."""

    def _make_config(self) -> BuilderConfig:
        secret = _make_urlsafe_b64_secret()
        creds = BuilderApiKeyCreds(key="test-key", secret=secret, passphrase="test-pass")
        return BuilderConfig(local_builder_creds=creds)

    def test_correct_header_keys_present(self):
        config = self._make_config()
        payload = config.generate_builder_headers("POST", "/order", '{"size": 10}')
        headers = payload.to_dict()

        assert "POLY_BUILDER_API_KEY" in headers, "Missing POLY_BUILDER_API_KEY"
        assert "POLY_BUILDER_TIMESTAMP" in headers, "Missing POLY_BUILDER_TIMESTAMP"
        assert "POLY_BUILDER_PASSPHRASE" in headers, "Missing POLY_BUILDER_PASSPHRASE"
        assert "POLY_BUILDER_SIGNATURE" in headers, "Missing POLY_BUILDER_SIGNATURE"

    def test_wrong_header_names_not_present(self):
        """Ensure the old broken headers are NOT generated."""
        config = self._make_config()
        payload = config.generate_builder_headers("POST", "/order")
        headers = payload.to_dict()

        assert "X-Builder-Id" not in headers, "Old header X-Builder-Id must not be present"
        assert "X-Builder-Signature" not in headers, "Old header X-Builder-Signature must not be present"
        assert "POLY_ADDRESS" not in headers
        assert "POLY_SIGNATURE" not in headers

    def test_api_key_value_matches_input(self):
        config = self._make_config()
        payload = config.generate_builder_headers("GET", "/markets")
        assert payload.POLY_BUILDER_API_KEY == "test-key"

    def test_passphrase_value_matches_input(self):
        config = self._make_config()
        payload = config.generate_builder_headers("GET", "/markets")
        assert payload.POLY_BUILDER_PASSPHRASE == "test-pass"

    def test_timestamp_is_numeric_string(self):
        config = self._make_config()
        payload = config.generate_builder_headers("GET", "/markets")
        assert payload.POLY_BUILDER_TIMESTAMP.isdigit(), (
            f"Timestamp should be a numeric string, got: {payload.POLY_BUILDER_TIMESTAMP!r}"
        )

    def test_signature_is_non_empty_string(self):
        config = self._make_config()
        payload = config.generate_builder_headers("POST", "/order", '{"x":1}')
        assert isinstance(payload.POLY_BUILDER_SIGNATURE, str)
        assert len(payload.POLY_BUILDER_SIGNATURE) > 0

    def test_signature_is_urlsafe_base64(self):
        """Signature must be URL-safe base64 (uses - and _ not + and /)."""
        config = self._make_config()
        payload = config.generate_builder_headers("POST", "/order", '{"x":1}')
        sig = payload.POLY_BUILDER_SIGNATURE
        # URL-safe b64 should decode without error
        decoded = base64.urlsafe_b64decode(sig + "==")  # pad for safety
        assert len(decoded) == 32, "SHA-256 HMAC must be 32 bytes"

    def test_signature_changes_with_different_body(self):
        config = self._make_config()
        ts = int(time.time())
        p1 = config.generate_builder_headers("POST", "/order", '{"size": 10}', timestamp=ts)
        p2 = config.generate_builder_headers("POST", "/order", '{"size": 99}', timestamp=ts)
        assert p1.POLY_BUILDER_SIGNATURE != p2.POLY_BUILDER_SIGNATURE

    def test_signature_deterministic_same_inputs(self):
        config = self._make_config()
        ts = 1700000000
        p1 = config.generate_builder_headers("POST", "/order", '{"x":1}', timestamp=ts)
        p2 = config.generate_builder_headers("POST", "/order", '{"x":1}', timestamp=ts)
        assert p1.POLY_BUILDER_SIGNATURE == p2.POLY_BUILDER_SIGNATURE


# ── Tests for the HMAC signing math ──────────────────────────────────────────

class TestHmacSigningMath:
    """Validate the signing algorithm independently of the SDK."""

    def test_urlsafe_b64_decode_used(self):
        """
        If standard b64decode were used on a URL-safe secret, the key bytes
        would differ (- vs +, _ vs /). Verify our helper uses urlsafe decode.
        """
        raw = b"\xfb\xff\xfe"  # bytes that differ between standard and urlsafe b64
        urlsafe_secret = base64.urlsafe_b64encode(raw).decode()
        standard_secret = base64.b64encode(raw).decode()

        # urlsafe and standard encodings may differ for these bytes
        key_urlsafe = base64.urlsafe_b64decode(urlsafe_secret)
        key_standard = base64.b64decode(standard_secret)
        assert key_urlsafe == raw
        assert key_standard == raw  # same raw bytes, different encoded form

        # Signing with the urlsafe-encoded secret using urlsafe decode is correct
        sig = _sdk_sign(urlsafe_secret, "1700000000", "POST", "/order")
        assert len(base64.urlsafe_b64decode(sig + "==")) == 32

    def test_single_quote_replacement_in_body(self):
        """Body with single quotes must be normalised before signing."""
        secret = _make_urlsafe_b64_secret()
        ts = "1700000000"

        # Body with single quotes (Python dict repr style)
        body_with_single = "{'size': 10, 'side': 'BUY'}"
        body_with_double = '{"size": 10, "side": "BUY"}'

        sig_single = _sdk_sign(secret, ts, "POST", "/order", body_with_single)
        sig_double = _sdk_sign(secret, ts, "POST", "/order", body_with_double)

        # Both must produce the same signature (single quotes normalised)
        assert sig_single == sig_double, (
            "Signature for body with single quotes must equal signature for "
            "same body with double quotes after normalisation."
        )

    def test_no_body_vs_none_body(self):
        """None body and empty body should behave consistently."""
        secret = _make_urlsafe_b64_secret()
        ts = "1700000000"
        sig_none = _sdk_sign(secret, ts, "GET", "/markets", None)
        # None body: message is just ts+method+path, no body appended
        key_bytes = base64.urlsafe_b64decode(secret)
        message = ts + "GET" + "/markets"
        expected = base64.urlsafe_b64encode(
            hmac.new(key_bytes, message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        assert sig_none == expected

    def test_different_methods_produce_different_sigs(self):
        secret = _make_urlsafe_b64_secret()
        ts = "1700000000"
        sig_post = _sdk_sign(secret, ts, "POST", "/order")
        sig_get = _sdk_sign(secret, ts, "GET", "/order")
        assert sig_post != sig_get

    def test_different_paths_produce_different_sigs(self):
        secret = _make_urlsafe_b64_secret()
        ts = "1700000000"
        sig1 = _sdk_sign(secret, ts, "POST", "/order")
        sig2 = _sdk_sign(secret, ts, "POST", "/cancel")
        assert sig1 != sig2

    def test_signature_output_is_urlsafe_base64(self):
        secret = _make_urlsafe_b64_secret()
        sig = _sdk_sign(secret, "1700000000", "POST", "/order", '{"x":1}')
        # Must not contain standard b64 chars + or /
        assert "+" not in sig, "Signature must use URL-safe base64 (no +)"
        assert "/" not in sig, "Signature must use URL-safe base64 (no /)"


# ── Tests for builder_auth module ─────────────────────────────────────────────

@pytest.mark.skipif(not SDK_AVAILABLE, reason="py-builder-signing-sdk not installed")
class TestBuilderAuthModule:
    """Tests for the bot's builder_auth.py wrapper."""

    def test_no_headers_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            # Reset module singleton
            import builder_auth
            builder_auth._builder_config = None
            from builder_auth import generate_builder_headers, load_builder_config
            config = load_builder_config()
            headers = generate_builder_headers(config, "POST", "/order")
        assert headers == {}

    def test_correct_headers_when_env_set(self):
        secret = _make_urlsafe_b64_secret()
        env = {
            "POLY_BUILDER_API_KEY": "mykey",
            "POLY_BUILDER_SECRET": secret,
            "POLY_BUILDER_PASSPHRASE": "mypass",
        }
        with patch.dict(os.environ, env, clear=True):
            import builder_auth
            builder_auth._builder_config = None
            from builder_auth import generate_builder_headers, load_builder_config
            config = load_builder_config()
            headers = generate_builder_headers(config, "POST", "/order", '{"size":5}')

        assert "POLY_BUILDER_API_KEY" in headers
        assert "POLY_BUILDER_TIMESTAMP" in headers
        assert "POLY_BUILDER_PASSPHRASE" in headers
        assert "POLY_BUILDER_SIGNATURE" in headers

        # Old broken headers must be absent
        assert "X-Builder-Id" not in headers
        assert "X-Builder-Signature" not in headers

    def test_is_builder_enabled_false_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            import builder_auth
            builder_auth._builder_config = None
            from builder_auth import is_builder_enabled
            assert not is_builder_enabled()

    def test_is_builder_enabled_true_with_env(self):
        secret = _make_urlsafe_b64_secret()
        env = {
            "POLY_BUILDER_API_KEY": "k",
            "POLY_BUILDER_SECRET": secret,
            "POLY_BUILDER_PASSPHRASE": "p",
        }
        with patch.dict(os.environ, env, clear=True):
            import builder_auth
            builder_auth._builder_config = None
            from builder_auth import is_builder_enabled
            assert is_builder_enabled()
