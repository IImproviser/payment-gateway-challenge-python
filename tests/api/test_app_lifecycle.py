"""Application resource lifecycle and instance isolation, without bank network I/O."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from payment_gateway_api.app import create_app
from payment_gateway_api.models import PaymentResponse, PaymentStatus


def test_http_client_is_shared_configured_and_closed_on_shutdown(monkeypatch):
    """The app owns one pool/repository and releases the pool when its lifespan ends."""
    monkeypatch.setenv("BANK_TIMEOUT_SECONDS", "1.25")
    app = create_app()
    with TestClient(app) as client:
        http_client = app.state.bank_client._client
        repository = app.state.repository
        assert not http_client.is_closed
        assert http_client.timeout.connect == 1.25
        assert http_client.timeout.read == 1.25
        assert http_client.timeout.write == 1.25
        assert http_client.timeout.pool == 1.25
        assert not http_client.follow_redirects
        assert client.get("/").status_code == 200
        assert client.get("/").status_code == 200
        assert app.state.bank_client._client is http_client
        assert app.state.repository is repository
    assert http_client.is_closed


def test_different_app_instances_do_not_share_payment_storage():
    # An already-expired record is still queryable, but only in its owning app.
    first, second = create_app(), create_app()
    payment = PaymentResponse(
        id=uuid4(),
        status=PaymentStatus.AUTHORIZED,
        card_number_last_four="0012",
        expiry_month=1,
        expiry_year=2024,
        currency="USD",
        amount=1,
    )
    with TestClient(first) as client_one, TestClient(second) as client_two:
        first.state.repository.save(payment)
        assert client_one.get("/payments/" + str(payment.id)).status_code == 200
        assert client_two.get("/payments/" + str(payment.id)).status_code == 404


@pytest.mark.parametrize(
    "variable,value", [("BANK_BASE_URL", "not-a-url"), ("BANK_TIMEOUT_SECONDS", "nan")]
)
def test_invalid_configuration_prevents_startup(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        with TestClient(create_app()):
            pytest.fail("The app must not start with invalid configuration.")
