"""Rate-limited, retrying JSON-RPC client.

Design notes driven by measurements against the live public RPCs (2026-08-29):

* Both public RPCs reject requests with an absent/default User-Agent (HTTP 403).
  curl's default UA passes, python-urllib's does not. We always send one.
* The ``eth_getLogs`` range limit is PER CHAIN, not global:
    - Ink       : hard cap, exactly 10_000 blocks (-32602 on 10_001).
    - Robinhood : no block cap; large ranges fail with -32000 "log query timed
                  out". We start large and halve adaptively.
  ``get_logs_chunked`` handles both by splitting on the per-chain limit and then
  halving any window that still times out.
* There is no archive state. ``eth_call``/``eth_getCode`` at a historical block
  returns "metadata is not found". ``call()`` therefore refuses any block tag
  other than "latest", so we cannot silently build a backtest on data that does
  not exist. This is the constraint that forces the log-derived replay harness.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .constants import CHAINS, GLOBAL_RATE_LIMIT_RPS, HTTP_USER_AGENT


class ArchiveStateError(RuntimeError):
    """Raised when historical state is requested. Public RPCs cannot serve it."""


class RpcError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# Errors that mean "this query asked for too much" rather than "try again".
# Retrying them verbatim is pure latency; the fix is a smaller window.
# MEASURED on the public RPCs:
#   Ink       -32602 "block range greater than 10000 max"   (range cap)
#   Robinhood -32000 "log query timed out"                  (time cap)
#   Robinhood -32000 "logs matched by query exceeds limit of 10000" (RESULT cap)
# The result cap is independent of block range: a topic-filtered 120k-block
# window is fine, an unfiltered one over the same range is not.
_RANGE_REDUCIBLE = (
    "timed out",
    "exceeds limit",
    "exceeds max results",      # Ink: "query exceeds max results 20000"
    "max results",
    "greater than",
    "too many",
    "query returned more than",
    "response size exceeded",
    "retry with the range",     # Ink suggests a narrower range -- honour it
    "limit exceeded",
)

# Ink returns the exact sub-range it can serve, e.g.
#   "query exceeds max results 20000, retry with the range 54011362-54015249"
# Splitting blindly wastes requests when the server has already told us where to
# split, so the hint is parsed and used when present.
_RANGE_HINT = re.compile(r"retry with the range\s+(\d+)\s*-\s*(\d+)", re.I)


def parse_range_hint(msg: str) -> tuple[int, int] | None:
    m = _RANGE_HINT.search(msg or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def is_range_reducible(msg: str) -> bool:
    m = msg.lower()
    return any(p in m for p in _RANGE_REDUCIBLE)


class TokenBucket:
    def __init__(self, rps: float, burst: float | None = None):
        self.rps = rps
        self.capacity = burst if burst is not None else max(1.0, rps)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rps)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = (n - self._tokens) / self.rps
            time.sleep(min(deficit, 0.25))


@dataclass
class RpcStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    timeouts: int = 0
    errors: int = 0
    seconds: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bump(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def as_dict(self) -> dict:
        return {"requests": self.requests, "retries": self.retries,
                "rate_limited": self.rate_limited, "timeouts": self.timeouts,
                "errors": self.errors, "seconds": round(self.seconds, 2)}


class RpcClient:
    def __init__(self, chain: str, rpc_url: str | None = None, rps: float | None = None):
        if chain not in CHAINS:
            raise KeyError(f"unknown chain {chain!r}; known: {list(CHAINS)}")
        self.chain = chain
        cfg = CHAINS[chain]
        self.cfg = cfg
        self.url = rpc_url or cfg["rpc"]
        self.chain_id = cfg["chain_id"]
        self.max_log_range = cfg.get("max_log_range", 10_000)
        self.bucket = TokenBucket(rps or GLOBAL_RATE_LIMIT_RPS)
        self.sem = threading.Semaphore(cfg.get("max_concurrency", 8))
        self.stats = RpcStats()
        self._id = 0
        self._id_lock = threading.Lock()

    def _next_id(self) -> int:
        with self._id_lock:
            self._id += 1
            return self._id

    def raw(self, method: str, params: list, *, timeout: int = 60, retries: int = 5) -> Any:
        """One JSON-RPC call with retry.

        Three distinct failure classes, handled differently:
          * range-reducible (too much data asked for) -> raise at once, caller
            shrinks the window. Retrying identically can never succeed.
          * 429 rate limit -> genuinely transient; retry patiently with a longer
            backoff and its own budget, honouring Retry-After when present.
          * everything else -> bounded exponential backoff.
        """
        payload = json.dumps({"jsonrpc": "2.0", "id": self._next_id(),
                              "method": method, "params": params}).encode()
        last: Exception | None = None
        throttle_budget = 6
        attempt = 0
        while attempt < retries:
            self.bucket.take()
            t0 = time.monotonic()
            try:
                with self.sem:
                    req = urllib.request.Request(
                        self.url, data=payload,
                        headers={"content-type": "application/json",
                                 "user-agent": HTTP_USER_AGENT})
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        body = json.load(resp)
                self.stats.bump(requests=1, seconds=time.monotonic() - t0)
                if "error" in body:
                    err = body["error"]
                    raise RpcError(err.get("code", 0), str(err.get("message", err)))
                return body["result"]
            except RpcError as e:
                last = e
                # Range-reducible errors are raised immediately so the caller can
                # halve the window. Retrying the same oversized query never helps.
                if is_range_reducible(e.message):
                    self.stats.bump(timeouts=1)
                    raise
                if e.code == -32602:
                    raise
                # An execution revert is a DETERMINISTIC answer, not a transient
                # failure: the call will revert identically every time. Retrying
                # it five times turned a fast pre-flight into a slow one for no
                # information. Raise immediately so callers see the revert.
                if e.code == 3 or "revert" in e.message.lower():
                    raise
                self.stats.bump(errors=1)
            except urllib.error.HTTPError as e:
                last = e
                self.stats.bump(seconds=time.monotonic() - t0)
                if e.code == 429:
                    self.stats.bump(rate_limited=1, retries=1)
                    if throttle_budget > 0:
                        throttle_budget -= 1
                        try:
                            wait = float(e.headers.get("Retry-After", "") or 0)
                        except (TypeError, ValueError):
                            wait = 0.0
                        wait = wait or min(12.0, 1.2 * (2 ** (6 - throttle_budget)))
                        time.sleep(wait * (0.7 + random.random() * 0.6))
                        continue          # does not consume a normal attempt
                elif e.code == 400:
                    try:
                        detail = json.loads(e.read().decode("utf8", "replace"))
                        err = detail.get("error", {})
                        raise RpcError(err.get("code", -32602), str(err.get("message", detail)))
                    except (ValueError, AttributeError):
                        pass
                    self.stats.bump(errors=1)
                else:
                    self.stats.bump(errors=1)
            except Exception as e:
                last = e
                self.stats.bump(errors=1, seconds=time.monotonic() - t0)
                # DNS resolution failures ("nodename nor servname provided") and
                # connection resets are transient and clear on their own within
                # seconds. Back off harder than for a generic error rather than
                # burning the retry budget in under a second.
                if isinstance(e, (urllib.error.URLError, OSError)) and \
                        any(k in str(e).lower() for k in
                            ("nodename", "temporary failure", "name resolution",
                             "connection reset", "timed out")):
                    time.sleep(min(10.0, 1.5 * (attempt + 1)) * (0.7 + random.random() * 0.6))
            attempt += 1
            if attempt < retries:
                self.stats.bump(retries=1)
                time.sleep(min(8.0, (2 ** attempt) * 0.4) * (0.6 + random.random() * 0.8))
        raise RuntimeError(f"{self.chain}: {method} failed after {retries} attempts: {last}")

    def block_number(self) -> int:
        return int(self.raw("eth_blockNumber", []), 16)

    def get_chain_id(self) -> int:
        return int(self.raw("eth_chainId", []), 16)

    def get_code(self, address: str, block: str = "latest") -> bytes:
        if block != "latest":
            raise ArchiveStateError(
                "public RPCs have no archive state; eth_getCode at a historical "
                "block returns 'metadata is not found'. Derive history from logs.")
        return bytes.fromhex(self.raw("eth_getCode", [address, block])[2:])

    def call(self, to: str, data: str, block: str = "latest",
             *, allow_historical: bool = False) -> str:
        if block != "latest" and not allow_historical:
            raise ArchiveStateError(
                f"eth_call at block={block!r} is not served (no archive state). "
                "This is the constraint that forces the log-derived replay harness.")
        return self.raw("eth_call", [{"to": to, "data": data}, block])

    # Revert-ish JSON-RPC signatures. Anything NOT matching these is a transport
    # or rate-limit failure and must NOT be reported as "the contract has no
    # such value" -- see try_call.
    _REVERT_HINTS = ("execution reverted", "revert", "invalid opcode",
                     "out of gas", "call exception", "no data")

    def try_call(self, to: str, data: str) -> str | None:
        """eth_call returning None ONLY when the contract genuinely has no value.

        This distinction matters more than it looks. The first version swallowed
        every exception into None, so under concurrent load a rate-limited read
        was indistinguishable from "this collection has no metadata" -- and 181
        of 201 collections were recorded as metadata-less purely because the
        static-read pass was hammering the RPC. A transient infrastructure
        failure had turned into a fabricated feature value.

        Now a revert returns None (a real absence) and a transport failure
        raises, so the caller must decide what to do rather than silently
        inheriting a wrong feature.
        """
        try:
            r = self.call(to, data)
            return r if r and r != "0x" else None
        except RpcError as e:
            if any(h in e.message.lower() for h in self._REVERT_HINTS):
                return None
            raise
        except ArchiveStateError:
            raise
        except RuntimeError:
            raise                    # retries exhausted: transport, not absence

    def get_block(self, number: int, full: bool = False) -> dict:
        return self.raw("eth_getBlockByNumber", [hex(number), full])

    def get_block_timestamp(self, number: int) -> int:
        return int(self.get_block(number)["timestamp"], 16)

    def get_receipt(self, tx_hash: str) -> dict:
        return self.raw("eth_getTransactionReceipt", [tx_hash])

    def get_transaction(self, tx_hash: str) -> dict:
        return self.raw("eth_getTransactionByHash", [tx_hash])

    def get_logs_chunked(self, address: str | Sequence[str] | None, topics: list | None,
                         from_block: int, to_block: int, *, progress: bool = False,
                         workers: int | None = None, label: str = "") -> list[dict]:
        windows: list[tuple[int, int]] = []
        cur, step = from_block, self.max_log_range
        while cur <= to_block:
            end = min(cur + step - 1, to_block)
            windows.append((cur, end))
            cur = end + 1

        out: list[dict] = []
        lock = threading.Lock()
        done = [0]

        def fetch(lo: int, hi: int, depth: int = 0) -> list[dict]:
            flt: dict[str, Any] = {"fromBlock": hex(lo), "toBlock": hex(hi)}
            if address is not None:
                flt["address"] = address
            if topics:
                flt["topics"] = topics
            try:
                return self.raw("eth_getLogs", [flt], timeout=90, retries=4)
            except RpcError as e:
                # NOTE: RpcError subclasses RuntimeError, so this handler MUST
                # come first -- otherwise the RuntimeError clause below catches
                # structured RPC errors too and the range-reducing logic never
                # runs. That ordering bug cost two failed Ink builds.
                if is_range_reducible(e.message) and hi > lo and depth < 40:
                    hint = parse_range_hint(e.message)
                    if hint and lo <= hint[0] <= hint[1] < hi:
                        # The node told us exactly what it can serve. Use it
                        # rather than splitting blindly.
                        return (fetch(hint[0], hint[1], depth + 1)
                                + fetch(hint[1] + 1, hi, depth + 1))
                    mid = (lo + hi) // 2
                    return fetch(lo, mid, depth + 1) + fetch(mid + 1, hi, depth + 1)
                raise
            except RuntimeError as e:
                # Retries exhausted on a transport-level failure. Ink's public RPC
                # intermittently answers a large getLogs with a bare HTTP 500
                # instead of a structured error, so an oversized window and a sick
                # node look identical from here. Halving is the safe response to
                # both: it either fixes the size problem or costs one small extra
                # request before failing honestly.
                if hi > lo and depth < 40:
                    mid = (lo + hi) // 2
                    return fetch(lo, mid, depth + 1) + fetch(mid + 1, hi, depth + 1)
                raise

        def task(w: tuple[int, int]) -> None:
            logs = fetch(*w)
            with lock:
                out.extend(logs)
                done[0] += 1
                if progress and (done[0] % 10 == 0 or done[0] == len(windows)):
                    print(f"    [{self.chain}{'/' + label if label else ''}] "
                          f"{done[0]}/{len(windows)} windows, {len(out)} logs", flush=True)

        nw = workers or self.cfg.get("max_concurrency", 8)
        if len(windows) == 1:
            task(windows[0])
        else:
            with ThreadPoolExecutor(max_workers=nw) as ex:
                list(ex.map(task, windows))
        out.sort(key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))
        return out

    def batch_block_timestamps(self, numbers: Iterable[int]) -> dict[int, int]:
        uniq = sorted(set(numbers))
        res: dict[int, int] = {}
        lock = threading.Lock()

        def one(n: int) -> None:
            ts = self.get_block_timestamp(n)
            with lock:
                res[n] = ts

        with ThreadPoolExecutor(max_workers=self.cfg.get("max_concurrency", 8)) as ex:
            list(ex.map(one, uniq))
        return res


def client(chain: str, **kw) -> RpcClient:
    return RpcClient(chain, **kw)
