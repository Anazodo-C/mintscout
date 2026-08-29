"""Capture real mainnet logs as committed fixtures for topic assertions.

BUILD.md non-negotiable #1: derive every topic with keccak and assert it against
a committed fixture captured from a real log. This script produces that fixture.
"""
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from mintscout.rpc import client
from mintscout import constants as C

OUT = pathlib.Path(__file__).resolve().parents[1] / "data/fixtures/topics.json"
SIGS = {
    "PublicDropUpdated": C.SIG_PUBLIC_DROP_UPDATED,
    "SeaDropMint": C.SIG_SEADROP_MINT,
    "DropURIUpdated": C.SIG_DROP_URI_UPDATED,
    "CreatorPayoutAddressUpdated": C.SIG_CREATOR_PAYOUT_UPDATED,
}

def main():
    fixture = {"_note": "Captured from live mainnet logs. verify.py asserts that "
                        "keccak(signature) equals topic0 of each sample log.",
               "events": {}, "selectors": {}}
    c = client("robinhood")
    tip = c.block_number()
    logs = c.get_logs_chunked(C.SEADROP, None, tip - 60_000, tip)
    by_topic = {}
    for l in logs:
        by_topic.setdefault(l["topics"][0], l)
    for name, sig in SIGS.items():
        t0 = C.topic(sig)
        sample = by_topic.get(t0)
        if sample is None:
            print(f"  ! no live sample for {name} in window; skipping")
            continue
        fixture["events"][name] = {
            "signature": sig,
            "topic0": t0,
            "sample": {"chain": "robinhood", "address": sample["address"],
                       "blockNumber": sample["blockNumber"],
                       "transactionHash": sample["transactionHash"],
                       "topics": sample["topics"], "data": sample["data"]},
        }
        print(f"  captured {name}: {t0}")
    # Transfer is emitted by collections, not SeaDrop -- sample one separately.
    cols = sorted({"0x" + l["topics"][1][-40:] for l in logs
                   if l["topics"][0] == C.TOPIC_SEADROP_MINT})[:40]
    if cols:
        tlogs = c.get_logs_chunked(cols, [C.TOPIC_TRANSFER], tip - 20_000, tip)
        t = next((l for l in tlogs if len(l["topics"]) == 4), None)
        if t:
            fixture["events"]["Transfer"] = {
                "signature": C.SIG_TRANSFER, "topic0": C.TOPIC_TRANSFER,
                "sample": {"chain": "robinhood", "address": t["address"],
                           "blockNumber": t["blockNumber"],
                           "transactionHash": t["transactionHash"],
                           "topics": t["topics"], "data": t["data"]}}
            print(f"  captured Transfer: {C.TOPIC_TRANSFER}")
    fixture["selectors"] = {
        C.SIG_MINT_PUBLIC: C.SEL_MINT_PUBLIC,
        C.SIG_GET_PUBLIC_DROP: C.SEL_GET_PUBLIC_DROP,
    }
    fixture["unidentified_topics"] = {
        C.TOPIC_UNIDENTIFIED_TOKEN_GATED:
            "Observed on SeaDrop (3 topics, 14 data words). Shaped like a "
            "token-gated drop stage. Not matched to a canonical signature and "
            "not decoded -- MintScout discovers public drops only. Recorded as a "
            "known-unknown rather than guessed at."}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, indent=2) + "\n")
    print(f"wrote {OUT} ({len(fixture['events'])} events)")

if __name__ == "__main__":
    main()
