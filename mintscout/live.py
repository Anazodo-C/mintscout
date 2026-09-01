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
from .executor import (DEFAULT_MINT_GAS, build_mint_tx, estimate_cost_wei,
                       read_public_drop, send_mint, wait_for_receipt)
from .rpc import client
from .watcher import Watcher

# ------------------------------------------------------------------ logging
_START = time.time()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str = "", *, indent: int = 0) -> None:
    print(("  " * indent) + msg, flush=True)


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
        seed = (os.environ.get("MINT_SEED") or "").strip()
        if not seed:
            log("[wallet] MINT_SEED not set -> observation only, cannot execute")
            return
        from eth_account import Account
        Account.enable_unaudited_hdwallet_features()
        idx = int(os.environ.get("WALLET_INDEX", "0"))
        acct = Account.from_mnemonic(seed, account_path=f"m/44'/60'/0'/0/{idx}")
        self.wallet = acct.address
        self._key = acct.key.hex()          # never logged, never serialised
        log(f"[wallet] {self.wallet}  (derivation index {idx})")
        for ch, rpc in self.rpcs.items():
            try:
                bal = int(rpc.raw("eth_getBalance", [self.wallet, "latest"]), 16)
                log(f"[wallet] {ch}: {bal / 1e18:.6f} ETH")
                if bal == 0:
                    log(f"[wallet] WARNING {ch} balance is zero — cannot mint")
            except Exception as e:
                log(f"[wallet] {ch}: balance read failed ({type(e).__name__})")

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
        log(f"  gas reserve     {fmt_limit(lim.gas_reserve_wei)}", indent=1)
        for warn in (("MAX_SPEND_PER_MINT_WEI", lim.max_spend_per_mint_wei),
                     ("MAX_MINTS_PER_HOUR", lim.max_mints_per_hour)):
            if not warn[1]:
                log(f"  WARNING {warn[0]} is not set — that limit is NOT enforced",
                    indent=1)
        v = volume_check()
        icon = "OK " if v["persisted"] else ("!! " if not v["writable"] else "?? ")
        log(f"state volume    : {icon}{v['state_dir']}  boots={v['boots']}  "
            f"writable={v['writable']}")
        log(f"                  {v['detail']}")
        if not v["writable"]:
            log("                  SPEND STATE CANNOT BE SAVED — caps will not "
                "survive a restart. Mount a Railway volume at this path.")
        elif not v["persisted"]:
            log("                  (expected on a genuinely first deploy)")
        self.load_wallet()
        if self.live:
            if not self._key:
                log("\n[FATAL] LIVE_EXECUTION is on but MINT_SEED is not set.")
                return False
            try:
                self.guard.check(0, 0)
            except Denied as e:
                log(f"\n[FATAL] live execution refused: {e}")
                return False
            log("\n*** LIVE MODE — this process will spend real funds ***")
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

        self.execute(chain, cand, dossier)

    # ------------------------------------------------------------ execute
    def execute(self, chain: str, cand, dossier: dict) -> None:
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

        if not self._key:
            log("DECISION DRY RUN — would mint, but no MINT_SEED configured", indent=1)
            return

        value = live_cfg["mint_price"] * qty
        nonce = int(rpc.raw("eth_getTransactionCount", [self.wallet, "pending"]), 16)
        tx = build_mint_tx(rpc, cand.collection, self.wallet, qty, value, nonce)
        cost = estimate_cost_wei(tx)
        try:
            bal = int(rpc.raw("eth_getBalance", [self.wallet, "latest"]), 16)
        except Exception:
            bal = None

        log(f"EXECUTE quantity={qty}  value={fmt_eth(value)}  "
            f"max_cost={fmt_eth(cost)}  gas={tx['gas']}  "
            f"maxFee={tx['maxFeePerGas'] / 1e9:.3f} gwei", indent=1)

        # eth_call the exact transaction. Cheaper to find out now than on-chain.
        try:
            rpc.call(C.SEADROP, tx["data"])
            log("       pre-flight simulation: OK", indent=1)
        except Exception as e:
            log(f"DECISION ABORT — pre-flight would revert: {str(e)[:110]}", indent=1)
            return

        try:
            self.guard.check(cost, tx["maxFeePerGas"], wallet_balance_wei=bal)
        except Denied as e:
            log(f"DECISION BLOCKED by spend guard — {e}", indent=1)
            return

        if not self.live:
            log("DECISION DRY RUN — all checks passed; would broadcast now. "
                "Set DRY_RUN=false and LIVE_EXECUTION=true to send.", indent=1)
            return

        # Commit budget BEFORE broadcasting: a crash must over-count, not under.
        self.guard.commit(cost)
        try:
            h = send_mint(rpc, tx, self._key)
            log(f"DECISION SENT  tx={h}", indent=1)
            r = wait_for_receipt(rpc, h)
            if r is None:
                log("       receipt not seen within timeout (tx may still land)", indent=1)
                self.stats["executed"] += 1
                return
            ok = int(r.get("status", "0x0"), 16) == 1
            used = int(r.get("gasUsed", "0x0"), 16)
            price = int(r.get("effectiveGasPrice", "0x0"), 16)
            actual = value + used * price
            log(f"       {'CONFIRMED' if ok else 'REVERTED'}  gasUsed={used:,}  "
                f"actual_cost={fmt_eth(actual)}  block={int(r['blockNumber'], 16)}",
                indent=1)
            self.stats["executed" if ok else "failed"] += 1
            if cost > actual:
                self.guard.refund(cost - actual)   # give back the over-estimate
                self.guard._state["mints"] += 1    # refund() decremented the count
                self.guard._save()
        except Exception as e:
            self.guard.refund(cost)
            self.stats["failed"] += 1
            log(f"DECISION SEND FAILED — {type(e).__name__}: {str(e)[:140]}", indent=1)

    # --------------------------------------------------------------- loop
    def heartbeat(self) -> None:
        s = self.stats
        b = self.guard.summary
        rule()
        log(f"[{_ts()}] HEARTBEAT  up {int(time.time() - _START) // 60}m")
        log(f"seen={s['candidates']}  prefiltered_out={s['prefiltered']}  "
            f"llm_calls={s['triaged']}  MINT={s['mint']} WATCH={s['watch']} "
            f"SKIP={s['skip']}", indent=1)
        log(f"executed={s['executed']}  failed={s['failed']}  "
            f"spent={b['spent_eth']} / {b['budget_eth']} ETH  "
            f"mints={b['mints']}/{b['max_mints']}", indent=1)
        rule()

    def run(self) -> int:
        if not self.preflight_config():
            return 2
        last_hb = 0.0
        while not self._stop:
            for chain in self.chains:
                try:
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
