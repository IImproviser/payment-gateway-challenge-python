"""Domain exceptions and safe HTTP error responses."""

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from payment_gateway_api.models import (
    ErrorBody,
    ErrorDetail,
    ErrorResponse,
    RejectedResponse,
)

LOG = logging.getLogger(__name__)


class BankError(Exception):
    """A bank interaction failed; reasons contain no raw request or response."""

    def __init__(self, reason: str, status_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class PaymentNotFound(Exception):
    """No stored payment exists for the supplied UUID."""


def validation_details(exc: RequestValidationError) -> list[ErrorDetail]:
    """Use fixed field names/messages instead of echoing raw validation data."""
    details = []
    messages = {
        "card_number": "Must contain 14 to 19 ASCII digits as a string.",
        "expiry_month": "Must be an integer between 1 and 12.",
        "expiry_year": "Must be an integer between 1 and 9999.",
        "currency": "Must be one of GBP, USD or EUR.",
        "amount": "Must be a positive integer in minor currency units.",
        "cvv": "Must contain 3 or 4 ASCII digits as a string.",
        "payment_id": "Must be a valid UUID.",
    }
    for error in exc.errors():
        # Expiry is a combined rule: both fields may need correction, even
        # when the year is current and only the month has passed.
        if error.get("type") == "card_expired":
            details.extend(
                ErrorDetail(
                    field=field,
                    code="InvalidValue",
                    message="The combined expiry month and year must not be in the past.",
                )
                for field in ("expiry_month", "expiry_year")
            )
            continue
        location = error.get("loc", ())
        candidate = location[1] if len(location) > 1 else None
        field = candidate if candidate in messages else "request"
        if error.get("type") == "missing":
            code, message = "Required", "Required field is missing."
        else:
            code = "InvalidValue"
            message = messages.get(field, "Request body must be a valid JSON object.")
        details.append(ErrorDetail(field=field, code=code, message=message))
    return details


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        # Invalid UTF-8 can fail during body parsing, before Pydantic validation.
        # Keep FastAPI's HTTP 400, but return a safe Rejected body for payments.
        if (
            exc.status_code == 400
            and request.method == "POST"
            and request.url.path == "/payments"
        ):
            result = RejectedResponse(
                error=ErrorBody(
                    code="InvalidPaymentRequest",
                    message="Payment request is invalid.",
                    details=[
                        ErrorDetail(
                            field="request",
                            code="InvalidValue",
                            message="Request body must be a valid JSON object.",
                        )
                    ],
                )
            )
            return JSONResponse(
                status_code=400, content=jsonable_encoder(result, exclude_none=True)
            )
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        # Keep FastAPI's default 422 while sanitizing the validation error body.
        # Only invalid payment creation is Rejected; bad lookup IDs are API errors.
        is_payment = request.method == "POST" and request.url.path == "/payments"
        body = ErrorBody(
            code="InvalidPaymentRequest" if is_payment else "InvalidRequest",
            message="Payment request is invalid."
            if is_payment
            else "Request is invalid.",
            details=validation_details(exc),
        )
        result = (
            RejectedResponse(error=body) if is_payment else ErrorResponse(error=body)
        )
        return JSONResponse(
            status_code=422, content=jsonable_encoder(result, exclude_none=True)
        )

    @app.exception_handler(PaymentNotFound)
    async def handle_payment_not_found(request: Request, exc: PaymentNotFound):
        result = ErrorResponse(
            error=ErrorBody(code="PaymentNotFound", message="Payment was not found.")
        )
        return JSONResponse(
            status_code=404, content=jsonable_encoder(result, exclude_none=True)
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        # Keep programming/storage errors as 500; exception text may contain secrets.
        LOG.error("Unhandled gateway error: exception_type=%s", type(exc).__name__)
        result = ErrorResponse(
            error=ErrorBody(
                code="InternalServerError", message="An internal error occurred."
            )
        )
        return JSONResponse(
            status_code=500, content=jsonable_encoder(result, exclude_none=True)
        )
