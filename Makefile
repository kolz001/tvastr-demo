.PHONY: help install sync lint fmt typecheck test demo run up down clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install sync:  ## Create venv and install all deps (incl. dev)
	uv sync --extra dev

lint:  ## Lint with ruff
	uv run ruff check src tests

fmt:  ## Auto-format with ruff
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:  ## Static type check with mypy
	uv run mypy

test:  ## Run the test suite
	uv run pytest

demo:  ## Run the end-to-end pipeline against sample logs (mock mode)
	uv run python -m tvastr.cli demo

run:  ## Start the FastAPI server (http://localhost:8000)
	uv run uvicorn tvastr.api.app:create_app --factory --reload

up:  ## Start local infra (OpenSearch + dashboards)
	docker compose up -d

down:  ## Stop local infra
	docker compose down

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
