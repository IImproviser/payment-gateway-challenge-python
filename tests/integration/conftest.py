"""Full application fixture connected to the supplied Docker bank simulator."""

import pytest
from fastapi.testclient import TestClient

from payment_gateway_api.app import create_app


@pytest.fixture
def simulator_client():
    """Use the real BankClient on port 8080 and fresh application storage."""
    with TestClient(create_app()) as client:
        yield client
