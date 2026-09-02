"""SeaDrop log tailer -> DropCandidate, with a persisted per-chain cursor.

Deterministic. No LLM in this path -- the watcher's only job is to notice a drop
config early enough that triage has minutes of slack before `startTime`.

Config revisions are treated as UPSERT keyed on (chain, collection), never
INSERT: drop configs are mutable and are edited mid-flight. The full revision
history is retained, because repeated price flips are themselves a risk signal.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass

from . import constants as C
from .decode import decode_drop_uri_updated, decode_public_drop_updated
from .rpc import RpcClient, client

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The cursor MUST live on the persistent volume. Kept under the repo's data/
# directory it sat inside the container image, so every redeploy forgot where
# the tail had reached and re-scanned the whole LOOKBACK_MINUTES window -- 60+
# candidates and several minutes of work to rediscover drops already judged.
STATE = pathlib.Path(os.environ.get("MINTSCOUT_STATE_DIR",
                                    str(ROOT / "data"))) / "watcher_state.json"


@dataclass
class DropCandidate:
    chain: str
    collection: str
    mint_price: int
    start_time: int
    end_time: int
    max_per_wallet: int
    fee_bps: int
    restrict_fee_recipients: bool
    drop_uri: str | None = None
    n_revisions: int = 1
    first_seen_ts: int = 0

    @property
    def seconds_until_open(self) -> int:
        return self.start_time - int(time.time())

    def as_dict(self) -> dict:
        d = asdict(self)
        d["seconds_until_open"] = self.seconds_until_open
        return d


class Watcher:
    def __init__(self, chain: str, dust: int = C.DUST_THRESHOLD_WEI,
                 rpc: RpcClient | None = None, state_path=STATE):
        self.chain = chain
        self.dust = dust
        self.rpc = rpc or client(chain)
        self.state_path = pathlib.Path(state_path)
        self.configs: dict[str, list] = {}
        self.uris: dict[str, str] = {}

    # ------------------------------------------------------------ cursor
    def _state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except ValueError:
                pass
        return {}

    def cursor(self) -> int | None:
        return self._state().get(self.chain, {}).get("cursor")

    def save_cursor(self, block: int) -> None:
        s = self._state()
        s.setdefault(self.chain, {})["cursor"] = block
        s[self.chain]["updated_at"] = int(time.time())
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(s, indent=2))

    # ------------------------------------------------------------- poll
    def poll(self, from_block: int | None = None, to_block: int | None = None,
             lookback_minutes: float = 30.0, save: bool = True) -> list[DropCandidate]:
        tip = to_block or self.rpc.block_number()
        if from_block is None:
            from_block = self.cursor() or (
                tip - int(lookback_minutes * 60 / self.rpc.cfg["block_time"]))
        from_block = max(1, min(from_block, tip))

        logs = self.rpc.get_logs_chunked(
            C.SEADROP, [[C.TOPIC_PUBLIC_DROP_UPDATED, C.TOPIC_DROP_URI_UPDATED]],
            from_block, tip)

        for l in logs:
            t0 = l["topics"][0]
            if t0 == C.TOPIC_DROP_URI_UPDATED:
                col, uri = decode_drop_uri_updated(l)
                self.uris[col] = uri
            elif t0 == C.TOPIC_PUBLIC_DROP_UPDATED:
                p = decode_public_drop_updated(l, self.chain)
                # UPSERT, not INSERT -- keyed on (chain, collection)
                self.configs.setdefault(p.collection, []).append(p)

        now = int(time.time())
        out: list[DropCandidate] = []
        for col, revs in self.configs.items():
            revs.sort(key=lambda p: (p.block_number, p.log_index))
            latest = revs[-1]
            if latest.mint_price > self.dust:
                continue                      # includes drops repriced OUT of free
            if latest.end_time <= now:
                continue
            out.append(DropCandidate(
                chain=self.chain, collection=col, mint_price=latest.mint_price,
                start_time=latest.start_time, end_time=latest.end_time,
                max_per_wallet=latest.max_per_wallet, fee_bps=latest.fee_bps,
                restrict_fee_recipients=latest.restrict_fee_recipients,
                drop_uri=self.uris.get(col), n_revisions=len(revs),
                first_seen_ts=now))
        if save:
            self.save_cursor(tip)
        out.sort(key=lambda c: c.start_time)
        return out
