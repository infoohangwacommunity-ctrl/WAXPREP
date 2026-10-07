.PHONY: check format lint types test test-live

check: format lint types test

format:
	uv run ruff format --check .

lint:
	uv run ruff check .

types:
	uv run pyright

test:
	uv run pytest -m "not live"

test-live:
	WAXPREP_RUN_LIVE_TESTS=1 uv run pytest -m live
