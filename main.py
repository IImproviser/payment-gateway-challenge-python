"""Local development entrypoint for the payment gateway."""

import uvicorn


def main() -> None:
    """Run the development server; reload clears the in-memory payment history."""
    uvicorn.run(
        app="payment_gateway_api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        # Multiple workers would have separate dictionaries and inconsistent lookup.
        workers=1,
    )


if __name__ == "__main__":
    main()
