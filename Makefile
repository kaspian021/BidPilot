.PHONY: install dev test lint format check demo docker clean

install:
	pip install -e .

dev:
	pip install -e ".[dev,postgres]"

test:
	python -m pytest -q

lint:
	ruff check src tests

format:
	ruff format src tests

check: lint test

fmt-check:
	ruff format --check src tests

demo:
	bidpilot demo

docker:
	docker compose up -d --build

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache data/demo.sqlite3
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
