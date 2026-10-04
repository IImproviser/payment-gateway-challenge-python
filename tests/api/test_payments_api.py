"""In-process HTTP contracts: real routes, validators and storage, with BankClient.process mocked."""

import logging
from datetime import date
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from payment_gateway_api import models
from payment_gateway_api.exceptions import BankError
from payment_gateway_api.models import PaymentRequest, PaymentStatus

FIELDS = {
    "id",
    "status",
    "card_number_last_four",
    "expiry_month",
    "expiry_year",
    "currency",
    "amount",
}


def assert_rejected(response, mock_bank, repository, status_code=422):
    """Common rejection contract; individual tests check their specific error details."""
    body = response.json()
    assert response.status_code == status_code
    assert body["status"] == "Rejected"
    assert body["error"]["code"] == "InvalidPaymentRequest"
    assert "id" not in body
    mock_bank.process.assert_not_awaited()
    assert repository._payments == {}


class TestPaymentValidation:
    """Invalid payment requests are Rejected without bank calls or stored records."""

    @pytest.mark.parametrize(
        "field,value,message",
        [
            ("card_number", "123", "Must contain 14 to 19 ASCII digits as a string."),
            ("amount", True, "Must be a positive integer in minor currency units."),
            ("expiry_month", 13, "Must be an integer between 1 and 12."),
            ("expiry_year", 0, "Must be an integer between 1 and 9999."),
        ],
        ids=[
            "invalid-card-format",
            "invalid-amount-type",
            "invalid-month",
            "invalid-year",
        ],
    )
    def test_invalid_field_is_rejected_without_side_effects(
        self, client, mock_bank, payment_payload, field, value, message
    ):
        """Representative field errors identify one field and have no bank/storage effects."""
        payment_payload[field] = value
        response = client.post("/payments", json=payment_payload)
        assert_rejected(response, mock_bank, client.app.state.repository)
        assert response.json()["error"]["details"] == [
            {"field": field, "code": "InvalidValue", "message": message}
        ]

    @pytest.mark.parametrize(
        "month,year",
        [(9, 2026), (12, 2025)],
        ids=["previous-month-in-current-year", "past-year"],
    )
    def test_expired_card_identifies_both_expiry_fields(
        self, client, mock_bank, payment_payload, month, year
    ):
        """Expiry belongs to the month/year pair, even when only the month is past."""
        payment_payload.update(expiry_month=month, expiry_year=year)
        response = client.post("/payments", json=payment_payload)
        assert_rejected(response, mock_bank, client.app.state.repository)
        assert response.json()["error"]["details"] == [
            {
                "field": field,
                "code": "InvalidValue",
                "message": "The combined expiry month and year must not be in the past.",
            }
            for field in ("expiry_month", "expiry_year")
        ]

    def test_missing_field_is_required_without_side_effects(
        self, client, mock_bank, payment_payload
    ):
        # Unit tests cover all required fields; check the API's missing-field error mapping.
        del payment_payload["cvv"]
        response = client.post("/payments", json=payment_payload)
        assert_rejected(response, mock_bank, client.app.state.repository)
        assert response.json()["error"]["details"] == [
            {
                "field": "cvv",
                "code": "Required",
                "message": "Required field is missing.",
            }
        ]
        assert payment_payload["card_number"] not in response.text

    @pytest.mark.parametrize(
        "body,status_code",
        [
            (b'{"card_number":"2222405343248877","cvv":"9734",', 422),
            (b'{"card_number":"\xff","cvv":"9734"}', 400),
        ],
        ids=[
            "truncated-json",
            "invalid-utf8-field",
        ],
    )
    def test_invalid_body_returns_safe_rejected_response(
        self, client, mock_bank, body, status_code
    ):
        """Keep framework 400/422 codes with safe Rejected bodies and no side effects."""
        response = client.post(
            "/payments", content=body, headers={"Content-Type": "application/json"}
        )
        assert_rejected(response, mock_bank, client.app.state.repository, status_code)
        assert "2222405343248877" not in response.text
        assert "9734" not in response.text

    def test_array_body_is_rejected_without_side_effects(self, client, mock_bank):
        # One non-object input is enough to exercise the safe request-level error response.
        response = client.post("/payments", json=["2222405343248877"])
        assert_rejected(response, mock_bank, client.app.state.repository)
        assert response.json()["error"]["details"] == [
            {
                "field": "request",
                "code": "InvalidValue",
                "message": "Request body must be a valid JSON object.",
            }
        ]
        assert "2222405343248877" not in response.text


class TestPaymentCreation:
    """POST /payments: bank outcomes, stored results and separate attempts."""

    @pytest.mark.parametrize(
        "status",
        [PaymentStatus.AUTHORIZED, PaymentStatus.DECLINED],
        ids=["authorized", "declined"],
    )
    def test_create_and_retrieve_payment(
        self, client, mock_bank, payment_payload, status
    ):
        """Verify the full HTTP contract and ensure lookup does not call the bank again."""
        # A string is required to preserve zeros in the masked last four digits.
        payment_payload["card_number"] = "2222405343240012"
        mock_bank.process.return_value = status
        created = client.post("/payments", json=payment_payload)
        assert created.status_code == 201
        body = created.json()
        assert set(body) == FIELDS
        assert UUID(body["id"]).version == 4
        assert body == {
            "id": body["id"],
            "status": status.value,
            "card_number_last_four": "0012",
            "expiry_month": 12,
            "expiry_year": 2030,
            "currency": "GBP",
            "amount": 1050,
        }
        assert created.headers["location"] == "/payments/" + body["id"]
        queried = client.get(created.headers["location"])
        assert queried.status_code == 200
        assert queried.json() == body
        mock_bank.process.assert_awaited_once()
        assert mock_bank.process.await_args.args[0].model_dump() == payment_payload
        stored = client.app.state.repository.get(UUID(body["id"]))
        assert set(stored.model_dump()) == FIELDS
        assert payment_payload["card_number"] not in created.text
        assert payment_payload["card_number"] not in queried.text

    @pytest.mark.parametrize(
        "reason,http_status",
        [
            ("HttpStatus", 503),
            ("HttpStatus", 400),
            ("Timeout", None),
            ("TransportError", None),
            ("InvalidResponse", None),
        ],
        ids=["bank-503", "bank-400", "timeout", "transport-error", "invalid-response"],
    )
    def test_bank_failures_create_queryable_declined_payment(
        self, client, mock_bank, payment_payload, reason, http_status, caplog
    ):
        # A bank technical failure creates a queryable decline under our documented assumption.
        caplog.set_level(logging.DEBUG, logger="payment_gateway_api")
        mock_bank.process.side_effect = BankError(reason, http_status)
        response = client.post("/payments", json=payment_payload)
        assert response.status_code == 201
        assert response.json()["status"] == "Declined"
        assert set(response.json()) == FIELDS
        queried = client.get(response.headers["location"])
        assert queried.status_code == 200
        assert queried.json() == response.json()
        mock_bank.process.assert_awaited_once()
        assert f"reason={reason}" in caplog.text
        assert payment_payload["card_number"] not in caplog.text
        assert "cvv=" not in caplog.text

    def test_repeated_posts_are_independent_payments(
        self, client, mock_bank, payment_payload
    ):
        """Identical payloads are separate attempts because no idempotency key exists."""
        first = client.post("/payments", json=payment_payload)
        second = client.post("/payments", json=payment_payload)
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] != second.json()["id"]
        assert mock_bank.process.await_count == 2
        assert client.get(first.headers["location"]).json() == first.json()
        assert client.get(second.headers["location"]).json() == second.json()

    def test_extra_fields_are_not_forwarded_or_stored(
        self, client, mock_bank, payment_payload
    ):
        payment_payload["private_metadata"] = {"anything": "ignored"}
        response = client.post("/payments", json=payment_payload)
        assert response.status_code == 201
        assert (
            "private_metadata" not in mock_bank.process.await_args.args[0].model_dump()
        )
        assert set(response.json()) == FIELDS


class TestRetrievePayment:
    """GET /payments/{id}: original results, missing payments and invalid IDs."""

    def test_historical_payment_is_not_revalidated(
        self, client, mock_bank, payment_payload, monkeypatch
    ):
        # Move past expiry after creation: GET must return the originally stored result.
        response = client.post("/payments", json=payment_payload)
        monkeypatch.setattr(models, "utc_today", lambda: date(2031, 1, 1))
        queried = client.get(response.headers["location"])
        assert queried.status_code == 200
        assert queried.json() == response.json()
        mock_bank.process.assert_awaited_once()

    def test_unknown_payment_is_404_without_bank_call(self, client, mock_bank):
        response = client.get("/payments/" + str(uuid4()))
        assert response.status_code == 404
        assert response.json() == {
            "error": {"code": "PaymentNotFound", "message": "Payment was not found."}
        }
        mock_bank.process.assert_not_awaited()

    @pytest.mark.parametrize(
        "payment_id",
        ["not-a-uuid", "2222405343248877"],
        ids=["invalid-format", "card-number"],
    )
    def test_invalid_id_is_safe_422_without_payment_status(
        self, client, mock_bank, payment_id
    ):
        response = client.get("/payments/" + payment_id)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "InvalidRequest"
        assert "status" not in response.json()
        assert payment_id not in response.text
        mock_bank.process.assert_not_awaited()


class TestErrorHandling:
    """Safe error responses and logs; internal failures must not report success."""

    def test_safe_validation_errors_and_logging(
        self, client, mock_bank, payment_payload, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="payment_gateway_api")
        payment_payload["cvv"] = "sensitive-cvv-value"
        response = client.post("/payments", json=payment_payload)
        assert response.status_code == 422
        assert payment_payload["card_number"] not in response.text
        assert payment_payload["cvv"] not in response.text
        assert payment_payload["card_number"] not in caplog.text
        assert payment_payload["cvv"] not in caplog.text
        mock_bank.process.assert_not_awaited()

    def test_programming_error_is_500_not_declined(
        self, app, mock_bank, payment_payload, caplog
    ):
        # Disable TestClient's exception re-raise to inspect the real HTTP 500 response.
        mock_bank.process.side_effect = RuntimeError(
            "secret 2222405343248877 sensitive-cvv-value"
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/payments", json=payment_payload)
            assert app.state.repository._payments == {}
        assert response.status_code == 500
        assert response.json() == {
            "error": {
                "code": "InternalServerError",
                "message": "An internal error occurred.",
            }
        }
        assert "2222405343248877" not in response.text + caplog.text
        assert "sensitive-cvv-value" not in response.text + caplog.text
        assert "exception_type=RuntimeError" in caplog.text

    def test_storage_failure_does_not_report_success(
        self, app, payment_payload, monkeypatch
    ):
        def failing_save(payment):
            raise RuntimeError("Storage failed.")

        with TestClient(app, raise_server_exceptions=False) as client:
            monkeypatch.setattr(app.state.repository, "save", failing_save)
            response = client.post("/payments", json=payment_payload)
            assert app.state.repository._payments == {}
        assert response.status_code == 500
        assert "status" not in response.json()


class TestApiMetadata:
    """Published API schema and the template/framework routes."""

    def test_openapi_matches_http_contract(self, client):
        """Keep published response models and status codes aligned with actual handlers."""
        schema = client.get("/openapi.json").json()
        post = schema["paths"]["/payments"]["post"]["responses"]
        get = schema["paths"]["/payments/{payment_id}"]["get"]["responses"]
        assert set(post) == {"201", "400", "422", "500"}
        assert set(get) == {"200", "422", "404", "500"}
        for status_code in ("400", "422"):
            response_schema = post[status_code]["content"]["application/json"]["schema"]
            assert response_schema["$ref"] == "#/components/schemas/RejectedResponse"
        assert get["422"]["content"]["application/json"]["schema"]["$ref"] == (
            "#/components/schemas/ErrorResponse"
        )
        definitions = schema["components"]["schemas"]
        assert set(definitions["PaymentResponse"]["properties"]) == FIELDS
        assert definitions["PaymentResponse"]["properties"]["status"]["enum"] == [
            "Authorized",
            "Declined",
        ]
        # Pydantic v2 emits a single Literal as JSON Schema const.
        assert (
            definitions["RejectedResponse"]["properties"]["status"]["const"]
            == "Rejected"
        )
        assert set(definitions["PaymentRequest"]["required"]) == set(
            PaymentRequest.model_fields
        )
        assert client.get("/docs").status_code == 200

    def test_ping_returns_template_response(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert response.json() == {"app": "payment-gateway-api"}

    def test_unknown_route_keeps_framework_404(self, client, mock_bank):
        response = client.get("/unknown-route")
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        mock_bank.process.assert_not_awaited()
