"""Participation-aware surplus-sharing rule (float, vectorised). See fixedpoint.py for the integer twin.

Notation follows the paper: S gross surplus, C cost, R reservation, K = C + R required margin,
M = S - K participation margin, lam target share, gamma retained margin, delta safety buffer.
"""
from __future__ import annotations

import numpy as np

TIE_RTOL = 1e-9  # best-response tie tolerance (DECISIONS D13)


def gross_surplus(pi, delta0):
    """S(q) = max(pi . Delta0(q), 0). pi and delta0 are arrays with the last axis = tokens."""
    return np.maximum(np.sum(np.asarray(pi) * np.asarray(delta0), axis=-1), 0.0)


def required_margin(C, R):
    return np.asarray(C) + np.asarray(R)


def participation_margin(S, C, R):
    return np.asarray(S) - required_margin(C, R)


def baseline_feasible(S, C, R):
    """S - C >= R."""
    return (np.asarray(S) - np.asarray(C)) >= np.asarray(R)


def transfer_ideal(S, K, lam, gamma):
    """r_{lam,gamma}(q) = min(lam*S, (1-gamma)*max(M,0)), M = S - K."""
    S = np.asarray(S, dtype=float)
    return np.minimum(lam * S, (1.0 - gamma) * np.maximum(S - K, 0.0))


def transfer_est(S_hat, K_hat, lam, gamma, delta):
    """Estimated transfer with buffer: min(lam*S+, (1-gamma)*max(S+ - K_hat - delta, 0))."""
    Sp = np.maximum(np.asarray(S_hat, dtype=float), 0.0)
    return np.minimum(lam * Sp, (1.0 - gamma) * np.maximum(Sp - K_hat - delta, 0.0))


def transfer_unconstrained(S, lam):
    return lam * np.maximum(np.asarray(S, dtype=float), 0.0)


def transfer_static(notional, fee_rate):
    """Static-fee benchmark: fee_rate * arbitrage notional."""
    return fee_rate * np.asarray(notional, dtype=float)


def arb_payoff(S, C, r):
    """Pi(q) = S - C - r."""
    return np.asarray(S) - np.asarray(C) - np.asarray(r)


def participation_feasible(S, C, R, r, tol=0.0):
    return arb_payoff(S, C, r) >= np.asarray(R) - tol


def recapture_rate(r, S):
    S = np.asarray(S, dtype=float)
    return np.where(S > 0, np.asarray(r) / np.where(S > 0, S, 1.0), np.nan)


def cap_violation(r, S, K, gamma, tol=1e-12):
    """r > (1-gamma)*max(M,0) under true quantities."""
    cap = (1.0 - gamma) * np.maximum(np.asarray(S) - K, 0.0)
    return np.asarray(r) > cap + tol * np.maximum(1.0, np.abs(cap))


def F_scalar(a, K_tx, lam, gamma, delta):
    """Scalar transfer function used by the cumulative accounting (paper Sec. 3.5)."""
    a = np.asarray(a, dtype=float)
    return np.minimum(lam * a, (1.0 - gamma) * np.maximum(a - K_tx - delta, 0.0))


def watermark_charges(prefix_surplus, K_tx, lam, gamma, delta):
    """Marginal charges r_j = W_j - W_{j-1}, W_j = max(W_{j-1}, F(A_j)), W_0 = 0."""
    A = np.maximum(np.asarray(prefix_surplus, dtype=float), 0.0)
    T = F_scalar(A, K_tx, lam, gamma, delta)
    W = np.maximum.accumulate(np.maximum(T, 0.0))
    return np.diff(np.concatenate([[0.0], W]))


def independent_charges(fragment_surplus, K_tx, lam, gamma, delta):
    """Naive per-callback rule applied to each fragment on its own (paper Table 1 baseline)."""
    return F_scalar(np.asarray(fragment_surplus, dtype=float), K_tx, lam, gamma, delta)


def argmax_smallest(payoff, rtol=TIE_RTOL):
    """Index of the maximum payoff along the last axis; ties resolved to the smallest index
    (payoff must be ordered by increasing correction size)."""
    payoff = np.asarray(payoff, dtype=float)
    best = np.nanmax(payoff, axis=-1, keepdims=True)
    tol = rtol * np.maximum(1.0, np.abs(best))
    return np.argmax(payoff >= best - tol, axis=-1)
