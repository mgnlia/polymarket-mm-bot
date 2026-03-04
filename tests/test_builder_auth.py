"""Tests for builder_auth — credential loading and header generation."""
import os
import pytest
from unittest.mock import patch

from builder_auth import BuilderCreds, build_builder_headers, _sign_message


class TestBuilderCreds:
    def test_empty_env_returns_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            creds = BuilderCreds.from_env()
        assert not creds.configured
        assert not creds.hmac_configured

    def test_builder_id_env_var(self):
        env = {
            "POLYMARKET_BUILDER_ID": "0xABCDEF1234567890",
            "POLYMARKET_BUILDER_SIGNATURE": "my-static-sig",
        }
        with patch.dict(os.environ, env, clear=True):
            creds = BuilderCreds.from_env()
        assert creds.configured
        assert creds.builder_id == "0xABCDEF1234567890"
        assert creds.builder_signature == "my-static-sig"

    def test_hmac_env_vars(self):
        env = {
            "POLY_BUILDER_API_KEY": "mykey",
            "POLY_BUILDER_SECRET": "bXlzZWNyZXQ=",  # base64 "mysecret"
            "POLY_BUILDER_PASSPHRASE": "mypass",
        }
        with patch.dict(os.environ, env, clear=True):
            creds = BuilderCreds.from_env()
        assert creds.configured  # key falls back to builder_id
        assert creds.hmac_configured
        assert creds.key == "mykey"

    def test_builder_id_priority_over_key(self):
        env = {
            "POLYMARKET_BUILDER_ID": "explicit-id",
            "POLY_BUILDER_API_KEY": "api-key",
            "POLY_BUILDER_SECRET": "sec",
            "POLY_BUILDER_PASSPHRASE": "pass",
        }
        with patch.dict(os.environ, env, clear=True):
            creds = BuilderCreds.from_env()
        assert creds.builder_id == "explicit-id"


class TestBuildBuilderHeaders:
    def test_no_headers_when_not_configured(self):
        creds = BuilderCreds()
        headers = build_builder_headers(creds)
        assert headers == {}

    def test_static_sig_headers(self):
        creds = BuilderCreds(
            builder_id="0xBUILDER123",
            builder_signature="static-sig-value",
        )
        headers = build_builder_headers(creds, method="POST", path="/order")
        assert headers["X-Builder-Id"] == "0xBUILDER123"
        assert headers["X-Builder-Signature"] == "static-sig-value"

    def test_hmac_headers_computed(self):
        import base64
        secret_raw = b"mysecret"
        secret_b64 = base64.b64encode(secret_raw).decode()
        creds = BuilderCreds(
            builder_id="0xBUILDER",
            key="0xBUILDER",
            secret=secret_b64,
            passphrase="mypass",
        )
        headers = build_builder_headers(creds, method="POST", path="/order", body="")
        assert "X-Builder-Id" in headers
        assert "X-Builder-Signature" in headers
        assert "POLY_ADDRESS" in headers
        assert "POLY_SIGNATURE" in headers
        assert "POLY_TIMESTAMP" in headers
        assert "POLY_PASSPHRASE" in headers

    def test_x_builder_id_is_always_set(self):
        creds = BuilderCreds(builder_id="0xTEST")
        headers = build_builder_headers(creds)
        assert headers.get("X-Builder-Id") == "0xTEST"


class TestSignMessage:
    def test_deterministic_with_same_inputs(self):
        import base64
        secret = base64.b64encode(b"test-secret").decode()
        sig1 = _sign_message(secret, "1700000000", "POST", "/order", "")
        sig2 = _sign_message(secret, "1700000000", "POST", "/order", "")
        assert sig1 == sig2

    def test_different_timestamps_produce_different_sigs(self):
        import base64
        secret = base64.b64encode(b"test-secret").decode()
        sig1 = _sign_message(secret, "1700000000", "POST", "/order", "")
        sig2 = _sign_message(secret, "1700000001", "POST", "/order", "")
        assert sig1 != sig2

    def test_returns_base64_string(self):
        import base64
        secret = base64.b64encode(b"test-secret").decode()
        sig = _sign_message(secret, "1700000000", "POST", "/order", "")
        # Should be valid base64
        decoded = base64.b64decode(sig)
        assert len(decoded) == 32  # SHA256 = 32 bytes
