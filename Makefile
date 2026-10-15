# MEV Monitor. Run `make help` for the list of targets.
# Every target runs through uv, so it uses the exact pinned versions in uv.lock.

.PHONY: help setup test lint format sample collect collect-wait data detect profit results credits fixtures

help:
	@echo "setup     install pinned dependencies (uv sync --locked)"
	@echo "test      run the test suite (offline)"
	@echo "lint      ruff lint + format check"
	@echo "sample    resolve config.yaml's sampling rule -> sample_blocks.csv (spends a few credits)"
	@echo "collect   fetch sampled blocks into the cache; stops before the credit limit"
	@echo "collect-wait  same, but sleeps through budget resets until the sample is complete"
	@echo "data      cache -> Parquet tables (offline)"
	@echo "detect    run detectors -> data/results/ (looks up new pools once)"
	@echo "profit    realised profit per MEV bundle, traced sample via keyless dRPC"
	@echo "results   rebuild every result from cached data, no network"
	@echo "credits   show credit usage per provider and method"
	@echo "fixtures  rebuild the golden test fixtures from mainnet (keyless public RPC)"

setup:
	uv sync --locked

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

sample:
	uv run python -m collector sample

collect:
	uv run python -m collector collect

collect-wait:
	uv run python -m collector collect --wait

data:
	uv run python -m collector build

detect:
	uv run python -m detectors

profit:
	uv run python -m profit --provider drpc --trace-provider drpc

results:
	uv run python -m collector build
	uv run python -m detectors --offline
	uv run python -m profit --offline
	uv run python -m analysis.summary

credits:
	uv run python -m collector credits

fixtures:
	uv run python -m validation.build_v2_golden
