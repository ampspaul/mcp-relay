.PHONY: install dev lint test run docker-build docker-run

install:
	pip3 install -e .

dev:
	pip3 install -e ".[dev]"

lint:
	ruff check src/ tests/
	mypy src/

test:
	python3 -m pytest tests/

run:
	python3 -m mcp_relay.main

docker-build:
	docker build -t mcp-relay .

docker-run:
	docker run --env-file .env -p 8080:8080 mcp-relay
