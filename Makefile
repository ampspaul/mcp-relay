.PHONY: install dev lint test run docker-build docker-run

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

lint:
	ruff check src/ tests/
	mypy src/

test:
	pytest tests/

run:
	python -m mcp_relay.main

docker-build:
	docker build -t mcp-relay .

docker-run:
	docker run --env-file .env -p 8080:8080 mcp-relay
