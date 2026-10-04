"""Real BankClient behavior with simulated HTTP responses; no network requests."""

import json
from unittest.mock import Mock

import httpx
import pytest

from payment_gateway_api.bank_client import BankClient
from payment_gateway_api.exceptions import BankError
from payment_gateway_api.models import PaymentRequest, PaymentStatus

pytestmark = pytest.mark.anyio


@pytest.fixture
def bank_http():
    """HTTP response handler mock; it receives a Request and returns a Response."""
    return Mock(return_value=httpx.Response(200, json={"authorized": True}))


@pytest.fixture
async def bank_client(bank_http):
    # This BankClient is real. Only sending bytes over the network is replaced.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(bank_http), follow_redirects=False
    ) as client:
        yield BankClient(client, "http://bank.test/")


class TestBankRequest:
    """Convert a gateway payment into the bank HTTP request."""

    @pytest.mark.parametrize(
        "authorized,expected_status",
        [
            pytest.param(True, PaymentStatus.AUTHORIZED, id="authorized"),
            pytest.param(False, PaymentStatus.DECLINED, id="declined"),
        ],
    )
    async def test_bank_http_request_and_authorization_mapping(
        self, bank_client, bank_http, payment_payload, authorized, expected_status
    ):
        # Arrange: choose the HTTP response; BankClient.process remains real.
        payment_payload.update(expiry_month=1, private_metadata="do not forward")
        bank_http.return_value = httpx.Response(
            200, json={"authorized": authorized, "authorization_code": "ignored"}
        )

        # Act: convert the request, send it through MockTransport, parse the response.
        status = await bank_client.process(PaymentRequest(**payment_payload))

        # Assert: check the payment result and the outgoing HTTP request.
        assert status == expected_status
        bank_http.assert_called_once()
        request = bank_http.call_args.args[0]
        assert request.method == "POST"
        assert str(request.url) == "http://bank.test/payments"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "card_number": payment_payload["card_number"],
            "expiry_date": "01/2030",
            "currency": "GBP",
            "amount": 1050,
            "cvv": "012",
        }


class TestBankResponse:
    """Accept real boolean authorization and reject unparseable/invalid bodies."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({}, id="missing-authorized"),
            pytest.param({"authorized": None}, id="null-authorized"),
            pytest.param({"authorized": "false"}, id="string-authorized"),
            pytest.param({"authorized": 1}, id="integer-authorized"),
            pytest.param([], id="non-object-response"),
        ],
    )
    async def test_bank_authorization_must_be_a_real_boolean(
        self, bank_client, bank_http, payment_payload, body
    ):
        # Strings and numbers can be truthy; neither may authorize a payment.
        bank_http.return_value = httpx.Response(
            200, content=json.dumps(body), headers={"Content-Type": "application/json"}
        )

        with pytest.raises(BankError, match="InvalidResponse"):
            await bank_client.process(PaymentRequest(**payment_payload))

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"not JSON 2222405343248877 cvv=9734", id="invalid-json"),
            pytest.param(b'{"authorized": "\xff"}', id="invalid-utf8"),
        ],
    )
    async def test_unparseable_bank_response_is_safe(
        self, bank_client, bank_http, payment_payload, body
    ):
        bank_http.return_value = httpx.Response(200, content=body)

        with pytest.raises(BankError, match="InvalidResponse") as error:
            await bank_client.process(PaymentRequest(**payment_payload))

        assert "2222405343248877" not in str(error.value)
        assert "9734" not in str(error.value)


class TestBankFailures:
    """Wrap known bank failures safely; allow programming errors to propagate."""

    @pytest.mark.parametrize(
        "status_code",
        [201, 302, 400, 503],
        ids=["unexpected-success", "redirect", "client-error", "server-error"],
    )
    async def test_unexpected_http_status_is_safe_and_not_retried(
        self, bank_client, bank_http, payment_payload, status_code
    ):
        bank_http.return_value = httpx.Response(
            status_code,
            text="secret 2222405343248877 cvv=9734",
            headers={"Location": "http://other.test/payments"},
        )

        with pytest.raises(BankError) as error:
            await bank_client.process(PaymentRequest(**payment_payload))

        assert error.value.reason == "HttpStatus"
        assert error.value.status_code == status_code
        assert "2222405343248877" not in str(error.value)
        assert "9734" not in str(error.value)
        bank_http.assert_called_once()

    @pytest.mark.parametrize(
        "exception_type,reason",
        [
            pytest.param(httpx.ConnectTimeout, "Timeout", id="connect-timeout"),
            pytest.param(httpx.ReadTimeout, "Timeout", id="read-timeout"),
            pytest.param(httpx.ConnectError, "TransportError", id="connection-error"),
            pytest.param(
                httpx.RemoteProtocolError, "TransportError", id="protocol-error"
            ),
        ],
    )
    async def test_transport_errors_are_safe_and_not_retried(
        self, bank_client, bank_http, payment_payload, exception_type, reason
    ):
        def failing_bank(request):
            raise exception_type("secret 2222405343248877 cvv=9734", request=request)

        bank_http.side_effect = failing_bank

        with pytest.raises(BankError) as error:
            await bank_client.process(PaymentRequest(**payment_payload))

        assert error.value.reason == reason
        assert error.value.status_code is None
        assert "2222405343248877" not in str(error.value)
        assert "9734" not in str(error.value)
        assert error.value.__suppress_context__
        bank_http.assert_called_once()

    async def test_unexpected_programming_error_is_not_a_bank_decline(
        self, bank_client, bank_http, payment_payload
    ):
        bank_http.side_effect = RuntimeError("Programming error.")

        with pytest.raises(RuntimeError, match="Programming error"):
            await bank_client.process(PaymentRequest(**payment_payload))
