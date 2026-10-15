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


def victim_losses(sandwich: Sandwich, pool_swaps: list[Swap]) -> list[VictimLoss]:
    """Loss of each victim in `sandwich`. `pool_swaps` = all swaps in that pool and block."""
    start = sandwich.front.reserves_before
    if start is None:
        return []
    between = sorted(
        (
            s
            for s in pool_swaps
            if s.is_simple and sandwich.front.position < s.position < sandwich.back.position
        ),
        key=lambda s: s.position,
    )
    victim_ids = {(v.tx_hash, v.log_index) for v in sandwich.victims}
    reserves = start
    results = []
    try:
        for swap in between:
            reserves, out = apply_swap(reserves, swap.amount_in, swap.zero_for_one)
            if (swap.tx_hash, swap.log_index) in victim_ids:
                results.append(VictimLoss(swap.tx_hash, swap.token_out, swap.amount_out, out))
    except InsufficientLiquidity:
        return results
    return results
