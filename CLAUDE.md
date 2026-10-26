# MEV Monitor – Third Year Project

## What this is
A University of Manchester third-year project (supervisor: Dr Bernardo Magri). We build a tool that detects arbitrage and sandwich attacks on Ethereum mainnet (Uniswap V2 first, then V3) from historical data, and use it to answer three research questions:

1. **RQ1 – Profit estimate accuracy:** how closely do pre-execution profit estimates (AMM formula on pre-block state; Anvil forked simulation on a sample) match realised profit (token transfers − gas − builder payment)?
2. **RQ2 – Public mempool visibility:** what share of detected MEV (by count and value) never appeared in the public mempool before inclusion? Uses Flashbots Mempool Dumpster, corrected by a coverage baseline from ordinary ETH transfers.
3. **RQ3 – User mitigation:** can users reduce sandwich risk? Compare sandwich rate and victim loss for public vs private swaps, by slippage tolerance (decoded from `amountOutMin`) and trade size relative to pool liquidity. Answer: what % of victim losses was avoidable.

The scope is approved. **The mark depends on execution quality**, so correctness, testing and reproducibility matter more than adding features.

## Hard constraints
- **Free tiers only.** Infura Core (3M credits/day, 500/sec) as the main RPC, Alchemy free (30M CUs/month) as backup. No paid services. No own node.
- **Never fetch the same data twice.** Cache every RPC response locally (Parquet/SQLite). The collector must track credits and stop before the daily limit, then resume.
- **Expensive calls (traces, Anvil forks) only on samples.** Analytical methods for everything else.
- **Detection and measurement only.** Never write code that sends transactions or executes MEV on mainnet. No private keys anywhere.
- **Secrets:** API keys live in `.env` (gitignored). Provide `.env.example`.

## Stack
Python, web3.py, DuckDB + pandas, statsmodels/scipy, Foundry/Anvil, matplotlib, pytest, GitHub Actions.

## Repo layout
```
collector/   detectors/   profit/   mempool/   mitigation/
validation/  analysis/    tests/    data/ (gitignored)
Makefile     config.yaml  README.md CLAUDE.md
```

## Quality rules (follow every time)
- **Tests first for maths.** AMM formulas must have golden tests against real on-chain swaps with known outputs.
- **Detectors** must have tests on hand-picked known sandwiches and known non-sandwiches.
- **Reproducible:** `make results` rebuilds every figure from cached data. Pinned dependencies. Block sample and seeds in `config.yaml`.
- **Statistics:** report n, median and mean, and bootstrap confidence intervals for headline numbers.
- **Data provenance:** record source, download date and block range for every dataset.
- Small, reviewed steps. Explain design choices so they can go into the decision log.
- Prefer clear, documented code over clever code – it will be read by markers.

## Build order
1. Repo scaffold: Makefile, config, tests, CI, `.env.example`.
2. Collector + cache + credit counter (start daily collection early).
3. Uniswap V2 analytical maths with golden tests.
4. Arbitrage detector → sandwich detector (must identify the victim tx).
5. Realised profit; Anvil simulation on a sample (RQ1).
6. Mempool matcher for MEV txs and all user swaps + coverage baseline (RQ2).
7. Slippage decoder (V2 Router first, then Universal Router), victim loss, mitigation analysis (RQ3).
8. Validation: labelled data (ZeroMEV or hand-labelled), precision/recall, manual audit of 100+100.
9. Uniswap V3 support.
10. Stretch: sandwich risk checker CLI.

## Project notes
Full planning notes (methodology, caveats, timeline, decisions) are in the Obsidian vault at:
`/Users/abdullahqazi/Documents/Personal/MEV Project/`
Key notes: `01 Project/Execution Standard.md`, `03 Methodology/RQ1–RQ3`, `04 Build/Components.md`.

## Commands
- `make setup` / `make test` / `make lint` – install pinned deps, run tests (offline), lint.
- `make sample` → `make collect` (or `collect-wait`) → `make data` → `make detect` → `make results`.
- `make credits` – credit usage per provider; `make fixtures` – rebuild golden fixtures.
- Packages are top-level (`collector/`, `detectors/`, ...); run modules with `uv run python -m <pkg>`.

## Conventions in the code
- Addresses and hashes are lowercase hex strings everywhere; checksum only for display.
- Token amounts are Python ints in code and decimal strings in Parquet (uint256 overflows int64).
- All RPC goes through `collector.rpc.RpcClient` (cache → credit check → network). Never call a
  provider directly. Only data from blocks ≥ 64 behind head is cached.
- Pools are trusted only after a CREATE2 address check against a known factory (`collector/pools.py`).
- Fixtures in `tests/fixtures/` are committed and record their provenance; CI never hits the network.

## Current status (26 Oct 2026)
- [x] Steps 1–9 done on the full sample (7,800 blocks, Jan–Jun 2024, Infura only):
      collector, V2 + V3 maths (golden: 48 V2, 60 V3 real swaps), detectors on V2 + V3
      (6,975 sandwiches, 1,591 arbitrages), realised profit (golden vs balances), RQ1
      estimates + Anvil sample, RQ2 mempool visibility, RQ3 decoder (V2 + V3 routers),
      safe threshold, regression, avoidable loss, robustness checks, risk checker CLI.
- Bundle completeness: no undetected swaps AND no transfers to non-detected counterparties
  (`unexplained_transfers`); 3,989 of 5,406 bundles qualify.
- V3 replays are single-range; refused when active liquidity changed. Without RPC, V3
  pre-block state is inferred from the pool's first swap (`profit/v3_state.py`).
- [ ] Validation: label `validation/labels/*.csv` (user), then `python -m validation.audit score`.
      ZeroMEV API unreachable (26 Oct 2026), so no labelled-data comparison.
- `make results` rebuilds everything offline (about 7 minutes).
- pandas 3 returns NaN, not None, for missing strings: use `common.nulls.is_missing`.
- Deadline target: core build and first results for all RQs by 20 Dec 2026.
