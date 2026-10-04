"""Strict request validation and the public, masked payment contract."""

from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


class PaymentStatus(str, Enum):
    AUTHORIZED = "Authorized"
    DECLINED = "Declined"
    REJECTED = "Rejected"


# Rejected requests never create a payment, so stored results exclude that status.
ProcessedPaymentStatus = Literal[PaymentStatus.AUTHORIZED, PaymentStatus.DECLINED]


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


class PaymentRequest(BaseModel):
    """Incoming payment data; full PAN and CVV are used only for the bank request."""

    model_config = ConfigDict(extra="ignore")

    # Strict fields reject coercion; strings preserve leading zeros. Rust's \z
    # requires the actual end of input, including rejection of trailing newlines.
    card_number: str = Field(
        strict=True, min_length=14, max_length=19, pattern=r"\A[0-9]+\z", repr=False
    )
    expiry_month: int = Field(strict=True, ge=1, le=12)
    expiry_year: int = Field(strict=True, ge=1, le=9999)
    currency: str = Field(strict=True, pattern=r"\A(?:GBP|USD|EUR)\z")
    amount: int = Field(strict=True, gt=0)
    cvv: str = Field(
        strict=True, min_length=3, max_length=4, pattern=r"\A[0-9]+\z", repr=False
    )

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        """Check the combined expiry only after individual fields are valid."""
        today = utc_today()
        # A card remains valid through its expiry month, using the UTC calendar.
        if (self.expiry_year, self.expiry_month) < (today.year, today.month):
            # A stable code lets the safe HTTP handler identify the business rule.
            raise PydanticCustomError("card_expired", "Card has expired.")
        return self


class PaymentResponse(BaseModel):
    """Store/return masked details without revalidating historical card expiry."""

    # The repository returns this object directly; protect stored details.
    model_config = ConfigDict(frozen=True)

    id: UUID
    status: ProcessedPaymentStatus
    card_number_last_four: str = Field(strict=True, pattern=r"\A[0-9]{4}\z")
    expiry_month: int = Field(strict=True, ge=1, le=12)
    expiry_year: int = Field(strict=True, ge=1, le=9999)
    currency: str = Field(strict=True, pattern=r"\A(?:GBP|USD|EUR)\z")
    amount: int = Field(strict=True, gt=0)


class ErrorDetail(BaseModel):
    field: str
    code: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class RejectedResponse(ErrorResponse):
    status: Literal[PaymentStatus.REJECTED] = PaymentStatus.REJECTED
