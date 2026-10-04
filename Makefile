.PHONY: install run simulator test check
install:
	@poetry install

run:
	@poetry run python main.py

test: simulator
	@poetry run python -m pytest -q --cov=payment_gateway_api --cov-report=term-missing

simulator:
	@docker compose up -d bank_simulator

check:
	@poetry check --lock
	@poetry run ruff check payment_gateway_api tests main.py
	@poetry run ruff format --check payment_gateway_api tests main.py
	@poetry run pyright
	@poetry run python -m compileall -q payment_gateway_api tests main.py
