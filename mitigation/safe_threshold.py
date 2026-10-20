"""The most a sandwich can make from a trade, and the slippage below which it is not worth it.

Setting: a Uniswap V2 pool with reserves (r_in, r_out) before the block; a victim sells
`victim_in` of the input token with tolerance s, so its router reverts unless it receives at
least (1 - s) x the expected output. An attacker front-runs by selling `a` of the same token,
lets the victim trade, then sells back everything it bought. The victim must not revert, so
`a` is capped; the attacker's profit, in the input token, is

    profit(a) = back-run output - a.

`max_sandwich_profit` searches `a` up to that cap. `safe_tolerance` is the largest s at
which the best profit does not exceed `cost` (the attacker's gas, in the input token): a
victim at or below it is not worth sandwiching. Using only gas as the cost (no builder tip)
makes the threshold conservative, i.e. lower than the true break-even.
"""

from profit.uniswap_v2 import InsufficientLiquidity, get_amount_out


def _profit(a: int, victim_in: int, r_in: int, r_out: int, min_out: int) -> int | None:
    """Attacker profit for front-run size `a`, or None if the victim would revert."""
    try:
        bought = get_amount_out(a, r_in, r_out) if a > 0 else 0
        r_in1, r_out1 = r_in + a, r_out - bought
        victim_out = get_amount_out(victim_in, r_in1, r_out1)
        if victim_out < min_out:
            return None
        r_in2, r_out2 = r_in1 + victim_in, r_out1 - victim_out
        back = get_amount_out(bought, r_out2, r_in2) if bought > 0 else 0
    except (InsufficientLiquidity, ValueError):
        return None
    return back - a


def max_sandwich_profit(victim_in: int, r_in: int, r_out: int, tolerance: float) -> int:
    """Best attacker profit (input-token units) against a victim with this tolerance."""
    expected = get_amount_out(victim_in, r_in, r_out)
    min_out = int(expected * (1 - tolerance))
    if _profit(1, victim_in, r_in, r_out, min_out) is None:
        return 0
    # Largest a the victim's limit allows (the constraint is monotone in a).
    lo, hi = 1, 1
    while _profit(hi, victim_in, r_in, r_out, min_out) is not None and hi < r_in * 1000:
        lo, hi = hi, hi * 2
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _profit(mid, victim_in, r_in, r_out, min_out) is None:
            hi = mid
        else:
            lo = mid
    # Profit is concave in a on V2; check an even 64-point grid up to the cap.
    best = 0
    for k in range(64):
        a = lo * (k + 1) // 64
        p = _profit(a, victim_in, r_in, r_out, min_out)
        if p is not None and p > best:
            best = p
    return best


def safe_tolerance(victim_in: int, r_in: int, r_out: int, cost: int, upper: float = 0.5) -> float:
    """Largest tolerance (up to `upper`) at which no sandwich beats `cost`."""
    if max_sandwich_profit(victim_in, r_in, r_out, upper) <= cost:
        return upper
    lo, hi = 0.0, upper
    for _ in range(40):
        mid = (lo + hi) / 2
        if max_sandwich_profit(victim_in, r_in, r_out, mid) <= cost:
            lo = mid
        else:
            hi = mid
    return lo
