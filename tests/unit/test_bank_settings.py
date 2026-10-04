"""Bank configuration rules in isolation; no HTTP requests or application startup."""

import pytest

from payment_gateway_api.bank_client import BankSettings


class TestBankSettings:
    """Defaults, environment overrides and invalid startup configuration."""

    def test_default_bank_settings(self):
        settings = BankSettings.from_env()
        assert settings.base_url == "http://localhost:8080"
        assert settings.timeout_seconds == 5

    def test_configurable_bank_settings(self, monkeypatch):
        monkeypatch.setenv("BANK_BASE_URL", "https://bank.test:8443/api/")
        monkeypatch.setenv("BANK_TIMEOUT_SECONDS", "1.25")
        settings = BankSettings.from_env()
        assert settings.base_url == "https://bank.test:8443/api"
        assert settings.timeout_seconds == 1.25

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "localhost:8080",
            "ftp://bank.test",
            "http://",
            "http://user:password@bank.test",
            "http://bank.test?token=secret",
            "http://bank.test#secret",
            "http://bank.test:0",
            "http://bank.test:99999",
            "http://bank.test:abc",
            "http://[broken",
            "http://bank test",
        ],
    )
    def test_invalid_base_url_fails_with_safe_message(self, monkeypatch, url):
        monkeypatch.setenv("BANK_BASE_URL", url)
        with pytest.raises(ValueError, match="BANK_BASE_URL must be") as error:
            BankSettings.from_env()
        assert "password" not in str(error.value)
        assert "secret" not in str(error.value)

    @pytest.mark.parametrize(
        "timeout", ["", "not-a-number", "0", "-1", "nan", "inf", "-inf"]
    )
    def test_invalid_timeout_fails_at_startup(self, monkeypatch, timeout):
        monkeypatch.setenv("BANK_TIMEOUT_SECONDS", timeout)
        with pytest.raises(ValueError, match="BANK_TIMEOUT_SECONDS must be"):
            BankSettings.from_env()
