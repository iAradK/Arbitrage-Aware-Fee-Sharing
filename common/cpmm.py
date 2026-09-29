"""Constant-product model with fee (v3-style: fee taken from the input leg, the net amount moves the price).

State is (x, y): virtual reserves of token0 and token1. Price p = y/x is token1 per token0.
Fee-on-input with the fee kept outside the reserves makes the model path independent: splitting a
trade into fragments reaches the same final state and the same summed balance deltas.
Trader balance deltas use the paper's convention (positive = tokens received).
"""
from __future__ import annotations

import numpy as np


def price(x, y):
    return np.asarray(y) / np.asarray(x)


def direction_toward(x, y, pi):
    """+1: buy token0 with token1 (pool price below reference); -1: sell token0; 0: equal."""
    return np.sign(np.asarray(pi) - price(x, y)).astype(int)


def full_correction_net(x, y, fee, pi):
    """Net input (after fee) that takes the pool to the no-arbitrage boundary.

    Buy token0 (pi*(1-f) > p): final price p' = pi*(1-f), net token1 in n = sqrt(k*pi*(1-f)) - y.
    Sell token0 (pi/(1-f) < p): final price p' = pi/(1-f), net token0 in n = sqrt(k*(1-f)/pi) - x.
    Returns (direction, n); n = 0 when the reference lies inside the fee band.
    """
    x, y, pi = np.broadcast_arrays(*(np.asarray(v, dtype=float) for v in (x, y, pi)))
    k = x * y
    p = y / x
    up = pi * (1.0 - fee) > p           # buy token0: marginal cost p'/(1-f) <= pi  ->  p' = pi*(1-f)
    down = pi / (1.0 - fee) < p         # sell token0: marginal proceeds p'(1-f) >= pi -> p' = pi/(1-f)
    n_up = np.sqrt(k * pi * (1.0 - fee)) - y
    n_down = np.sqrt(k * (1.0 - fee) / pi) - x
    n = np.where(up, n_up, np.where(down, n_down, 0.0))
    d = np.where(up, 1, np.where(down, -1, 0))
    return d, np.maximum(n, 0.0)


def trade(x, y, fee, direction, n_net):
    """Execute a net input n_net in `direction` (+1 pays token1, receives token0; -1 the reverse).

    Returns trader deltas (d0, d1), gross input, new reserves and the trade notional in token1."""
    x, y, n, d = np.broadcast_arrays(*(np.asarray(v, dtype=float) for v in (x, y, n_net, direction)))
    k = x * y
    buy, sell = d > 0, d < 0
    y_b = y + n
    x_s = x + n
    x_new = np.where(buy, k / y_b, np.where(sell, x_s, x))
    y_new = np.where(buy, y_b, np.where(sell, k / x_s, y))
    gross = np.where(d == 0, 0.0, n / (1.0 - fee))
    d0 = np.where(buy, x - x_new, np.where(sell, -gross, 0.0))
    d1 = np.where(buy, -gross, np.where(sell, y - y_new, 0.0))
    notional = np.where(buy, gross, np.where(sell, y - y_new, 0.0))
    return {"d0": d0, "d1": d1, "gross_in": gross, "x": x_new, "y": y_new, "notional": notional}


def surplus_of(x, y, fee, direction, n_net, pi):
    """Gross surplus in token1 of a trade valued at token1-per-token0 reference pi."""
    t = trade(x, y, fee, direction, n_net)
    return np.maximum(pi * t["d0"] + t["d1"], 0.0), t


def smallest_net_for_surplus(x, y, fee, direction, n_max, pi, target, iters=60):
    """Smallest net input n in [0, n_max] with S(n) >= target (S is increasing and concave on [0, n*]).

    Returns n_max where target is unreachable. Vectorised bisection."""
    lo = np.zeros(np.broadcast(x, y, pi, target).shape)
    hi = np.broadcast_to(np.asarray(n_max, dtype=float), lo.shape).copy()
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        s, _ = surplus_of(x, y, fee, direction, mid, pi)
        ok = s >= target
        hi = np.where(ok, mid, hi)
        lo = np.where(ok, lo, mid)
    return hi


def price_impact_log(x, y, fee, direction, n_net):
    """log(p_post / p_pre) for a net input."""
    t = trade(x, y, fee, direction, n_net)
    return np.log(t["y"] / t["x"]) - np.log(np.asarray(y) / np.asarray(x))
