"""Application-owned storage; accessed only on the event-loop thread."""

from uuid import UUID

from payment_gateway_api.models import PaymentResponse


class PaymentsRepository:
    """Store masked results for one application instance.

    Calls run on the event-loop thread without await, so these operations need
    no lock. Each worker would own a separate dictionary; restarting loses it.
    """

    def __init__(self) -> None:
        self._payments: dict[UUID, PaymentResponse] = {}

    def save(self, payment: PaymentResponse) -> None:
        self._payments[payment.id] = payment

    def get(self, payment_id: UUID) -> PaymentResponse | None:
        return self._payments.get(payment_id)
