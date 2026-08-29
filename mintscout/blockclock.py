"""Timestamp <-> block-number conversion by calibrated interpolation.

Why not just read every block's timestamp: a 6-day window on Robinhood is ~5.2M
blocks. Fetching timestamps for even the blocks that carry logs would be tens of
thousands of eth_getBlockByNumber calls.

Instead we anchor on a handful of real blocks and interpolate. `calibrate()`
reports the measured worst-case error against held-out real blocks so the
approximation is quantified in the README rather than assumed.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass


@dataclass
class Anchor:
    block: int
    ts: int


class BlockClock:
    def __init__(self, rpc, n_anchors: int = 24):
        self.rpc = rpc
        self.anchors: list[Anchor] = []
        self.max_error_s: float | None = None
        self._build(n_anchors)

    def _build(self, n: int) -> None:
        tip = self.rpc.block_number()
        bt = self.rpc.cfg["block_time"]
        span = int((14 * 86400) / bt)          # cover ~14 days of history
        lo = max(1, tip - span)
        pts = [lo + round(i * (tip - lo) / (n - 1)) for i in range(n)]
        ts = self.rpc.batch_block_timestamps(pts)
        self.anchors = sorted((Anchor(b, t) for b, t in ts.items()), key=lambda a: a.block)
        self._blocks = [a.block for a in self.anchors]
        self._ts = [a.ts for a in self.anchors]

    def calibrate(self, samples: int = 8) -> float:
        """Measure interpolation error against real blocks not used as anchors."""
        lo, hi = self._blocks[0], self._blocks[-1]
        pts = [lo + round(i * (hi - lo) / (samples + 1)) + 7919 for i in range(1, samples + 1)]
        pts = [p for p in pts if lo < p < hi]
        real = self.rpc.batch_block_timestamps(pts)
        errs = [abs(self.ts_of(b) - t) for b, t in real.items()]
        self.max_error_s = max(errs) if errs else 0.0
        return self.max_error_s

    def ts_of(self, block: int) -> float:
        i = bisect.bisect_left(self._blocks, block)
        if i <= 0:
            return self._ts[0] + (block - self._blocks[0]) * self.rpc.cfg["block_time"]
        if i >= len(self._blocks):
            return self._ts[-1] + (block - self._blocks[-1]) * self.rpc.cfg["block_time"]
        b0, b1 = self._blocks[i - 1], self._blocks[i]
        t0, t1 = self._ts[i - 1], self._ts[i]
        if b1 == b0:
            return t0
        return t0 + (block - b0) * (t1 - t0) / (b1 - b0)

    def block_of(self, ts: float) -> int:
        i = bisect.bisect_left(self._ts, ts)
        if i <= 0:
            return max(1, int(self._blocks[0] - (self._ts[0] - ts) / self.rpc.cfg["block_time"]))
        if i >= len(self._ts):
            return int(self._blocks[-1] + (ts - self._ts[-1]) / self.rpc.cfg["block_time"])
        t0, t1 = self._ts[i - 1], self._ts[i]
        b0, b1 = self._blocks[i - 1], self._blocks[i]
        if t1 == t0:
            return b0
        return int(b0 + (ts - t0) * (b1 - b0) / (t1 - t0))
