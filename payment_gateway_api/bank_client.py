"""The acquiring-bank HTTP boundary; no retries and no raw payload logging."""

import json
import math
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from payment_gateway_api.exceptions import BankError
from payment_gateway_api.models import (
    PaymentRequest,
    PaymentStatus,
    ProcessedPaymentStatus,
)


@dataclass(frozen=True)
class BankSettings:
    """Bank configuration; timeout applies separately to each HTTPX I/O phase."""

    base_url: str
    timeout_seconds: float

    @classmethod
    def from_env(cls):
        """Fail startup on invalid settings without echoing potentially secret URLs."""
        base_url = os.environ.get("BANK_BASE_URL", "http://localhost:8080")
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in base_url)
                or (port is not None and port == 0)
            ):
                raise ValueError()
        except ValueError:
            raise ValueError(
                "BANK_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment."
            ) from None
        try:
            timeout = float(os.environ.get("BANK_TIMEOUT_SECONDS", "5"))
        except ValueError:
            raise ValueError(
                "BANK_TIMEOUT_SECONDS must be a finite positive number."
            ) from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("BANK_TIMEOUT_SECONDS must be a finite positive number.")
        return cls(base_url=base_url.rstrip("/"), timeout_seconds=timeout)


class BankClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._payments_url = base_url.rstrip("/") + "/payments"

    async def process(self, payment: PaymentRequest) -> ProcessedPaymentStatus:
        # Forward only the bank's fields, including its combined MM/YYYY expiry.
        payload = {
            "card_number": payment.card_number,
            "expiry_date": "{:02d}/{:04d}".format(
                payment.expiry_month, payment.expiry_year
            ),
            "currency": payment.currency,
            "amount": payment.amount,
            "cvv": payment.cvv,
        }
        try:
            # Do not retry: a timed-out request might already have been authorized.
            response = await self._client.post(self._payments_url, json=payload)
        except httpx.TimeoutException:
            raise BankError("Timeout") from None
        except httpx.RequestError:
            raise BankError("TransportError") from None
        # Only HTTP 200 carries the simulator's normal authorization response.
        if response.status_code != 200:
            raise BankError("HttpStatus", response.status_code)
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BankError("InvalidResponse") from None
        # Truthiness would incorrectly accept values such as "false" or 1.
        if not isinstance(body, dict) or type(body.get("authorized")) is not bool:
            raise BankError("InvalidResponse")
        return (
            PaymentStatus.AUTHORIZED if body["authorized"] else PaymentStatus.DECLINED
        )
