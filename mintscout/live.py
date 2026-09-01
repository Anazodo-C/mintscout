"""Live runner: watch -> pre-filter -> triage -> execute.

Logging is a first-class feature here, not an afterthought. Every drop the agent
considers prints its evidence, its verdict and the reasoning behind it, so the
operator can read the Railway logs and see exactly why each decision was made and
where the rubric needs adjusting.

## Why there is a deterministic pre-filter in front of the LLM

Robinhood alone configures ~2,700 drops/day, ~53% of them free. Sending every one
to an LLM would cost roughly $17/day and add nothing: the deterministic rubric
already rejects the obvious junk (no resolvable drop config, 45-byte minimal
proxy, placeholder name) for free, and on held-out data it had a BETTER
precision/recall balance than the LLM arm (F1 0.325 vs 0.177).

So the cheap filter runs first and only survivors reach the model. That cuts LLM
volume by ~90% (~$1.40/day) and puts the model where it adds value: judging
plausible candidates, not discarding spam.
"""
from __future__ import annotations

import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

from . import constants as C
from .budget import Denied, SpendGuard, is_live, volume_check
from .enrich import ToolRecorder, build_dossier
from .agent.social_gate import (auto_flag_enabled, evaluate_social,
                                format_social_line, social_enabled)
from .eval.baselines import deterministic_rubric
from .eval.replay import ReplayContext
from .executor import (DEFAULT_MINT_GAS, build_mint_tx, decode_revert,
                       expected_cost_wei, read_public_drop, send_mint,
                       wait_for_receipt)
from .rpc import client
from .watcher import Watcher

# ------------------------------------------------------------------ logging
_START = time.time()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str = "", *, indent: int = 0) -> None:
    print(("  " * indent) + msg, flush=True)


class Block:
    """Buffer a candidate's lines and emit them as ONE print.

    Railway orders log lines by timestamp, so a block written as 15 separate
    prints can interleave with another block written in the same millisecond --
    which is exactly what happened in production, producing candidate blocks
    with each other's NAME and DECISION lines spliced in. One write per
    candidate keeps each block contiguous, and it matters far more once 20
    wallets are reporting concurrently.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def add(self, msg: str = "", *, indent: int = 0) -> "Block":
        self._lines.append(("  " * indent) + msg)
        return self

    def flush(self) -> None:
        if self._lines:
            print("\n".join(self._lines), flush=True)
            self._lines.clear()


def rule(char: str = "─", n: int = 78) -> None:
    print(char * n, flush=True)


def banner(title: str) -> None:
    rule("═")
    log(f"  {title}")
    rule("═")


def fmt_eth(wei: int | None) -> str:
    if wei is None:
        return "?"
    return "FREE" if wei == 0 else f"{wei / 1e18:.6f} ETH"


def fmt_limit(wei: int | None) -> str:
    """Format a LIMIT, where 0 means 'no limit' -- never 'FREE'.

    fmt_eth renders 0 as "FREE", which is right for a mint price and actively
    dangerous for a cap: an operator checking the startup banner before going
    live would read an unset per-mint ceiling as a zero-cost one.
    """
    if not wei:
        return "NOT SET (no limit enforced)"
    return f"{wei / 1e18:.6f} ETH"


def fmt_in(seconds: int) -> str:
    if seconds < 0:
        return f"OPEN (started {-seconds // 60}m ago)"
    if seconds < 90:
        return f"in {seconds}s"
    if seconds < 5400:
        return f"in {seconds // 60}m"
    return f"in {seconds / 3600:.1f}h"


class LiveDrop:
    """Adapts a live DropCandidate to the same interface the eval uses.

    The point of this shim is that the decision code running against real money
    is the SAME code the offline evaluation measured. If live used a different
    path, the reported precision would say nothing about production behaviour.
    """

    def __init__(self, cand, static: dict, uris: list[dict], revisions: list[dict]):
        self.chain = cand.chain
        self.collection = cand.collection
        self.cutoff = int(time.time())
        self._cand = cand
        self._static = static
        self._uris = uris
        self._revs = revisions

    @property
    def public_drop(self) -> dict:
        c = self._cand
        return {"mint_price_wei": c.mint_price, "start_time": c.start_time,
                "end_time": c.end_time, "duration_s": max(0, c.end_time - c.start_time),
                "max_per_wallet": c.max_per_wallet, "fee_bps": c.fee_bps,
                "restrict_fee_recipients": c.restrict_fee_recipients,
                "config_block_timestamp": self.cutoff}

    @property
    def static(self) -> dict:
        return dict(self._static)

    def config_revisions(self) -> list[dict]:
        return list(self._revs)

    def drop_uris(self) -> list[dict]:
        return list(self._uris)


def read_static(rpc, col: str) -> dict:
    from .dataset import _read_static
    try:
        return _read_static(rpc, col)
    except Exception as e:
        return {"_read_ok": False, "_read_errors": [f"{type(e).__name__}: {e}"[:120]]}


# ------------------------------------------------------------------- runner
class Runner:
    def __init__(self) -> None:
        self.chains = [c.strip() for c in
                       (os.environ.get("CHAINS") or "robinhood").split(",") if c.strip()]
        self.poll_s = float(os.environ.get("POLL_INTERVAL_S", "60"))
        self.lookback_min = float(os.environ.get("LOOKBACK_MINUTES", "20"))
        self.prefilter_min = int(os.environ.get("PREFILTER_MIN_SCORE", "45"))
        self.use_llm = (os.environ.get("USE_LLM", "true").lower()
                        in ("1", "true", "yes"))
        # What to do when the LLM is unreachable (credits exhausted, outage,
        # rate limit). Two defensible answers, so it is explicit rather than
        # implicit:
        #   "deterministic" -- fall back to the rubric verdict. Defensible
        #       because the rubric actually beat the LLM arm on F1 (0.325 vs
        #       0.177) in the held-out evaluation. This is the default.
        #   "skip"          -- refuse to mint anything the LLM did not approve.
        #       Choose this if you want an LLM outage to halt spending.
        self.on_llm_failure = (os.environ.get("ON_LLM_FAILURE", "deterministic")
                               .strip().lower())
        # Social runs BEFORE the deterministic pre-filter, not after. It has to:
        # the whole point of auto-flag is that a strong social presence can
        # rescue a project the rubric would otherwise reject, and a check that
        # only ran on rubric survivors could never do that. The cost is one
        # OpenSea page fetch per NEW collection (cached forever after), bounded
        # per cycle by SOCIAL_MAX_PER_CYCLE.
        self.social_max_per_cycle = int(os.environ.get("SOCIAL_MAX_PER_CYCLE", "25"))
        self._social_this_cycle = 0
        self.max_lead_s = int(os.environ.get("MAX_LEAD_SECONDS", "7200"))
        self.guard = SpendGuard()
        self.live = is_live()
        self.watchers = {c: Watcher(c) for c in self.chains}
        self.rpcs = {c: self.watchers[c].rpc for c in self.chains}
        self.wallet = None
        self._key = None
        self.fleet: list[dict] = []
        self._last_armed: dict[str, int] = {}
        # Approved drops whose window has not opened yet, keyed "chain:collection".
        # This is the scheduler the architecture describes: judge once, early,
        # then hold a cached verdict and let a dumb fast loop fire at startTime.
        # Without it, widening the lead window means re-enriching every pending
        # drop on every poll -- 40+ dossiers a minute for one answer that does
        # not change.
        self.pending: dict[str, dict] = {}
        self.blocked_reason: str | None = None
        self.seen: set[str] = set()
        self.stats = {"candidates": 0, "prefiltered": 0, "triaged": 0,
                      "mint": 0, "watch": 0, "skip": 0, "executed": 0, "failed": 0}
        self._stop = False
        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)

    def _sig(self, *_):
        log("\n[signal] shutting down after current cycle…")
        self._stop = True

    # ------------------------------------------------------------ wallet
    def load_wallet(self) -> None:
        """Derive the fleet and report which wallets are actually armed.

        `payer == minter` in 98.5% of live SeaDrop mints, so a wallet can only
        mint for itself: every wallet in the fleet must hold its own gas and
        sign its own transaction. A wallet with no balance is dead weight, so
        the banner reports armed-vs-total rather than just listing addresses.
        """
        seed = (os.environ.get("MINT_SEED") or "").strip()
        if not seed:
            log("[wallet] MINT_SEED not set -> observation only, cannot execute")
            return
        from eth_account import Account
        Account.enable_unaudited_hdwallet_features()
        start = int(os.environ.get("WALLET_INDEX", "0"))
        n = max(1, int(os.environ.get("MAX_WALLETS", str(C.MAX_WALLETS_DEFAULT))))
        self.fleet = []
        for i in range(start, start + n):
            a = Account.from_mnemonic(seed, account_path=f"m/44'/60'/0'/0/{i}")
            self.fleet.append({"index": i, "address": a.address,
                               "key": a.key.hex(), "balance": {}})
        # index 0 of the fleet stays the primary for single-wallet code paths
        self.wallet = self.fleet[0]["address"]
        self._key = self.fleet[0]["key"]

        min_bal = self._min_balance_to_mint()
        for ch, rpc in self.rpcs.items():
            armed = 0
            total = 0
            for w in self.fleet:
                try:
                    b = int(rpc.raw("eth_getBalance", [w["address"], "latest"]), 16)
                except Exception:
                    b = 0
                w["balance"][ch] = b
                total += b
                if b >= min_bal:
                    armed += 1
            log(f"[wallet] {ch}: {armed}/{len(self.fleet)} armed  "
                f"(>= {min_bal / 1e18:.6f} ETH each)  "
                f"fleet total {total / 1e18:.6f} ETH")
            for w in self.fleet[:3]:
                log(f"         [{w['index']:>2}] {w['address']}  "
                    f"{w['balance'][ch] / 1e18:.6f} ETH", indent=0)
            if len(self.fleet) > 3:
                log(f"         … {len(self.fleet) - 3} more", indent=0)
            if armed == 0:
                log(f"[wallet] WARNING {ch}: no wallet holds enough to mint. "
                    f"Run:  mintscout fund --chain {ch} --target-eth 0.002 "
                    f"--wallets {len(self.fleet)} --live")

    def refresh_balances(self, chain: str) -> tuple[int, int]:
        """Re-read fleet balances. Returns (armed, total_wei).

        Balances were previously read ONCE at startup, so a fleet funded after
        boot stayed invisible until the next redeploy -- the running process
        reported '1/5 armed' while four wallets sat funded on-chain. Wallets
        also drain as they mint, now that no gas reserve is enforced. Five
        eth_getBalance calls per cycle is a negligible cost for not having to
        redeploy to notice your own money.
        """
        rpc = self.rpcs[chain]
        min_bal = self._min_balance_to_mint()
        armed = total = 0
        for w in self.fleet:
            try:
                b = int(rpc.raw("eth_getBalance", [w["address"], "latest"]), 16)
                w["balance"][chain] = b
                total += b
                if b >= min_bal:
                    armed += 1
            except Exception:
                pass
        return armed, total

    def _min_balance_to_mint(self) -> int:
        """Balance a wallet needs to be considered armed.

        Uses the same expected-cost basis as the spend guard (measured gas x
        safety margin), not the 260k gas ceiling. Using the ceiling here marked
        wallets unarmed that could comfortably afford several mints -- the same
        confusion between a gas LIMIT and a gas PRICE that blocked a live mint.
        """
        from .executor import BUDGET_SAFETY_MARGIN, MEASURED_MINT_GAS
        try:
            rpc = next(iter(self.rpcs.values()))
            gp = int(rpc.raw("eth_gasPrice", []), 16)
        except Exception:
            gp = 1_000_000_000
        return int(MEASURED_MINT_GAS * BUDGET_SAFETY_MARGIN) * gp * 2

    # ----------------------------------------------------------- startup
    def preflight_config(self) -> bool:
        banner("MintScout — live runner")
        log(f"mode            : {'LIVE (real funds)' if self.live else 'DRY RUN (no funds spent)'}")
        log(f"chains          : {', '.join(self.chains)}")
        log(f"poll interval   : {self.poll_s:.0f}s   lookback {self.lookback_min:.0f}m")
        log(f"pre-filter      : deterministic score >= {self.prefilter_min} before LLM")
        if social_enabled():
            from .agent.social_gate import min_followers, min_posts
            log(f"social gate     : >= {min_followers():,} followers and "
                f">= {min_posts()} posts   mode="
                f"{'AUTO-FLAG (sufficient on its own)' if auto_flag_enabled() else 'gate (confirms only)'}")
            log(f"                  max {self.social_max_per_cycle} lookups/cycle")
        else:
            log("social gate     : disabled")
        log(f"LLM triage      : {'on' if self.use_llm else 'OFF (deterministic only)'}"
            + (f"   on failure -> {self.on_llm_failure}" if self.use_llm else ""))
        log(f"max lead time   : {self.max_lead_s}s (ignore drops opening later than this)")
        lim = self.guard.limits
        log("limits          :")
        log(f"  total spend     {fmt_limit(lim.max_total_spend_wei)}", indent=1)
        log(f"  per-mint max    {fmt_limit(lim.max_spend_per_mint_wei)}", indent=1)
        log(f"  max mints       {lim.max_mints_total or 'NOT SET'} total, "
            f"{lim.max_mints_per_hour or 'unlimited'}/hour", indent=1)
        log(f"  max gas price   {lim.max_gas_price_wei / 1e9:.2f} gwei", indent=1)
        log(f"  gas reserve     {fmt_limit(lim.gas_reserve_wei)}"
            + ("  (disabled — wallets may be spent to empty)"
               if not lim.gas_reserve_wei else ""), indent=1)
        for warn in (("MAX_SPEND_PER_MINT_WEI", lim.max_spend_per_mint_wei),
                     ("MAX_MINTS_PER_HOUR", lim.max_mints_per_hour)):
            if not warn[1]:
                log(f"  WARNING {warn[0]} is not set — that limit is NOT enforced",
                    indent=1)
        v = volume_check()
        icon = "OK " if v["persisted"] else ("!! " if not v["writable"] else "?? ")
        log(f"state volume    : {icon}{v['state_dir']}  boots={v['boots']}  "
            f"writable={v['writable']}")
        if not v["writable"]:
            log("                  SPEND STATE CANNOT BE SAVED — caps will not "
                "survive a restart. Mount a Railway volume at this path.")

        # Restart-loop detection. Nothing in this process restarts itself: the
        # poll loop is exception-guarded and only exits on SIGTERM. So if boots
        # is climbing quickly, something OUTSIDE the app is cycling the
        # container, and the operator needs to see that rather than mistake a
        # repeated startup banner for normal behaviour.
        gap = v.get("seconds_since_last_boot")
        if gap is not None:
            if gap < 300:
                log(f"                  WARNING last boot was only {int(gap)}s ago "
                    f"— this looks like a RESTART LOOP, not a normal start.")
                log("                  Likely causes: Railway healthcheck timing "
                    "out, the container being OOM-killed, or a redeploy. Check "
                    "the Railway deploy log for the exit reason.")
            else:
                mins = gap / 60
                log(f"                  previous boot {mins:.0f}m ago — "
                    f"steady, not a restart loop.")
        self.load_wallet()
        if self.live:
            # NOTE: none of these conditions exits the process any more.
            #
            # They used to `return False`, which exited non-zero, which Railway's
            # ON_FAILURE policy restarted, which hit the same condition, which
            # exited again -- an infinite crash loop. And the most likely trigger
            # was the most ordinary event in the system: simply reaching
            # MAX_MINTS_TOTAL. Spending your allowance is a SUCCESS, not a
            # failure, and it must never look like a crash.
            #
            # So a blocked live mode degrades to observation: the process stays
            # up, the logs keep flowing, /status reports why, and nothing spends.
            if not self.fleet:
                log("\n[BLOCKED] LIVE_EXECUTION is on but MINT_SEED is not set.")
                log("          Running in OBSERVATION mode — watching and logging, "
                    "not spending.")
                self.live = False
                self.blocked_reason = "MINT_SEED not set"
            else:
                try:
                    self.guard.check(0, 0)
                    log("\n*** LIVE MODE — this process will spend real funds ***")
                except Denied as e:
                    log(f"\n[BLOCKED] live execution not permitted: {e}")
                    log("          Running in OBSERVATION mode — watching and "
                        "logging, not spending. Raise the cap (or clear "
                        "/data/spend_state.json) and redeploy to resume.")
                    self.live = False
                    self.blocked_reason = str(e)
        rule()
        return True

    # -------------------------------------------------------------- cycle
    def handle(self, chain: str, cand) -> None:
        rpc = self.rpcs[chain]
        key = f"{chain}:{cand.collection}"
        opens_in = cand.start_time - int(time.time())
        if opens_in > self.max_lead_s:
            return
        if key in self.seen:
            return
        self.seen.add(key)
        self.stats["candidates"] += 1

        rule()
        log(f"[{_ts()}] CANDIDATE  {cand.collection}")
        log(f"chain={chain}  price={fmt_eth(cand.mint_price)}  cap={cand.max_per_wallet}"
            f"  opens {fmt_in(opens_in)}  window "
            f"{max(0, cand.end_time - cand.start_time) / 3600:.1f}h", indent=1)

        static = read_static(rpc, cand.collection)
        uris = ([{"uri": cand.drop_uri, "block_timestamp": int(time.time())}]
                if cand.drop_uri else [])
        ctx = LiveDrop(cand, static, uris, [])
        rec = ToolRecorder()
        dossier = build_dossier(ctx, memory=None, allow_network=True, recorder=rec)

        col = dossier["collection"]
        meta = dossier["metadata"]
        log(f"NAME   {col.get('name')!r}  symbol={col.get('symbol')!r}  "
            f"supply={col.get('max_supply')}", indent=1)
        log(f"TOOLS  standard={dossier['standard']['standard']}  "
            f"code_size={col.get('code_size')}  "
            f"metadata={meta.get('shape') or 'none'}"
            + (f" ({meta.get('n_stages')} stages)" if meta.get("n_stages") else ""),
            indent=1)
        if meta.get("stage_names"):
            log(f"       stages: {', '.join(str(s) for s in meta['stage_names'][:4])}",
                indent=1)

        # ---- social gate (runs before the pre-filter so it can rescue)
        social = None
        if social_enabled() and self._social_this_cycle < self.social_max_per_cycle:
            self._social_this_cycle += 1
            try:
                social = evaluate_social(chain, cand.collection)
                log(format_social_line(chain, cand.collection, social), indent=1)
            except Exception as e:
                log(f"SOCIAL  lookup error ({type(e).__name__}) — ignored", indent=1)
                social = None
        elif social_enabled():
            log(f"SOCIAL  skipped (SOCIAL_MAX_PER_CYCLE="
                f"{self.social_max_per_cycle} reached this cycle)", indent=1)

        # ---- stage 1: deterministic pre-filter (free)
        det = deterministic_rubric(dossier)
        log(f"FILTER deterministic score={det['score']} verdict={det['verdict']}", indent=1)
        for r in det.get("reasons", [])[:3]:
            log(f"       + {r}", indent=1)
        for r in det.get("risk_flags", [])[:3]:
            log(f"       ! {r}", indent=1)

        # An auto-flag overrides the pre-filter. Absence of social data never
        # forces a SKIP -- UNRESOLVED and NO_SOCIAL fall through untouched.
        social_mint = bool(social and social.get("flag") == "MINT")
        if social_mint:
            self.stats["social_autoflag"] = self.stats.get("social_autoflag", 0) + 1
            log(f"SOCIAL  AUTO-FLAG MINT — @{social['handle']} "
                f"{social['followers']:,} followers, {social['posts']} posts "
                f"(thresholds {social['thresholds']['followers']}/"
                f"{social['thresholds']['posts']}); bypasses pre-filter "
                f"and cannot be downgraded", indent=1)

        if det["score"] < self.prefilter_min and not social_mint:
            self.stats["prefiltered"] += 1
            self.stats["skip"] += 1
            log(f"DECISION SKIP — below pre-filter threshold "
                f"({det['score']} < {self.prefilter_min}); no LLM call made", indent=1)
            return

        # ---- stage 2: LLM triage (only for survivors)
        verdict, score, reasons, flags = det["verdict"], det["score"], \
            det.get("reasons", []), det.get("risk_flags", [])
        if self.use_llm:
            try:
                from .agent.triage import triage
                t0 = time.perf_counter()
                out = triage(dossier)
                self.stats["triaged"] += 1
                verdict = out["verdict"]; score = out.get("score", 0)
                reasons = out.get("reasons", []); flags = out.get("risk_flags", [])
                log(f"TRIAGE {verdict}  score={score}  "
                    f"({(time.perf_counter() - t0) * 1000:.0f}ms)", indent=1)
                for r in reasons[:4]:
                    log(f"       + {r}", indent=1)
                for r in flags[:3]:
                    log(f"       ! {r}", indent=1)
                for e in (out.get("evidence") or [])[:3]:
                    if isinstance(e, dict):
                        log(f"       evidence: {e.get('field')} = "
                            f"{str(e.get('value'))[:60]} [{e.get('reads_as')}]", indent=1)
            except Exception as e:
                detail = str(e)
                hint = ("  <-- API CREDITS EXHAUSTED"
                        if "credit balance" in detail.lower() else "")
                if self.on_llm_failure == "skip":
                    log(f"TRIAGE UNAVAILABLE ({type(e).__name__}){hint}", indent=1)
                    log("DECISION SKIP — ON_LLM_FAILURE=skip, refusing to mint "
                        "without model approval", indent=1)
                    self.stats["skip"] += 1
                    return
                log(f"TRIAGE UNAVAILABLE ({type(e).__name__}: "
                    f"{detail[:70]}){hint}", indent=1)
                log(f"       falling back to DETERMINISTIC verdict "
                    f"{det['verdict']} (score {det['score']}). Set "
                    f"ON_LLM_FAILURE=skip to halt instead.", indent=1)

        if social_mint and verdict != "MINT":
            # SOCIAL_AUTO_FLAG=true means a passing social check is sufficient.
            log(f"OVERRIDE triage said {verdict}; social auto-flag holds -> MINT "
                f"(set SOCIAL_AUTO_FLAG=false to let the rubric decide)", indent=1)
            verdict = "MINT"

        self.stats[verdict.lower()] = self.stats.get(verdict.lower(), 0) + 1
        if verdict != "MINT":
            log(f"DECISION {verdict} — not minting", indent=1)
            return

        now = int(time.time())
        if cand.start_time > now:
            self.pending[key] = {"chain": chain, "cand": cand,
                                 "queued_at": now, "score": score}
            log(f"DECISION QUEUED — approved, opens {fmt_in(cand.start_time - now)} "
                f"at {datetime.fromtimestamp(cand.start_time, timezone.utc):%H:%M UTC}. "
                f"Verdict is cached; no re-analysis until it fires.", indent=1)
            return

        self.execute(chain, cand, dossier)

    # ------------------------------------------------------------ execute
    def execute(self, chain: str, cand, dossier: dict) -> None:
        """Mint from every armed wallet in the fleet.

        One transaction per wallet, never a batch. `payer == minter` means a
        wallet can only mint for itself, so 20 wallets is 20 transactions and
        EIP-7702 cannot compress that -- it batches across COLLECTIONS within
        one wallet, not across wallets.

        The spend guard is global and is consulted per transaction, so
        MAX_MINTS_TOTAL bounds the whole fleet rather than each wallet. That is
        deliberate: a per-wallet cap of 5 across 20 wallets would silently mean
        100 mints of exposure.
        """
        rpc = self.rpcs[chain]
        cap = max(1, int(cand.max_per_wallet or 1))
        qty = min(cap, int(os.environ.get("MAX_QUANTITY_PER_MINT", "2")))

        # Configs are mutable and get edited mid-flight. Never trust the queued
        # value: re-read immediately before spending.
        try:
            live_cfg = read_public_drop(rpc, cand.collection)
        except Exception as e:
            log(f"DECISION ABORT — getPublicDrop re-read failed "
                f"({type(e).__name__})", indent=1)
            return
        if live_cfg["mint_price"] > C.DUST_THRESHOLD_WEI:
            log(f"DECISION ABORT — repriced since queueing: "
                f"{fmt_eth(live_cfg['mint_price'])} (was free)", indent=1)
            return
        now = int(time.time())
        if not (live_cfg["start_time"] <= now <= live_cfg["end_time"]):
            log(f"DECISION DEFER — window not open "
                f"(opens {fmt_in(live_cfg['start_time'] - now)})", indent=1)
            self.seen.discard(f"{chain}:{cand.collection}")   # revisit next cycle
            return

        if not self.fleet:
            log("DECISION DRY RUN — would mint, but no MINT_SEED configured",
                indent=1)
            return

        # Which wallets can actually participate.
        min_bal = self._min_balance_to_mint()
        # MAX_WALLETS is the single fleet knob: it sizes the fleet AND bounds how
        # many wallets mint one collection. A separate per-drop limit was
        # redundant -- with a fleet of N you would always set both to N -- and two
        # variables that must agree is a configuration trap, not a feature.
        armed = [w for w in self.fleet if w["balance"].get(chain, 0) >= min_bal]
        value_each = live_cfg["mint_price"] * qty

        b = Block()
        b.add(f"EXECUTE fleet={len(armed)}/{len(self.fleet)} armed  "
              f"qty={qty}/wallet (cap {cap})  value={fmt_eth(value_each)} each",
              indent=1)
        if not armed:
            b.add(f"DECISION SKIP — no wallet holds the "
                  f"{min_bal / 1e18:.6f} ETH needed to mint", indent=1)
            b.flush()
            return

        # Pre-flight EACH wallet, not one representative.
        # `from` changes the answer: SeaDrop's per-wallet cap accounting is
        # relative to msg.sender, so a fleet-wide simulation cannot see that
        # wallet 3 already holds one and will revert. Simulating per wallet is N
        # cheap eth_calls and it turns a whole-fleet abort into "skip the two
        # wallets that would fail, mint from the other three".
        ok_wallets, blocked = [], []
        for w in armed:
            try:
                probe = build_mint_tx(rpc, cand.collection, w["address"], qty,
                                      value_each, nonce=0)
                rpc.call(C.SEADROP, probe["data"], from_address=w["address"])
                ok_wallets.append(w)
            except Exception as e:
                why = decode_revert(getattr(e, "data", None)) or str(e)[:70]
                blocked.append((w["index"], why))

        for idx, why in blocked[:5]:
            b.add(f"        [{idx:>2}] pre-flight revert: {why}", indent=1)
        if not ok_wallets:
            reasons = {w for _, w in blocked}
            b.add(f"DECISION ABORT — {cand.collection} all {len(armed)} wallet(s) "
                  f"would revert: {'; '.join(list(reasons)[:2])}", indent=1)
            b.flush()
            # Wasted gas avoided is the whole point of pre-flight; count it.
            self.stats["gas_saved_wei"] = self.stats.get("gas_saved_wei", 0) + \
                len(armed) * DEFAULT_MINT_GAS * probe["maxFeePerGas"]
            return
        if blocked:
            b.add(f"        pre-flight: {len(ok_wallets)}/{len(armed)} wallets "
                  f"can mint; {len(blocked)} would revert and are skipped",
                  indent=1)
            self.stats["gas_saved_wei"] = self.stats.get("gas_saved_wei", 0) + \
                len(blocked) * DEFAULT_MINT_GAS * probe["maxFeePerGas"]
        else:
            b.add(f"        pre-flight simulation: OK for all "
                  f"{len(ok_wallets)} wallet(s)", indent=1)
        armed = ok_wallets

        if not self.live:
            b.add(f"DECISION DRY RUN — would mint from {len(armed)} wallet(s), "
                  f"{qty * len(armed)} token(s) total. Set DRY_RUN=false and "
                  f"LIVE_EXECUTION=true to send.", indent=1)
            b.flush()
            return
        b.flush()

        sent = 0
        for w in armed:
            try:
                nonce = int(rpc.raw("eth_getTransactionCount",
                                    [w["address"], "pending"]), 16)
                tx = build_mint_tx(rpc, cand.collection, w["address"], qty,
                                   value_each, nonce)
                cost = expected_cost_wei(rpc, tx)
                self.guard.check(cost, tx["maxFeePerGas"],
                                 wallet_balance_wei=w["balance"].get(chain))
            except Denied as e:
                # A global cap stopping the fleet is expected, not an error.
                log(f"  [{w['index']:>2}] stopped by spend guard — {e}", indent=1)
                break
            except Exception as e:
                log(f"  [{w['index']:>2}] skipped ({type(e).__name__}: "
                    f"{str(e)[:70]})", indent=1)
                continue

            # Commit BEFORE broadcasting: a crash must over-count, not under.
            self.guard.commit(cost)
            try:
                h = send_mint(rpc, tx, w["key"])
                sent += 1
                log(f"  [{w['index']:>2}] SENT {w['address'][:10]}… tx={h}",
                    indent=1)
                r = wait_for_receipt(rpc, h, timeout_s=45)
                if r is None:
                    log(f"  [{w['index']:>2}] receipt not seen yet "
                        f"(may still land)", indent=1)
                    self.stats["executed"] += 1
                    continue
                ok = int(r.get("status", "0x0"), 16) == 1
                used = int(r.get("gasUsed", "0x0"), 16)
                price = int(r.get("effectiveGasPrice", "0x0"), 16)
                actual = value_each + used * price
                log(f"  [{w['index']:>2}] {'CONFIRMED' if ok else 'REVERTED'}  "
                    f"gas={used:,}  cost={fmt_eth(actual)}", indent=1)
                self.stats["executed" if ok else "failed"] += 1
                w["balance"][chain] = max(0, w["balance"].get(chain, 0) - actual)
                if cost > actual:
                    self.guard.refund(cost - actual)
                    self.guard._state["mints"] += 1   # refund() decremented it
                    self.guard._save()
            except Exception as e:
                self.guard.refund(cost)
                self.stats["failed"] += 1
                log(f"  [{w['index']:>2}] SEND FAILED — {type(e).__name__}: "
                    f"{str(e)[:110]}", indent=1)

        log(f"DECISION fleet complete — {sent}/{len(armed)} transactions sent, "
            f"{sent * qty} token(s) attempted", indent=1)

    # --------------------------------------------------------------- loop
    def fire_due(self) -> None:
        """Execute queued drops whose window has opened. No re-analysis.

        The verdict was decided when the drop was discovered, minutes to hours
        early. This loop is deliberately dumb and fast -- it re-reads the config
        (which is mutable) and simulates, but it never re-runs enrichment or the
        model. That is the whole point of scheduling on startTime.
        """
        now = int(time.time())
        for key, item in list(self.pending.items()):
            cand = item["cand"]
            if cand.end_time <= now:
                del self.pending[key]
                log(f"[{_ts()}] EXPIRED  {cand.collection} — window closed "
                    f"before it could be minted")
                continue
            if cand.start_time > now:
                continue
            del self.pending[key]
            log("")
            rule()
            log(f"[{_ts()}] FIRING QUEUED DROP  {cand.collection}  "
                f"(approved {fmt_in(-(now - item['queued_at']))} ago, "
                f"score {item['score']})")
            try:
                self.execute(item["chain"], cand, {})
            except Exception:
                log("[error] queued execution failed:")
                traceback.print_exc()

    def heartbeat(self) -> None:
        s = self.stats
        b = self.guard.summary
        up = int(time.time() - _START)
        rule()
        log(f"[{_ts()}] HEARTBEAT  up {up // 3600}h{(up % 3600) // 60:02d}m "
            f"(single continuous process — a rising uptime here means it is "
            f"NOT restarting)")
        log(f"seen={s['candidates']}  prefiltered_out={s['prefiltered']}  "
            f"llm_calls={s['triaged']}  MINT={s['mint']} WATCH={s['watch']} "
            f"SKIP={s['skip']}", indent=1)
        log(f"executed={s['executed']}  failed={s['failed']}  "
            f"spent={b['spent_eth']} / {b['budget_eth']} ETH  "
            f"mints={b['mints']}/{b['max_mints']}", indent=1)
        if s.get("gas_saved_wei"):
            log(f"wasted gas avoided by pre-flight: "
                f"{s['gas_saved_wei'] / 1e18:.8f} ETH", indent=1)
        if self.pending:
            nxt = min(self.pending.values(), key=lambda i: i["cand"].start_time)
            log(f"queued={len(self.pending)} approved drop(s) awaiting their "
                f"window; next {nxt['cand'].collection[:12]} in "
                f"{fmt_in(nxt['cand'].start_time - int(time.time()))}", indent=1)
        rule()

    def run(self) -> int:
        if not self.preflight_config():
            return 2
        last_hb = 0.0
        while not self._stop:
            self.fire_due()
            for chain in self.chains:
                try:
                    if self.fleet:
                        armed, total = self.refresh_balances(chain)
                        if armed != self._last_armed.get(chain):
                            log(f"[{_ts()}] [wallet] {chain}: {armed}/"
                                f"{len(self.fleet)} armed  "
                                f"fleet total {total / 1e18:.6f} ETH")
                            self._last_armed[chain] = armed
                    cands = self.watchers[chain].poll(
                        lookback_minutes=self.lookback_min, save=True)
                    for c in cands:
                        if self._stop:
                            break
                        self.handle(chain, c)
                except Exception:
                    log(f"[{_ts()}] [error] {chain} poll failed:")
                    traceback.print_exc()
            if time.time() - last_hb > 300:
                self.heartbeat()
                last_hb = time.time()
            self._social_this_cycle = 0
            for _ in range(int(self.poll_s)):
                if self._stop:
                    break
                time.sleep(1)
        self.heartbeat()
        log("stopped cleanly.")
        return 0


def main() -> int:
    return Runner().run()


if __name__ == "__main__":
    sys.exit(main())
