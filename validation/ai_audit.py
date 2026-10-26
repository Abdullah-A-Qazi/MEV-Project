"""An independent, rule-by-rule audit of the label sheets, from raw token transfers.

    uv run python -m validation.ai_audit             fills ai_label / ai_evidence in the sheets
    uv run python -m validation.ai_audit --scan-all  the non-detection rule on every unflagged
                                                     user swap (not only the 100 sampled)

Why this exists, and its limits: the manual audit is meant to be done by a person. This is
an AI-run audit, written at the user's request, and it is kept apart from the human audit:
it writes only the `ai_label` and `ai_evidence` columns and never touches `label`. Report it
as AI-run, and spot-check it by hand.

To be as independent of the detectors as possible, it does not use the detectors' Swap
decoding, roles or results. It re-derives every fact from the raw ERC-20 Transfer events
and transaction order in the cached blocks, then checks the written criteria one by one:

Sandwich sheet (is this a sandwich?) - y only if all of:
  1 same block   2 front < victim < back   3 the pool moves tokens in all three
  4 front and victim pay the pool the same token X and receive token Y
  5 the back-run pays Y and receives X; one of its Y transfers is 0.5x-1.5x one Y transfer
    the front-run received (single transfers, so batched back-runs are not summed)
  6 front and back share the sender, or the same non-router contract
  7 the victim is a different sender and does not call the attacker's contract
Non-detection sheet (was this swap NOT sandwiched?) - n if, in the same block and pool,
  one actor trades the same direction before it and the opposite direction after it
  (no amount rule: looser than the detector, so it hunts for misses); y otherwise.
Arbitrage sheet (is this an arbitrage?) - y only if all of:
  a the transaction does not call a public router (a bot, not a user)
  b the transfers contain a loop of swaps (counterparties that take one token and give
    another) through at least two pools, each hop passing on 0.5x-1.5x what the last
    produced, that ends in the token it started with
  c the loop returns more of that token than it started with (profitable)
  The loop is found among the transaction's transfers, so it still shows when a bot
  batches sandwich legs and an arbitrage into one transaction.
Unknown or ambiguous evidence gives `u` (not scored) with the reason: a human decides those.
"""

import json
from collections import defaultdict
from pathlib import Path

import duckdb
import pandas as pd

from common.config import REPO_ROOT, load_config
from common.constants import ERC20_TRANSFER_TOPIC, PUBLIC_ROUTERS
from common.nulls import is_missing
from common.routers import known_routers

LABELS_DIR = REPO_ROOT / "validation" / "labels"
ZERO = "0x" + "0" * 40


def _hash(url_or_hash: str) -> str:
    return url_or_hash.strip().split("/")[-1].lower()


class Chain:
    """Raw facts for a set of transactions, straight from the cached tables."""

    def __init__(self, txs: pd.DataFrame, transfers: pd.DataFrame):
        """txs: tx_hash, block_number, tx_index, from, to. transfers: block_number, tx_hash,
        log_index, address, src, dst, amount (one row per ERC-20 Transfer)."""
        self.txs = txs.set_index("tx_hash")
        transfers = transfers.sort_values(["block_number", "log_index"])
        self.by_tx = {h: g for h, g in transfers.groupby("tx_hash")}
        self.by_block = {b: g for b, g in transfers.groupby("block_number")}

    @classmethod
    def load(cls, processed: Path, blocks: set[int]) -> "Chain":
        con = duckdb.connect()
        con.register("blk", pd.DataFrame({"b": sorted(blocks)}))
        logs = str(processed / "logs" / "*.parquet")
        txs = str(processed / "transactions" / "*.parquet")
        tx_rows = con.sql(
            f'select tx_hash, block_number, tx_index, "from", "to" from \'{txs}\' '
            "where block_number in (select b from blk)"
        ).df()
        transfers = con.sql(
            f"select block_number, tx_hash, log_index, address, topic1, topic2, data "
            f"from '{logs}' where topic0 = '{ERC20_TRANSFER_TOPIC}' and topic3 is null "
            "and block_number in (select b from blk)"
        ).df()
        transfers["src"] = "0x" + transfers["topic1"].str[-40:]
        transfers["dst"] = "0x" + transfers["topic2"].str[-40:]
        transfers["amount"] = [int(d[2:66], 16) if isinstance(d, str) and len(d) >= 66 else 0
                               for d in transfers["data"]]  # fmt: skip
        return cls(tx_rows, transfers)

    def tx(self, h: str):
        return self.txs.loc[h] if h in self.txs.index else None

    def amounts(self, h: str, token: str, src: str | None = None, dst: str | None = None):
        """Amounts of each single `token` transfer in `h` from `src` / to `dst`."""
        g = self.by_tx.get(h)
        return [] if g is None else _amounts(g, token, src, dst)

    def pool_flows(self, h: str, pool: str) -> tuple[dict[str, int], dict[str, int]]:
        """(tokens the pool received, tokens the pool sent) in transaction `h`."""
        received, sent = defaultdict(int), defaultdict(int)
        g = self.by_tx.get(h)
        if g is not None:
            for token, src, dst, amount in zip(g["address"], g["src"], g["dst"], g["amount"],
                                               strict=True):  # fmt: skip
                if dst == pool and src != pool:
                    received[token] += amount
                if src == pool and dst != pool:
                    sent[token] += amount
        return dict(received), dict(sent)


def _amounts(g, token, src=None, dst=None):
    rows = g[(g["address"] == token)]
    if src is not None:
        rows = rows[rows["src"] == src]
    if dst is not None:
        rows = rows[rows["dst"] == dst]
    return [int(a) for a in rows["amount"]]


def direction(flows) -> tuple[str, str] | None:
    """(token paid in, token taken out) for one pool in one transaction, if clear-cut."""
    received, sent = flows
    if len(received) != 1 or len(sent) != 1:
        return None
    x, y = next(iter(received)), next(iter(sent))
    return (x, y) if x != y else None


ROUTERS = frozenset(PUBLIC_ROUTERS)  # main() adds the sample's shared contracts


def actors(tx) -> set[str]:
    ids = {tx["from"]}
    if not is_missing(tx["to"]) and tx["to"] not in ROUTERS:
        ids.add(tx["to"])
    return ids


def audit_sandwich(row, chain: Chain) -> tuple[str, str]:
    front, back = _hash(row["front_run"]), _hash(row["back_run"])
    victims = [_hash(v) for v in str(row["victims"]).split()]
    pool = row["pool"].lower()
    txs = {h: chain.tx(h) for h in [front, back, *victims]}
    if any(t is None for t in txs.values()):
        return "u", "transaction missing from the cached data"
    notes, ok = [], True

    def check(n, passed, text):
        nonlocal ok
        ok &= bool(passed)
        notes.append(f"{n}{'✓' if passed else '✗'} {text}")

    blocks = {int(t["block_number"]) for t in txs.values()}
    check(1, len(blocks) == 1, f"block {sorted(blocks)}")
    fi, bi = int(txs[front]["tx_index"]), int(txs[back]["tx_index"])
    vis = [int(txs[v]["tx_index"]) for v in victims]
    check(2, all(fi < v < bi for v in vis), f"positions front {fi}, victims {vis}, back {bi}")
    flows = {h: chain.pool_flows(h, pool) for h in txs}
    present = all(f[0] or f[1] for f in flows.values())
    check(3, present, "pool moves tokens in every transaction")
    if not present:
        return "n", "; ".join(notes)
    dirs = {h: direction(f) for h, f in flows.items()}
    if any(d is None for d in dirs.values()):
        return "u", "; ".join(notes + ["a trade's direction is not clear from transfers"])
    x, y = dirs[front]
    check(4, all(dirs[v] == (x, y) for v in victims),
          f"front and victims pay {x[:8]} for {y[:8]}")  # fmt: skip
    # Compare single transfers, not per-transaction totals: a bot that batches two back-runs
    # into one pool in one transaction would otherwise look like it sold twice as much.
    got = chain.amounts(front, y, src=pool)
    sold = chain.amounts(back, y, dst=pool)
    ratio = min((s_ / g for g in got for s_ in sold if g), key=lambda r: abs(r - 1), default=0.0)
    check(
        5, dirs[back] == (y, x) and 0.5 <= ratio <= 1.5, f"back-run sells {ratio:.2f}x the Y bought"
    )
    attacker = actors(txs[front]) & actors(txs[back])
    check(6, bool(attacker), f"shared attacker {sorted(a[:8] for a in attacker)}")
    check(7, all(not (actors(txs[v]) & (actors(txs[front]) | actors(txs[back]))) for v in victims),
          "victims are other actors")  # fmt: skip
    profit_x = chain.pool_flows(back, pool)[1].get(x, 0) - chain.pool_flows(front, pool)[0].get(
        x, 0
    )
    notes.append(f"gross {x[:8]} {profit_x:+d}")
    return ("y" if ok else "n"), "; ".join(notes)


def audit_nondetection(row, chain: Chain) -> tuple[str, str]:
    h = row["swap_id"].split(":")[0].lower()
    pool = row["pool"].lower()
    target = chain.tx(h)
    if target is None:
        return "u", "transaction missing from the cached data"
    d = direction(chain.pool_flows(h, pool))
    if d is None:
        return "u", "swap direction not clear from transfers"
    block = int(target["block_number"])
    g = chain.by_block.get(block)
    me, t_index = actors(target), int(target["tx_index"])
    before, after = defaultdict(list), defaultdict(list)
    for other in set(g.loc[(g["src"] == pool) | (g["dst"] == pool), "tx_hash"]) - {h}:
        tx = chain.tx(other)
        od = direction(chain.pool_flows(other, pool))
        if tx is None or od is None or actors(tx) & me:
            continue
        for a in actors(tx):
            if int(tx["tx_index"]) < t_index and od == d:
                before[a].append(int(tx["tx_index"]))
            if int(tx["tx_index"]) > t_index and od == (d[1], d[0]):
                after[a].append(int(tx["tx_index"]))
    wrapped = sorted(set(before) & set(after))
    if wrapped:
        a = wrapped[0]
        return "n", (f"possible missed sandwich: {a[:10]} trades the same way at {before[a]} "
                     f"and back at {after[a]} around position {t_index}")  # fmt: skip
    return "y", f"no actor brackets position {t_index} in this pool (block {block})"


def find_cycle(g: pd.DataFrame, bot: set[str], max_hops: int = 6, chain_ratio=(0.5, 1.5)):
    """A loop of swaps, read from transfers alone, that returns the start token to the bot.

    Every counterparty that both receives one token and sends a different one in the
    transaction is treated as a swap edge: token in -> token out, with the amounts. A loop is
    a chain of such edges through at least two different counterparties, each hop using
    `chain_ratio` (default 0.5x-1.5x) of what the previous hop produced, ending in the token
    it started with. Log order
    is not used: a V3 pool sends its output before it collects its input. Returns
    (token, sent, received, pools) for the most profitable loop found, or None.
    """
    edges = []
    parties = (set(g["src"]) | set(g["dst"])) - bot - {ZERO}
    for party in parties:
        received = g[(g["dst"] == party) & (g["src"] != party)].groupby("address")["amount"].sum()
        sent = g[(g["src"] == party) & (g["dst"] != party)].groupby("address")["amount"].sum()
        for tin, ain in received.items():
            for tout, aout in sent.items():
                if tin != tout and ain > 0 and aout > 0:
                    edges.append((party, tin, int(ain), tout, int(aout)))
    best = None

    def walk(path):
        nonlocal best
        first, last = path[0], path[-1]
        if len(path) >= 2 and last[3] == first[1] and len({e[0] for e in path}) == len(path):
            profit = last[4] - first[2]
            if best is None or profit > best[2] - best[1]:
                best = (first[1], first[2], last[4], [e[0] for e in path])
            return
        if len(path) == max_hops:
            return
        for e in edges:
            if (
                e[1] == last[3]
                and e[0] not in {x[0] for x in path}
                and chain_ratio[0] <= e[2] / last[4] <= chain_ratio[1]
            ):
                walk(path + [e])

    for e in edges:
        walk([e])
    return best


def audit_arbitrage(row, chain: Chain) -> tuple[str, str]:
    h = _hash(row["transaction"])
    tx = chain.tx(h)
    if tx is None:
        return "u", "transaction missing from the cached data"
    to = None if is_missing(tx["to"]) else tx["to"]
    notes = [f"a{'✓' if to not in ROUTERS else '✗'} called {str(to)[:10]}"]
    g = chain.by_tx.get(h)
    if g is None:
        return "u", "no token transfers"
    bot = {tx["from"], to} - {None}
    if to in ROUTERS:
        return "n", "; ".join(notes + ["a public router call is a user trade, not a bot"])
    cycle = find_cycle(g, bot)
    if cycle is None:
        if find_cycle(g, bot, chain_ratio=(0, float("inf"))) is not None:
            notes.append("b? a loop exists but its amounts do not chain hop to hop "
                         "(split route or bot inventory): judge by hand")  # fmt: skip
            return "u", "; ".join(notes)
        takers = set(g["dst"]) - set(g["src"]) - bot - {ZERO}
        if takers:
            notes.append(f"b? {len(takers)} counterparties take tokens and send no ERC-20 "
                         "back (non-standard token events?): judge by hand")  # fmt: skip
            return "u", "; ".join(notes)
        notes.append("b✗ no loop of swaps found in the transfers")
        return "n", "; ".join(notes)
    token, sent, received, hops = cycle
    notes.append(f"b✓ {token[:8]} loop through {len(hops)} pools")
    notes.append(f"c{'✓' if received > sent else '✗'} sent {sent}, got back {received}")
    ok = to not in ROUTERS and received > sent
    return ("y" if ok else "n"), "; ".join(notes)


def _use_shared_routers(config) -> None:
    """Treat contracts that many unrelated EOAs call as routers (common/routers.py)."""
    global ROUTERS
    ROUTERS = known_routers(config)


def main() -> None:
    config = load_config()
    _use_shared_routers(config)
    names = ("sandwich_detections", "user_swap_nondetections", "arbitrage_detections")
    sheets = {
        n: pd.read_csv(LABELS_DIR / f"{n}.csv", dtype=str, keep_default_na=False) for n in names
    }
    blocks = {int(b) for sheet in sheets.values() for b in sheet["block"]}
    chain = Chain.load(config.processed_dir, blocks)
    auditors = {"sandwich_detections": audit_sandwich,
                "user_swap_nondetections": audit_nondetection,
                "arbitrage_detections": audit_arbitrage}  # fmt: skip
    for name, sheet in sheets.items():
        results = [auditors[name](row, chain) for row in sheet.to_dict("records")]
        sheet["ai_label"] = [r[0] for r in results]
        sheet["ai_evidence"] = [r[1] for r in results]
        sheet.to_csv(LABELS_DIR / f"{name}.csv", index=False)
        counts = sheet["ai_label"].value_counts().to_dict()
        print(f"{name}: {counts}")


def scan_all_nondetections(config) -> dict:
    """The non-detection rule applied to every user swap the detector did not flag.

    One pass per (block, pool): each transaction's direction in the pool (from transfers),
    then, for each unflagged user swap, whether one actor trades the same way before it and
    the opposite way after it. Writes data/results/ai_miss_scan.parquet.
    """
    _use_shared_routers(config)
    users = pd.read_parquet(config.data_dir / "results" / "user_swaps.parquet",
                            columns=["tx_hash", "block_number", "pool", "sandwiched"])  # fmt: skip
    chain = Chain.load(config.processed_dir, set(users["block_number"].astype(int)))
    flagged = []
    for (block, pool), group in users.groupby(["block_number", "pool"]):
        g = chain.by_block.get(int(block))
        if g is None:
            continue
        trades = {}
        for h in set(g.loc[(g["src"] == pool) | (g["dst"] == pool), "tx_hash"]):
            tx, d = chain.tx(h), direction(chain.pool_flows(h, pool))
            if tx is not None and d is not None:
                trades[h] = (int(tx["tx_index"]), d, actors(tx))
        for h in group.loc[~group["sandwiched"], "tx_hash"].unique():
            if h not in trades:
                continue
            index, d, me = trades[h]
            before = {a for i, od, acts in trades.values() if i < index and od == d
                      and not acts & me for a in acts}  # fmt: skip
            after = {a for i, od, acts in trades.values() if i > index and od == (d[1], d[0])
                     and not acts & me for a in acts}  # fmt: skip
            if before & after:
                flagged.append({"tx_hash": h, "block_number": int(block), "pool": pool,
                                "bracketing_actor": sorted(before & after)[0]})  # fmt: skip
    frame = pd.DataFrame(flagged, columns=["tx_hash", "block_number", "pool", "bracketing_actor"])
    frame.to_parquet(config.data_dir / "results" / "ai_miss_scan.parquet", index=False)
    scanned = int((~users["sandwiched"]).sum())
    return {
        "unflagged_user_swaps": scanned,
        "possible_misses": len(frame),
        "share": len(frame) / scanned if scanned else None,
    }


if __name__ == "__main__":
    import sys

    if "--scan-all" in sys.argv:
        print(json.dumps(scan_all_nondetections(load_config()), indent=1))
    else:
        main()
