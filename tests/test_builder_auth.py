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
import importlib
import os
import sys
import time
from unittest.mock import patch

import pytest

# ── Helpers that mirror the SDK's exact signing logic ────────────────────────

def _sdk_sign(secret_b64url: str, timestamp: str, method: str, path: str, body=None) -> str:
    """Reproduce py_builder_signing_sdk/signing/hmac.py:build_hmac_signature."""
    key_bytes = base64.urlsafe_b64decode(secret_b64url)
    message = timestamp + method + path
    if body:
        message += str(body).replace("'", '"')
    h = hmac.new(key_bytes, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


def _make_urlsafe_b64_secret(raw: bytes = b"test-secret-value") -> str:
    """Encode raw bytes as URL-safe base64 (as Polymarket expects)."""
    return base64.urlsafe_b64encode(raw).decode("utf-8")


# ── SDK availability ──────────────────────────────────────────────────────────

try:
    from py_builder_signing_sdk import BuilderConfig, BuilderApiKeyCreds
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


# ── Tests using the real SDK (skipped if not installed) ───────────────────────

@pytest.mark.skipif(not SDK_AVAILABLE, reason="py-builder-signing-sdk not installed")
class TestSDKHeaderNames:
    """Validate that the SDK produces the four correct header names."""

    def _make_config(self) -> "BuilderConfig":
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
        """Old broken headers must NOT be generated."""
        config = self._make_config()
        payload = config.generate_builder_headers("POST", "/order")
        headers = payload.to_dict()

        assert "X-Builder-Id" not in headers
        assert "X-Builder-Signature" not in headers
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
        decoded = base64.urlsafe_b64decode(sig + "==")
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

    def test_urlsafe_b64_decode_correctness(self):
        """urlsafe_b64decode must produce correct key bytes from a URL-safe secret."""
        raw = b"\xfb\xff\xfe"
        urlsafe_secret = base64.urlsafe_b64encode(raw).decode()
        key_bytes = base64.urlsafe_b64decode(urlsafe_secret)
        assert key_bytes == raw

        sig = _sdk_sign(urlsafe_secret, "1700000000", "POST", "/order")
        assert len(base64.urlsafe_b64decode(sig + "==")) == 32

    def test_single_quote_replacement_in_body(self):
        """Body with single quotes must produce the same sig as double-quoted body."""
        secret = _make_urlsafe_b64_secret()
        ts = "1700000000"

        body_single = "{'size': 10, 'side': 'BUY'}"
        body_double = '{"size": 10, "side": "BUY"}'

        sig_single = _sdk_sign(secret, ts, "POST", "/order", body_single)
        sig_double = _sdk_sign(secret, ts, "POST", "/order", body_double)

        assert sig_single == sig_double, (
            "Signature for body with single quotes must equal signature for "
            "same body with double quotes after normalisation."
        )

    def test_none_body_excludes_body_from_message(self):
        """None body: message is just timestamp+method+path, no body appended."""
        secret = _make_urlsafe_b64_secret()
        ts = "1700000000"
        sig_none = _sdk_sign(secret, ts, "GET", "/markets", None)

        key_bytes = base64.urlsafe_b64decode(secret)
        message = ts + "GET" + "/markets"
        expected = base64.urlsafe_b64encode(
            hmac.new(key_bytes, message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        assert sig_none == expected

    def test_different_methods_produce_different_sigs(self):
        secret = _make_urlsafe_b64_secret()
        ts = "1700000000"
        assert _sdk_sign(secret, ts, "POST", "/order") != _sdk_sign(secret, ts, "GET", "/order")

    def test_different_paths_produce_different_sigs(self):
        secret = _make_urlsafe_b64_secret()
        ts = "1700000000"
        assert _sdk_sign(secret, ts, "POST", "/order") != _sdk_sign(secret, ts, "POST", "/cancel")

    def test_signature_output_is_urlsafe_base64(self):
        secret = _make_urlsafe_b64_secret()
        sig = _sdk_sign(secret, "1700000000", "POST", "/order", '{"x":1}')
        assert "+" not in sig, "Signature must use URL-safe base64 (no +)"
        assert "/" not in sig, "Signature must use URL-safe base64 (no /)"


# ── Tests for builder_auth module functions ───────────────────────────────────

def _reload_builder_auth():
    """Force a fresh import of builder_auth so env patches take effect."""
    if "builder_auth" in sys.modules:
        del sys.modules["builder_auth"]
    import builder_auth
    return builder_auth


@pytest.mark.skipif(not SDK_AVAILABLE, reason="py-builder-signing-sdk not installed")
class TestBuilderAuthModule:
    """Tests for builder_auth.py wrapper functions."""

    def test_no_headers_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            ba = _reload_builder_auth()
            config = ba.load_builder_config()
            headers = ba.generate_builder_headers(config, "POST", "/order")
        assert headers == {}

    def test_correct_headers_when_env_set(self):
        secret = _make_urlsafe_b64_secret()
        env = {
            "POLY_BUILDER_API_KEY": "mykey",
            "POLY_BUILDER_SECRET": secret,
            "POLY_BUILDER_PASSPHRASE": "mypass",
        }
        with patch.dict(os.environ, env, clear=True):
            ba = _reload_builder_auth()
            config = ba.load_builder_config()
            headers = ba.generate_builder_headers(config, "POST", "/order", '{"size":5}')

        assert "POLY_BUILDER_API_KEY" in headers
        assert "POLY_BUILDER_TIMESTAMP" in headers
        assert "POLY_BUILDER_PASSPHRASE" in headers
        assert "POLY_BUILDER_SIGNATURE" in headers
        # Old broken headers must be absent
        assert "X-Builder-Id" not in headers
        assert "X-Builder-Signature" not in headers

    def test_api_key_and_passphrase_values_correct(self):
        secret = _make_urlsafe_b64_secret()
        env = {
            "POLY_BUILDER_API_KEY": "mykey",
            "POLY_BUILDER_SECRET": secret,
            "POLY_BUILDER_PASSPHRASE": "mypass",
        }
        with patch.dict(os.environ, env, clear=True):
            ba = _reload_builder_auth()
            config = ba.load_builder_config()
            headers = ba.generate_builder_headers(config, "POST", "/order")

        assert headers["POLY_BUILDER_API_KEY"] == "mykey"
        assert headers["POLY_BUILDER_PASSPHRASE"] == "mypass"

    def test_is_builder_enabled_false_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            ba = _reload_builder_auth()
            assert not ba.is_builder_enabled()

    def test_is_builder_enabled_true_with_env(self):
        secret = _make_urlsafe_b64_secret()
        env = {
            "POLY_BUILDER_API_KEY": "k",
            "POLY_BUILDER_SECRET": secret,
            "POLY_BUILDER_PASSPHRASE": "p",
        }
        with patch.dict(os.environ, env, clear=True):
            ba = _reload_builder_auth()
            assert ba.is_builder_enabled()

    def test_signature_math_matches_sdk(self):
        """Signature produced by generate_builder_headers must match _sdk_sign."""
        secret = _make_urlsafe_b64_secret()
        env = {
            "POLY_BUILDER_API_KEY": "k",
            "POLY_BUILDER_SECRET": secret,
            "POLY_BUILDER_PASSPHRASE": "p",
        }
        ts = 1700000000
        with patch.dict(os.environ, env, clear=True):
            ba = _reload_builder_auth()
            config = ba.load_builder_config()
            headers = ba.generate_builder_headers(
                config, "POST", "/order", '{"size":5}', timestamp=ts
            )

        expected_sig = _sdk_sign(secret, str(ts), "POST", "/order", '{"size":5}')
        assert headers["POLY_BUILDER_SIGNATURE"] == expected_sig
