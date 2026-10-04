"""Model rules in isolation: strict input types, expiry boundaries and safe records."""

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from payment_gateway_api import models
from payment_gateway_api.models import PaymentRequest, PaymentResponse, PaymentStatus


class TestRequiredFields:
    """Every payment field must be present and non-null."""

    @pytest.mark.parametrize(
        "field",
        ["card_number", "expiry_month", "expiry_year", "currency", "amount", "cvv"],
    )
    def test_missing_field_is_rejected(self, payment_payload, field):
        del payment_payload[field]
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["type"] == "missing"
        assert error.value.errors()[0]["loc"] == (field,)

    @pytest.mark.parametrize(
        "field",
        ["card_number", "expiry_month", "expiry_year", "currency", "amount", "cvv"],
    )
    def test_null_field_is_rejected(self, payment_payload, field):
        payment_payload[field] = None
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["loc"] == (field,)


class TestCardNumber:
    """Strict strings of 14 to 19 ASCII digits; preserve leading zeros."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("1" * 13, id="card-too-short"),
            pytest.param("1" * 20, id="card-too-long"),
            pytest.param("22224053432488a7", id="card-letters"),
            pytest.param("2222 405343248877", id="card-spaces"),
            pytest.param("２" * 16, id="card-unicode-digits"),
            pytest.param("2222405343248877\n", id="card-newline"),
            pytest.param(2222405343248877, id="card-integer"),
        ],
    )
    def test_invalid_card_number_is_rejected(self, payment_payload, value):
        payment_payload["card_number"] = value
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["loc"] == ("card_number",)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("1" * 14, id="card-minimum-length"),
            pytest.param("1" * 19, id="card-maximum-length"),
            pytest.param("0000000000000012", id="card-leading-zeros"),
        ],
    )
    def test_valid_card_number_is_preserved(self, payment_payload, value):
        payment_payload["card_number"] = value
        payment = PaymentRequest.model_validate(payment_payload)
        assert payment.model_dump() == payment_payload


class TestExpiryMonth:
    """Strict integers in the range 1 to 12."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(0, id="month-zero"),
            pytest.param(13, id="month-above-twelve"),
            pytest.param("12", id="month-string"),
            pytest.param(12.0, id="month-float"),
            pytest.param(True, id="month-boolean"),
        ],
    )
    def test_invalid_expiry_month_is_rejected(self, payment_payload, value):
        payment_payload["expiry_month"] = value
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["loc"] == ("expiry_month",)


class TestExpiryYear:
    """Strict integers in the calendar range 1 to 9999."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(0, id="year-zero"),
            pytest.param(10000, id="year-above-calendar-range"),
            pytest.param("2030", id="year-string"),
            pytest.param(2030.0, id="year-float"),
            pytest.param(True, id="year-boolean"),
        ],
    )
    def test_invalid_expiry_year_is_rejected(self, payment_payload, value):
        payment_payload["expiry_year"] = value
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["loc"] == ("expiry_year",)


class TestExpiryDate:
    """Check the month/year pair against a fixed UTC date, including calendar boundaries."""

    @pytest.mark.parametrize(
        "month,year",
        [(10, 2026), (11, 2026), (1, 2027)],
        ids=["current-month", "next-month", "next-year"],
    )
    def test_current_or_future_month_is_accepted(self, payment_payload, month, year):
        payment_payload.update(expiry_month=month, expiry_year=year)
        payment = PaymentRequest.model_validate(payment_payload)
        assert (payment.expiry_year, payment.expiry_month) == (year, month)

    @pytest.mark.parametrize(
        "month,year",
        [(9, 2026), (12, 2025)],
        ids=["previous-month", "past-year"],
    )
    def test_past_month_is_rejected(self, payment_payload, month, year):
        payment_payload.update(expiry_month=month, expiry_year=year)
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["type"] == "card_expired"

    def test_expiry_across_year_boundary(self, payment_payload, monkeypatch):
        monkeypatch.setattr(models, "utc_today", lambda: date(2026, 12, 31))
        payment_payload.update(expiry_month=1, expiry_year=2027)
        assert PaymentRequest.model_validate(payment_payload).expiry_year == 2027
        monkeypatch.setattr(models, "utc_today", lambda: date(2027, 1, 1))
        payment_payload.update(expiry_month=12, expiry_year=2026)
        with pytest.raises(ValidationError):
            PaymentRequest.model_validate(payment_payload)

    def test_default_clock_uses_utc(self, monkeypatch):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                assert tz is timezone.utc
                return cls(2026, 12, 31, 23, 59, tzinfo=tz)

        # Restore the real clock helper beneath the test-wide fixed-date override.
        monkeypatch.undo()
        monkeypatch.setattr(models, "datetime", FixedDatetime)
        assert models.utc_today().isoformat() == "2026-12-31"


class TestCurrency:
    """Accept only GBP, USD and EUR as exact uppercase strings."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("JPY", id="currency-unsupported"),
            pytest.param("gbp", id="currency-lowercase"),
            pytest.param("US", id="currency-too-short"),
            pytest.param("USDD", id="currency-too-long"),
            pytest.param("GBP\n", id="currency-newline"),
            pytest.param(123, id="currency-integer"),
        ],
    )
    def test_invalid_currency_is_rejected(self, payment_payload, value):
        payment_payload["currency"] = value
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["loc"] == ("currency",)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("USD", id="currency-USD"),
            pytest.param("EUR", id="currency-EUR"),
            pytest.param("GBP", id="currency-GBP"),
        ],
    )
    def test_valid_currency_is_preserved(self, payment_payload, value):
        payment_payload["currency"] = value
        payment = PaymentRequest.model_validate(payment_payload)
        assert payment.model_dump() == payment_payload


class TestAmount:
    """Require positive integers in minor units, without coercion."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(0, id="amount-zero"),
            pytest.param(-1, id="amount-negative"),
            pytest.param(100.0, id="amount-whole-float"),
            pytest.param("100", id="amount-string"),
            pytest.param(True, id="amount-boolean"),
        ],
    )
    def test_invalid_amount_is_rejected(self, payment_payload, value):
        payment_payload["amount"] = value
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["loc"] == ("amount",)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(1, id="amount-minimum"),
            pytest.param(2**63, id="amount-no-artificial-ceiling"),
        ],
    )
    def test_valid_amount_is_preserved(self, payment_payload, value):
        payment_payload["amount"] = value
        payment = PaymentRequest.model_validate(payment_payload)
        assert payment.model_dump() == payment_payload


class TestCvv:
    """Strict strings of three or four ASCII digits; preserve leading zeros."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("12", id="cvv-too-short"),
            pytest.param("12345", id="cvv-too-long"),
            pytest.param("ab3", id="cvv-letters"),
            pytest.param("１２３", id="cvv-unicode-digits"),
            pytest.param("123\n", id="cvv-newline"),
            pytest.param(123, id="cvv-integer"),
        ],
    )
    def test_invalid_cvv_is_rejected(self, payment_payload, value):
        payment_payload["cvv"] = value
        with pytest.raises(ValidationError) as error:
            PaymentRequest.model_validate(payment_payload)
        assert error.value.errors()[0]["loc"] == ("cvv",)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("012", id="cvv-three-digits-leading-zero"),
            pytest.param("0012", id="cvv-four-digits-leading-zeros"),
        ],
    )
    def test_valid_cvv_is_preserved(self, payment_payload, value):
        payment_payload["cvv"] = value
        payment = PaymentRequest.model_validate(payment_payload)
        assert payment.model_dump() == payment_payload


class TestSensitiveRepresentation:
    """Keep raw card data out of the request representation."""

    def test_request_repr_omits_sensitive_values(self, payment_payload):
        request = PaymentRequest(**payment_payload)
        assert request.card_number not in repr(request)
        assert "card_number=" not in repr(request)
        assert "cvv=" not in repr(request)


class TestPaymentResponse:
    """Only immutable, processed payment results can be stored."""

    def test_rejected_cannot_be_a_stored_payment_status(self, payment_payload):
        details = {
            key: value
            for key, value in payment_payload.items()
            if key not in {"card_number", "cvv"}
        }
        details.update(id=str(uuid4()), status="Rejected", card_number_last_four="8877")
        with pytest.raises(ValidationError) as error:
            PaymentResponse.model_validate(details)
        assert error.value.errors()[0]["loc"] == ("status",)

    def test_stored_response_is_immutable(self):
        payment = PaymentResponse(
            id=uuid4(),
            status=PaymentStatus.DECLINED,
            card_number_last_four="0012",
            expiry_month=1,
            expiry_year=2030,
            currency="EUR",
            amount=1,
        )
        with pytest.raises(ValidationError, match="frozen_instance"):
            payment.amount = 100
        assert payment.amount == 1
