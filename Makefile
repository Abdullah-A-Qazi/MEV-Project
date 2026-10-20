# MEV Monitor. Run `make help` for the list of targets.
# Every target runs through uv, so it uses the exact pinned versions in uv.lock.

.PHONY: help setup test lint format sample collect collect-wait data detect profit mempool simulate audit results credits fixtures

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
	@echo "mempool   download Mempool Dumpster days (about 6 GB) and index them (RQ2)"
	@echo "simulate  replay a sample of sandwiches on a local Anvil fork (RQ1; needs Foundry)"
	@echo "audit     write the hand-labelling sheets into validation/labels/"
	@echo "results   rebuild every result and figure from cached data, no network"
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

mempool:
	uv run python -m mempool.dumpster download
	uv run python -m mempool.dumpster index

simulate:
	uv run python -m profit.simulate

audit:
	uv run python -m validation.audit sample

results:
	uv run python -m collector build
	uv run python -m detectors --offline
	uv run python -m profit --offline --trace-provider drpc
	uv run python -m mempool.dumpster index
	uv run python -m mempool.visibility
	uv run python -m mitigation.dataset
	uv run python -m analysis.summary
	uv run python -m analysis.rq1
	uv run python -m analysis.rq2
	uv run python -m analysis.rq3
	uv run python -m validation.audit score

credits:
	uv run python -m collector credits

fixtures:
	uv run python -m validation.build_v2_golden
