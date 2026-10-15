# MEV Monitor

University of Manchester third-year project (supervisor: Dr Bernardo Magri).

MEV Monitor detects **arbitrage** and **sandwich attacks** on Ethereum mainnet from historical
data, then uses the results to answer three research questions:

1. **RQ1 – Profit estimate accuracy:** how closely do pre-execution profit estimates match the
   profit searchers actually make?
2. **RQ2 – Public mempool visibility:** what share of detected MEV never appeared in the public
   mempool before it was included?
3. **RQ3 – User mitigation:** can users cut their sandwich risk with private submission, tighter
   slippage or smaller trades?

The tool only detects and measures. It never sends transactions, and it needs no private keys.

## Status

| Component | State |
|---|---|
| Collector: RPC client, cache, credit counter, block sampler, Parquet tables | built, unit-tested |
| Uniswap V2 maths (`profit/uniswap_v2.py`) | built, golden-tested on 48 real swaps |
| Pool verification (CREATE2 check against the Uniswap V2 and SushiSwap factories) | built, tested |
| Arbitrage detector (V2) | built, tested on synthetic cases |
| Sandwich detector with victim identification (V2) | built, tested on synthetic cases and 3 real blocks |
| Victim loss (counterfactual without the front-run) | built, tested |
| Realised profit, Anvil simulation, mempool matcher, slippage decoder, V3 | not started |

## Reproducing

You need [uv](https://docs.astral.sh/uv/) and free API keys from Infura and/or Alchemy.

```bash
make setup                 # exact pinned dependencies from uv.lock
cp .env.example .env       # then add INFURA_API_KEY / ALCHEMY_API_KEY
make test                  # offline; all fixtures are committed
```

Pipeline (each stage reads the previous stage's output):

```bash
make sample     # config.yaml sampling rule -> sample_blocks.csv (committed)
make collect    # fetch block + receipts per sampled block into data/cache/ (spends credits)
make data       # cache -> data/processed/*.parquet (offline)
make detect     # detectors -> data/results/*.parquet (looks up each new pool once)
make results    # rebuild every result from the cache, with no network access
```

`make collect` stops before the daily credit cap and can be rerun at any time: cached blocks
cost nothing, so it simply carries on from where it stopped. `make collect-wait` sleeps
through each reset until the sample is complete. `make credits` shows what has been spent.

## How it works

```
sample_blocks.csv
   │  collector/ (RpcClient: cache → credit check → provider, with fallback)
   ▼
data/cache/rpc_cache.sqlite  ── raw responses + provider + fetch time (provenance)
   │  collector build (offline)
   ▼
data/processed/{blocks,transactions,receipts,logs}/part-*.parquet
   │  detectors/ (swaps.py → arbitrage.py, sandwich.py; mitigation/victim_loss.py)
   ▼
data/results/{swaps,arbitrages,sandwiches}.parquet → analysis/
```

### Key design decisions

- **Plain JSON-RPC rather than web3.py providers.** Every request then has an explicit credit
  cost and a raw response that can be cached. `eth_abi` and `eth_utils` from the web3.py stack
  are still used for decoding.
- **The cache holds only final data.** Responses about blocks less than 64 blocks behind the
  head (or anything that uses `latest`) are never cached, so a reorg can't leave stale data.
- **The credit ledger records each request before sending it.** If a run crashes, the ledger
  over-counts and never under-counts. Infura doesn't bill 429 or 5xx responses, so those are
  refunded.
- **Reserves come from events, not state calls.** A V2 pair emits `Sync` just before `Swap`,
  and `reserve_before = reserve_after − amountIn + amountOut` holds exactly. Reconstructing
  pool state therefore costs no RPC calls. Golden tests check this against each pool's
  previous `Sync` event.
- **Pools are verified by CREATE2.** Any contract can emit a Swap-shaped event. A pool counts
  only if its address equals the one its claimed factory would deploy for that token pair.
- **Attacker identity comes from the transaction's EOA and its target contract, never a public
  router.** This catches bots that rotate EOAs. It avoids pairing two unrelated users who both
  went through Router02.
- **The sandwich back-run must sell within ±20% of what the front-run bought.** This tolerance
  is set in `config.yaml` so results can be checked for sensitivity to it.

### Known limitations (so far)

- Only V2-style pools are decoded. Arbitrage that touches a Uniswap V3 pool is not found yet.
  In the April 2023 smoke run this probably explains why no arbitrage was detected.
- The arbitrage detector finds contiguous cycles only. It misses split-route arbitrage and
  cycles interleaved with unrelated swaps.
- Victim loss ignores liquidity changes (Mint/Burn) between the front-run and the victim.
  For exact-output victims, the loss is measured as missed output rather than extra input.
- Alchemy compute-unit costs in `config.yaml` are estimates and still need checking against
  the dashboard. Infura costs come from Infura's published table.

## Tests and fixtures

- `tests/fixtures/uniswap_v2_golden.json` holds 48 mainnet swaps made through Router02 and
  the SushiSwap router (blocks around 13.0M and 18.0M). Our `get_amount_out` and
  `get_amount_in` reproduce each one exactly. Rebuild with `make fixtures`.
- `tests/fixtures/real_blocks_17200000_17200002.json` freezes the V2 logs of three real blocks.
  It is a regression fixture: its expected sandwiches come from this detector and have not
  been hand-labelled yet.
- Both fixtures record their source, build date and block range.

## Layout

```
collector/   RPC client, cache, credit ledger, sampler, pools, pipeline (python -m collector)
detectors/   swap decoding, arbitrage, sandwich (python -m detectors)
profit/      AMM maths (Uniswap V2 now; V3, realised profit and simulation to come)
mitigation/  victim loss (slippage decoder and analysis to come)
mempool/     Mempool Dumpster matcher (to come)
validation/  fixture builders, labelled-data comparison and audit samples
analysis/    statistics (bootstrap CIs) and result summaries
common/      config, constants, provenance
tests/       pytest suite; fixtures/ are committed
```
