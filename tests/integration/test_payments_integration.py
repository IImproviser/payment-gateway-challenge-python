"""POST/GET payment flows with the supplied Docker bank; no bank methods are mocked."""

from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.bank


def assert_created_payment(response, payload, status):
    """Check the payment creation response; each test performs its own GET."""
    assert response.status_code == 201
    body = response.json()
    assert UUID(body["id"]).version == 4
    assert body == {
        "id": body["id"],
        "status": status,
        "card_number_last_four": payload["card_number"][-4:],
        "expiry_month": payload["expiry_month"],
        "expiry_year": payload["expiry_year"],
        "currency": payload["currency"],
        "amount": payload["amount"],
    }
    assert response.headers["location"] == "/payments/" + body["id"]
    assert payload["card_number"] not in response.text


class TestPaymentsIntegration:
    """Core payment flows against Docker: authorization, decline, failure and rejection."""

    @pytest.mark.parametrize(
        "card_number,status",
        [
            pytest.param("2222405343248877", "Authorized", id="odd-card-authorized"),
            pytest.param("2222405343248878", "Declined", id="even-card-declined"),
        ],
    )
    def test_bank_result_is_stored_and_queryable(
        self, simulator_client, payment_payload, card_number, status
    ):
        payment_payload["card_number"] = card_number
        response = simulator_client.post("/payments", json=payment_payload)
        assert_created_payment(response, payment_payload, status)

        queried = simulator_client.get(response.headers["location"])
        assert queried.status_code == 200
        assert queried.json() == response.json()

    def test_bank_503_creates_queryable_decline(
        self, simulator_client, payment_payload, caplog
    ):
        # The supplied bank returns HTTP 503 when the card ends in zero.
        payment_payload["card_number"] = "2222405343248870"
        response = simulator_client.post("/payments", json=payment_payload)
        assert_created_payment(response, payment_payload, "Declined")
        assert "reason=HttpStatus http_status=503" in caplog.text

        queried = simulator_client.get(response.headers["location"])
        assert queried.status_code == 200
        assert queried.json() == response.json()

    def test_invalid_payment_is_rejected_without_creating_record(
        self, simulator_client, payment_payload
    ):
        payment_payload["card_number"] = "123"
        response = simulator_client.post("/payments", json=payment_payload)
        assert response.status_code == 422
        assert response.json()["status"] == "Rejected"
        assert response.json()["error"]["code"] == "InvalidPaymentRequest"
        assert "id" not in response.json()
        assert simulator_client.app.state.repository._payments == {}

    @pytest.mark.parametrize(
        "payment_id,status,code",
        [
            pytest.param(str(uuid4()), 404, "PaymentNotFound", id="unknown-id"),
            pytest.param("not-a-uuid", 422, "InvalidRequest", id="invalid-id"),
        ],
    )
    def test_lookup_error_returns_expected_response(
        self, simulator_client, payment_id, status, code
    ):
        response = simulator_client.get("/payments/" + payment_id)
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert "status" not in response.json()
