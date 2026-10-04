"""Common test inputs and deterministic settings for every test layer."""

from datetime import date

import pytest

from payment_gateway_api import models


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def deterministic_settings(monkeypatch):
    # Keep expiry boundaries and bank defaults independent of the caller's machine.
    monkeypatch.setattr(models, "utc_today", lambda: date(2026, 10, 3))
    monkeypatch.delenv("BANK_BASE_URL", raising=False)
    monkeypatch.delenv("BANK_TIMEOUT_SECONDS", raising=False)


@pytest.fixture
def payment_payload():
    return {
        "card_number": "2222405343248877",
        "expiry_month": 12,
        "expiry_year": 2030,
        "currency": "GBP",
        "amount": 1050,
        "cvv": "012",
    }
