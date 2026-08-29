"""Environment + constant verification. `python -m mintscout.verify`

This is the one-command proof that the claims MintScout is built on are true
*today*, not just on the day the research was done. It fails loudly rather than
letting the pipeline run on stale assumptions.
"""
from __future__ import annotations

import json
import pathlib
import sys

from . import constants as C
from .rpc import client

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "data/fixtures/topics.json"

_PASS, _FAIL = "PASS", "FAIL"


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, _PASS if ok else _FAIL, detail))
        print(f"  [{_PASS if ok else _FAIL}] {name}" + (f"  -- {detail}" if detail else ""))
        return ok

    @property
    def failed(self) -> list[tuple[str, str, str]]:
        return [r for r in self.rows if r[1] == _FAIL]


def mask(code: bytes) -> bytes:
    """Zero the known deploy-time immutable ranges so two chains' SeaDrop
    bytecode can be compared for implementation equality."""
    b = bytearray(code)
    for lo, hi in C.SEADROP_IMMUTABLE_RANGES:
        for i in range(lo, min(hi, len(b))):
            b[i] = 0
    return bytes(b)


def main(argv: list[str] | None = None) -> int:
    ck = Checks()
    print("MintScout environment verification")
    print("=" * 72)

    # ---- 1. derived selectors / topics ------------------------------------
    print("\n[1] Derived selectors and topics (keccak at import, no literals)")
    ck.check("mintPublic selector == 0x161ac21f",
             C.SEL_MINT_PUBLIC == "0x161ac21f", C.SEL_MINT_PUBLIC)
    ck.check("getPublicDrop selector == 0xbc6a629c",
             C.SEL_GET_PUBLIC_DROP == "0xbc6a629c", C.SEL_GET_PUBLIC_DROP)

    if not FIXTURES.exists():
        ck.check("topic fixture present", False, f"missing {FIXTURES}")
    else:
        fx = json.loads(FIXTURES.read_text())
        for name, ent in fx["events"].items():
            derived = C.topic(ent["signature"])
            sample_t0 = ent["sample"]["topics"][0]
            ck.check(f"{name}: keccak(sig) == committed topic0",
                     derived == ent["topic0"], derived)
            ck.check(f"{name}: matches topic0 of a real log "
                     f"(blk {int(ent['sample']['blockNumber'], 16)})",
                     derived == sample_t0, ent["sample"]["transactionHash"][:18] + "...")

    # ---- 2. live chain identity ------------------------------------------
    print("\n[2] Live chain identity")
    codes: dict[str, bytes] = {}
    for name, cfg in C.CHAINS.items():
        try:
            c = client(name)
            cid = c.get_chain_id()
            ck.check(f"{name}: eth_chainId == {cfg['chain_id']}",
                     cid == cfg["chain_id"], str(cid))
            codes[name] = c.get_code(C.SEADROP)
        except Exception as e:
            ck.check(f"{name}: reachable", False, f"{type(e).__name__}: {e}")

    # ---- 3. SeaDrop deployment -------------------------------------------
    print("\n[3] SeaDrop deployment")
    for name, code in codes.items():
        ck.check(f"{name}: SeaDrop code size == {C.SEADROP_CODE_SIZE}",
                 len(code) == C.SEADROP_CODE_SIZE, f"{len(code)} bytes")

    if len(codes) >= 2:
        names = list(codes)
        a, b = codes[names[0]], codes[names[1]]
        ck.check(f"SeaDrop implementation identical across "
                 f"{names[0]}/{names[1]} (immutables masked)",
                 mask(a) == mask(b),
                 f"{sum(1 for i in range(min(len(a), len(b))) if a[i] != b[i])} raw bytes differ, "
                 f"all inside known immutable ranges")
        # The chain id is baked into the bytecode as an immutable -- assert it
        # equals the chain we actually reached. This is a far stronger check
        # than a size comparison: it proves the contract was deployed for THIS
        # chain and is not a copy from another network.
        lo, hi = C.SEADROP_IMMUTABLE_RANGES[0]
        for name, code in codes.items():
            baked = int.from_bytes(code[lo:hi], "big")
            ck.check(f"{name}: chain id baked into SeaDrop bytecode == "
                     f"{C.CHAINS[name]['chain_id']}",
                     baked == C.CHAINS[name]["chain_id"], f"offset {lo}: {baked}")

    # ---- 4. getPublicDrop against a reference collection ------------------
    print("\n[4] Live getPublicDrop() read (reference collection)")
    UNDEADLINES = "0xed74a2029ff2633f21260e097e451358c26a507d"
    try:
        c = client("robinhood")
        data = C.SEL_GET_PUBLIC_DROP + "00" * 12 + UNDEADLINES[2:]
        res = c.call(C.SEADROP, data)
        words = [int(res[2:][i * 64:(i + 1) * 64], 16) for i in range(6)]
        ck.check("UNDEADLINES getPublicDrop returns 6 decodable words",
                 len(words) == 6, f"price={words[0]} cap={words[3]} feeBps={words[4]}")
        ck.check("UNDEADLINES mintPrice == 0 (free) as documented",
                 words[0] == 0, f"{words[0]} wei")
        ck.check("UNDEADLINES maxTotalMintableByWallet == 2 as documented",
                 words[3] == 2, str(words[3]))
    except Exception as e:
        ck.check("getPublicDrop reachable", False, f"{type(e).__name__}: {e}")

    # ---- summary ----------------------------------------------------------
    print("\n" + "=" * 72)
    n = len(ck.rows)
    if ck.failed:
        print(f"FAILED {len(ck.failed)}/{n} checks:")
        for name, _, detail in ck.failed:
            print(f"   - {name} ({detail})")
        return 1
    print(f"All {n} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
