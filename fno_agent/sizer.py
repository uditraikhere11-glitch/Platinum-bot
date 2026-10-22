"""Lot sizing from a fixed capital budget, and splitting lots across targets."""

import math
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class SizeResult:
    lots: int
    qty: int
    cost_per_lot: float


def size_position(budget: float, lot_size: int, entry_price: float, max_lots: int = 0) -> SizeResult:
    """Whole lots affordable at `entry_price` (use the top of the entry range). 0 lots = skip."""
    cost_per_lot = lot_size * entry_price
    if budget <= 0 or cost_per_lot <= 0:
        return SizeResult(0, 0, cost_per_lot)
    lots = math.floor(budget / cost_per_lot)
    if max_lots > 0:
        lots = min(lots, max_lots)
    return SizeResult(lots, lots * lot_size, cost_per_lot)


def split_tranches(lots: int, targets: List[float]) -> List[Tuple[int, float]]:
    """
    Split lots across targets as [(lots, target), ...].

    Uses as many targets as there are lots (earliest first); any remainder goes to
    the last (furthest) target so runners ride the bigger move.
    e.g. 3 lots, [500, 1000] -> [(1, 500), (2, 1000)];  1 lot, [500, 1000] -> [(1, 500)]
    """
    if lots <= 0 or not targets:
        return []
    targets = sorted(targets)
    n = min(len(targets), lots)
    base, rem = divmod(lots, n)
    tranches = [(base, t) for t in targets[:n]]
    tranches[-1] = (base + rem, tranches[-1][1])
    return tranches
