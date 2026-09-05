.PHONY: install-dev test lint format-check typecheck check ci

install-dev:
	uv pip install -e '.[dev,caveats,drf,strawberry]'

test:
	uv run --no-sync pytest -q

lint:
	uv run --no-sync ruff check src/ tests/

format-check:
	uv run --no-sync ruff format --check src/ tests/

typecheck:
	uv run --no-sync mypy --strict src/
	uv run --no-sync pyright src/

check: lint format-check typecheck test

ci: install-dev check
