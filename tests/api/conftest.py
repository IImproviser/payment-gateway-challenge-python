"""API fixtures: fresh application and storage, with BankClient.process mocked.

mock_bank -> app -> client. Each test receives the same mock_bank instance as
its application, and a new instance is created for the next test.
"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from payment_gateway_api.app import create_app, get_bank_client
from payment_gateway_api.bank_client import BankClient
from payment_gateway_api.models import PaymentStatus


@pytest.fixture
def mock_bank():
    """Replace only BankClient.process; async methods become AsyncMock objects."""
    mock = Mock(spec=BankClient)
    mock.process.return_value = PaymentStatus.AUTHORIZED
    return mock


@pytest.fixture
def app(mock_bank):
    """Each test owns an empty store; only the bank dependency is replaced."""
    application = create_app()

    async def override_bank():
        return mock_bank

    application.dependency_overrides[get_bank_client] = override_bank
    return application


@pytest.fixture
def client(app):
    # Start/stop the lifespan to create application state and close the HTTP pool.
    with TestClient(app) as test_client:
        yield test_client
