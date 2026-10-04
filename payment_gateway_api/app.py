"""Small HTTP routes with application-owned bank and repository instances."""

import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Request, Response

from payment_gateway_api.bank_client import BankClient, BankSettings
from payment_gateway_api.exceptions import (
    BankError,
    PaymentNotFound,
    register_exception_handlers,
)
from payment_gateway_api.models import (
    ErrorResponse,
    PaymentRequest,
    PaymentResponse,
    PaymentStatus,
    RejectedResponse,
)
from payment_gateway_api.repository import PaymentsRepository

LOG = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Reuse one bank connection pool and repository until application shutdown."""
    settings = BankSettings.from_env()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.timeout_seconds),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        app.state.repository = PaymentsRepository()
        app.state.bank_client = BankClient(client, settings.base_url)
        yield


async def get_bank_client(request: Request) -> BankClient:
    return request.app.state.bank_client


async def get_repository(request: Request) -> PaymentsRepository:
    return request.app.state.repository


def create_app() -> FastAPI:
    """Build an app with a bank client and repository owned by its lifespan."""
    app = FastAPI(title="Payment Gateway", version="0.1.0", lifespan=lifespan)
    register_exception_handlers(app)

    @app.get("/")
    async def ping() -> dict[str, str]:
        return {"app": "payment-gateway-api"}

    @app.post(
        "/payments",
        status_code=201,
        response_model=PaymentResponse,
        responses={
            400: {
                "model": RejectedResponse,
                "description": "Invalid request encoding; bank not called.",
            },
            422: {
                "model": RejectedResponse,
                "description": "Invalid JSON or payment fields; bank not called.",
            },
            500: {"model": ErrorResponse, "description": "Unexpected gateway failure."},
        },
    )
    async def process_payment(
        payment_request: PaymentRequest,
        response: Response,
        bank: BankClient = Depends(get_bank_client),
        repository: PaymentsRepository = Depends(get_repository),
    ) -> PaymentResponse:
        """Validate first, make one bank attempt, then store a masked result."""
        try:
            status = await bank.process(payment_request)
        except BankError as exc:
            # Take-home convention: bank failures become queryable Declined results.
            # After a timeout, this does not prove the bank never authorized payment.
            LOG.warning(
                "Bank call failed: reason=%s http_status=%s",
                exc.reason,
                exc.status_code,
            )
            status = PaymentStatus.DECLINED
        # Each POST is a separate attempt; this ID supports lookup, not deduplication.
        payment = PaymentResponse(
            id=uuid4(),
            status=status,
            card_number_last_four=payment_request.card_number[-4:],
            expiry_month=payment_request.expiry_month,
            expiry_year=payment_request.expiry_year,
            currency=payment_request.currency,
            amount=payment_request.amount,
        )
        # Save before responding so the returned Location can be queried immediately.
        repository.save(payment)
        response.headers["Location"] = "/payments/{}".format(payment.id)
        LOG.info(
            "Payment created: payment_id=%s status=%s", payment.id, payment.status.value
        )
        return payment

    @app.get(
        "/payments/{payment_id}",
        response_model=PaymentResponse,
        responses={
            422: {"model": ErrorResponse, "description": "Invalid payment ID."},
            404: {"model": ErrorResponse, "description": "Payment does not exist."},
            500: {"model": ErrorResponse, "description": "Unexpected gateway failure."},
        },
    )
    async def get_payment(
        payment_id: UUID,
        repository: PaymentsRepository = Depends(get_repository),
    ) -> PaymentResponse:
        """Read the original result without calling the bank or rechecking expiry."""
        payment = repository.get(payment_id)
        if payment is None:
            raise PaymentNotFound()
        return payment

    return app


app = create_app()
