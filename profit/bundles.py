"""Groups detected MEV events into bundles: sets of events that share transactions.

Searchers batch work: one jaredfromsubway transaction can hold the front-runs of several
sandwiches plus an arbitrage cycle. Gas and token flows are only observable per
transaction, so they cannot be split between the events inside it without arbitrary rules.
We therefore measure realised profit per bundle: the connected components of the graph in
which two events are linked if they share a transaction. A lone arbitrage or a sandwich
with its own front-run and back-run is a bundle of one event.
"""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    kind: str  # "arbitrage" or "sandwich"
    event_id: str
    block_number: int
    tx_hashes: frozenset[str]


@dataclass(frozen=True)
class Bundle:
    bundle_id: str
    block_number: int
    tx_hashes: tuple[str, ...]
    events: tuple[Event, ...]

    def count(self, kind: str) -> int:
        return sum(e.kind == kind for e in self.events)


def group_into_bundles(events: Iterable[Event]) -> list[Bundle]:
    events = list(events)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for event in events:
        hashes = sorted(event.tx_hashes)
        for h in hashes:
            parent.setdefault(h, h)
        for h in hashes[1:]:
            parent[find(h)] = find(hashes[0])

    groups: dict[str, list[Event]] = {}
    for event in events:
        groups.setdefault(find(next(iter(event.tx_hashes))), []).append(event)

    bundles = []
    for members in groups.values():
        hashes = sorted({h for e in members for h in e.tx_hashes})
        blocks = {e.block_number for e in members}
        if len(blocks) != 1:
            raise ValueError(f"events sharing transactions span blocks {sorted(blocks)}")
        bundles.append(
            Bundle(
                bundle_id=hashes[0],
                block_number=blocks.pop(),
                tx_hashes=tuple(hashes),
                events=tuple(sorted(members, key=lambda e: (e.kind, e.event_id))),
            )
        )
    return sorted(bundles, key=lambda b: (b.block_number, b.bundle_id))
