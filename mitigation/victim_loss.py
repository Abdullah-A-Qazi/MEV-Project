"""Victim loss for a detected sandwich (needed for RQ3).

Victim loss = output the victim would have received had the front-run not happened, minus
the output it actually received, in raw units of the victim's output token.

Counterfactual: start from the pool's reserves just before the front-run, replay every swap
that sat between the front-run and this victim (other victims included, front-run left out)
with its actual input amount, then price the victim's own input. This is exact for one
victim with nothing else in between. With several victims, the earlier victims' replayed
outputs also change, which is the right counterfactual for "no front-run at all".

Limitations: liquidity added or removed between the swaps (Mint/Burn) is not replayed, and
for exact-output victims the loss is expressed as missed output rather than extra input paid.
"""

from dataclasses import dataclass

from detectors.sandwich import Sandwich
from detectors.swaps import Swap
from profit.uniswap_v2 import InsufficientLiquidity, apply_swap


@dataclass(frozen=True)
class VictimLoss:
    victim_tx_hash: str
    token_out: str
    actual_out: int
    counterfactual_out: int

    @property
    def loss(self) -> int:
        return self.counterfactual_out - self.actual_out

    @property
    def loss_fraction(self) -> float:
        return self.loss / self.counterfactual_out if self.counterfactual_out else 0.0


def victim_losses(sandwich: Sandwich, pool_swaps: list[Swap], market=None) -> list[VictimLoss]:
    """Loss of each victim in `sandwich`. `pool_swaps` = all swaps in that pool and block.

    V2 needs nothing else. V3 needs `market` (profit.market.Market) for the state before the
    front-run and the pool's fee; it is computed only if the real swaps from the front-run to
    the back-run stayed in one liquidity range (otherwise no loss is reported for it).
    """
    front = sandwich.front
    if front.is_v3:
        if market is None:
            return []
        start = market.before(front)
        path = [s for s in pool_swaps if front.position <= s.position <= sandwich.back.position]
        if start is None or not market.trusted_v3_path(start, path):
            return []
        trade = lambda state, swap: market.trade(  # noqa: E731
            front.pool, state, swap.amount_in, swap.zero_for_one
        )
    else:
        start = front.reserves_before
        if start is None:
            return []
        trade = lambda state, swap: apply_swap(state, swap.amount_in, swap.zero_for_one)  # noqa: E731
    between = sorted(
        (
            s
            for s in pool_swaps
            if s.is_simple and front.position < s.position < sandwich.back.position
        ),
        key=lambda s: s.position,
    )
    victim_ids = {(v.tx_hash, v.log_index) for v in sandwich.victims}
    state = start
    results = []
    try:
        for swap in between:
            state, out = trade(state, swap)
            if (swap.tx_hash, swap.log_index) in victim_ids:
                results.append(VictimLoss(swap.tx_hash, swap.token_out, swap.amount_out, out))
    except (InsufficientLiquidity, ValueError):
        return results
    return results
