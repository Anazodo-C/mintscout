"""Verdict queue keyed on startTime, with cross-collection batch grouping.

The governing principle of the whole system lives here: because `startTime` is
published in `PublicDropUpdated` *before* the mint opens, triage runs minutes to
hours early and writes a cached verdict. The executor at `startTime` is a dumb,
fast loop that reads that verdict in microseconds. **No language model is ever in
the latency path.**

Batching scales with CONCURRENCY, not quantity: per-wallet caps make it
impossible to batch 20 of one collection (30% of Robinhood free drops are cap=1),
but many free drops are open simultaneously, so batching across collections is
both possible and the only form that helps.
"""
from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field


@dataclass(order=True)
class ScheduledDrop:
    start_time: int
    collection: str = field(compare=False)
    chain: str = field(compare=False)
    end_time: int = field(compare=False, default=0)
    cap: int = field(compare=False, default=0)
    verdict: str = field(compare=False, default="SKIP")
    score: int = field(compare=False, default=0)
    max_supply: int | None = field(compare=False, default=None)


class Scheduler:
    def __init__(self, budget_wei: int = 0, max_wallets: int = 20):
        self._q: list[ScheduledDrop] = []
        self.budget_wei = budget_wei
        self.max_wallets = max_wallets

    def submit(self, d: ScheduledDrop) -> bool:
        """Only MINT verdicts are queued. WATCH is re-evaluated after open."""
        if d.verdict != "MINT":
            return False
        heapq.heappush(self._q, d)
        return True

    def pending(self) -> list[ScheduledDrop]:
        return sorted(self._q)

    def due(self, now: int | None = None, lookahead_s: int = 0) -> list[ScheduledDrop]:
        now = now if now is not None else int(time.time())
        return [d for d in self._q if d.start_time <= now + lookahead_s <= d.end_time]

    def group_batches(self, now: int | None = None, window_s: int = 300,
                      max_per_batch: int = 10) -> list[list[ScheduledDrop]]:
        """Group drops whose mint windows overlap into one per-wallet batch.

        Overlap, not proximity: two drops belong in the same transaction only if
        both are actually open at the same moment, otherwise one of them reverts
        and takes the batch with it.
        """
        now = now if now is not None else int(time.time())
        live = sorted((d for d in self._q if d.end_time >= now), key=lambda d: d.start_time)
        batches: list[list[ScheduledDrop]] = []
        cur: list[ScheduledDrop] = []
        for d in live:
            if not cur:
                cur = [d]
                continue
            open_from = max(x.start_time for x in cur + [d])
            open_to = min(x.end_time for x in cur + [d])
            if open_from <= open_to and len(cur) < max_per_batch:
                cur.append(d)
            else:
                batches.append(cur)
                cur = [d]
        if cur:
            batches.append(cur)
        return batches
