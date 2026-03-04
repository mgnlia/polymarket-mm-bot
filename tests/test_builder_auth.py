"""Tests for builder_auth.py — Builder Program authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest

from builder_auth import BuilderCreds, _sign_message, build_builder_headers


class TestBuilderCreds:
    def test_from_env_empty(self, monkeypatch):
        monkeypatch.delenv("POLY_BUILDER_API_KEY", raising=False)
        monkeypatch.delenv("POLY_BUILDER_SECRET", raising=False)
        monkeypatch.delenv("POLY_BUILDER_PASSPHRASE", raising=False)
        creds = BuilderCreds.from_env()
        assert creds.key == ""
        assert not creds.configured

    def test_from_env_set(self, monkeypatch):
        monkeypatch.setenv("POLY_BUILDER_API_KEY", "test_key")
        monkeypatch.setenv("POLY_BUILDER_SECRET", base64.b64encode(b"secret123").decode())
        monkeypatch.setenv("POLY_BUILDER_PASSPHRASE", "pass123")
        creds = BuilderCreds.from_env()
        assert creds.key == "test_key"
        assert creds.configured

    def test_configured_false_when_partial(self, monkeypatch):
        monkeypatch.setenv("POLY_BUILDER_API_KEY", "key")
        monkeypatch.delenv("POLY_BUILDER_SECRET", raising=False)
        monkeypatch.delenv("POLY_BUILDER_PASSPHRASE", raising=False)
        creds = BuilderCreds.from_env()
        assert not creds.configured


class TestSignMessage:
    def test_signature_is_deterministic(self):
        secret = base64.b64encode(b"test_secret_bytes").decode()
        sig1 = _sign_message(secret, "1700000000", "POST", "/order", "")
        sig2 = _sign_message(secret, "1700000000", "POST", "/order", "")
        assert sig1 == sig2

    def test_signature_changes_with_timestamp(self):
        secret = base64.b64encode(b"test_secret_bytes").decode()
        sig1 = _sign_message(secret, "1700000000", "POST", "/order", "")
        sig2 = _sign_message(secret, "1700000001", "POST", "/order", "")
        assert sig1 != sig2

    def test_signature_is_valid_hmac_sha256(self):
        raw_secret = b"test_secret_bytes"
        secret = base64.b64encode(raw_secret).decode()
        timestamp = "1700000000"
        method = "POST"
        path = "/order"
        body = '{"test": true}'
        message = timestamp + method + path + body
        expected = base64.b64encode(
            hmac.new(raw_secret, message.encode(), hashlib.sha256).digest()
        ).decode()
        result = _sign_message(secret, timestamp, method, path, body)
        assert result == expected


class TestBuildBuilderHeaders:
    def _make_creds(self) -> BuilderCreds:
        return BuilderCreds(
            key="test_key_abc",
            secret=base64.b64encode(b"secret_bytes").decode(),
            passphrase="my_passphrase",
        )

    def test_returns_empty_when_not_configured(self):
        creds = BuilderCreds(key="", secret="", passphrase="")
        headers = build_builder_headers(creds)
        assert headers == {}

    def test_returns_all_required_headers(self):
        creds = self._make_creds()
        headers = build_builder_headers(creds, method="POST", path="/order")
        assert "POLY_ADDRESS" in headers
        assert "POLY_SIGNATURE" in headers
        assert "POLY_TIMESTAMP" in headers
        assert "POLY_PASSPHRASE" in headers

    def test_address_matches_key(self):
        creds = self._make_creds()
        headers = build_builder_headers(creds)
        assert headers["POLY_ADDRESS"] == "test_key_abc"

    def test_passphrase_matches(self):
        creds = self._make_creds()
        headers = build_builder_headers(creds)
        assert headers["POLY_PASSPHRASE"] == "my_passphrase"

    def test_timestamp_is_recent(self):
        creds = self._make_creds()
        before = int(time.time()) - 2
        headers = build_builder_headers(creds)
        after = int(time.time()) + 2
        ts = int(headers["POLY_TIMESTAMP"])
        assert before <= ts <= after

    def test_signature_is_base64(self):
        creds = self._make_creds()
        headers = build_builder_headers(creds)
        sig = headers["POLY_SIGNATURE"]
        # Should not raise
        decoded = base64.b64decode(sig)
        assert len(decoded) == 32  # SHA-256 = 32 bytes
