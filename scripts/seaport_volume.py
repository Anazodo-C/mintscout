"""Realised secondary volume per collection, from ONE Seaport log scan.

The first version scanned each collection's Transfer logs and then fetched a
receipt per sale -- 181 collections x N receipts, tens of thousands of requests.

Seaport's OrderFulfilled event already contains both the NFT that moved (in the
`offer` array) and what was paid (in `consideration`), so a single scan of the
Seaport contract yields every sale on the chain, which is then bucketed by
collection client-side. One scan replaces the lot.

Not obtainable without an OpenSea API key, and therefore not reported here:
  * floor price  -- a listing, i.e. an ask nobody has accepted
  * top offer    -- signed off-chain Seaport orders that never touch the chain
Realised volume and realised prices are what someone actually paid, which is the
stronger evidence anyway.

Item types: 0=native ETH, 1=ERC20 (WETH), 2=ERC721, 3=ERC1155.
"""
import argparse, json, pathlib, statistics, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from eth_utils import keccak

from mintscout import constants as C
from mintscout.blockclock import BlockClock
from mintscout.rpc import client

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEAPORT = "0x0000000000000068f116a894984e2db1123eb395"
# Only native ETH and WETH count as payment. Seaport orders can settle in ANY
# ERC-20, and counting those at face value as ETH produced a collection with
# "61,005,000 ETH volume" at a "1,000,000 ETH median" -- a memecoin-denominated
# sale read as ether. Item type alone is not enough; the token must be checked.
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
ORDER_FULFILLED = "0x" + keccak(text=(
    "OrderFulfilled(bytes32,address,address,address,"
    "(uint8,address,uint256,uint256)[],"
    "(uint8,address,uint256,uint256,address)[])")).hex()


def decode(data_hex: str):
    """-> (nft_contracts:set, paid_wei:int) for one OrderFulfilled."""
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    nfts, paid = set(), 0
    try:
        off_offer = int(d[2 * 64:3 * 64], 16) * 2
        n_off = int(d[off_offer:off_offer + 64], 16)
        for i in range(n_off):
            b = off_offer + 64 + i * 4 * 64
            it = int(d[b:b + 64], 16)
            tok = "0x" + d[b + 64:b + 128][-40:]
            amt = int(d[b + 3 * 64:b + 4 * 64], 16)
            if it in (2, 3):
                nfts.add(tok.lower())
            elif it == 0 or (it == 1 and tok.lower() == WETH):
                paid += amt          # NFT-for-ETH orders can pay on either leg
        off_cons = int(d[3 * 64:4 * 64], 16) * 2
        n_cons = int(d[off_cons:off_cons + 64], 16)
        for i in range(n_cons):
            b = off_cons + 64 + i * 5 * 64
            it = int(d[b:b + 64], 16)
            tok = "0x" + d[b + 64:b + 128][-40:]
            amt = int(d[b + 3 * 64:b + 4 * 64], 16)
            if it in (2, 3):
                nfts.add(tok.lower())
            elif it == 0 or (it == 1 and tok.lower() == WETH):
                paid += amt
    except Exception:
        return set(), 0
    return nfts, paid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="robinhood")
    ap.add_argument("--days", type=float, default=10.0)
    ap.add_argument("--out", default=str(ROOT / "data/seaport_volume.json"))
    a = ap.parse_args()

    rpc = client(a.chain)
    clock = BlockClock(rpc)
    import time
    tip = rpc.block_number()
    frm = max(1, clock.block_of(int(time.time()) - int(a.days * 86400)))
    print(f"scanning Seaport {SEAPORT[:12]}… blocks {frm}..{tip} "
          f"({a.days} days)", flush=True)
    logs = rpc.get_logs_chunked(SEAPORT, [ORDER_FULFILLED], frm, tip,
                                progress=True, label="OrderFulfilled")
    print(f"{len(logs)} OrderFulfilled events", flush=True)

    per: dict[str, dict] = {}
    seen_tx = set()
    for l in logs:
        nfts, paid = decode(l["data"])
        if not nfts or paid <= 0:
            continue
        # One sale emits two OrderFulfilled events (order + counter-order);
        # counting both would double the volume.
        key = (l["transactionHash"], tuple(sorted(nfts)))
        if key in seen_tx:
            continue
        seen_tx.add(key)
        blk = int(l["blockNumber"], 16)
        for n in nfts:
            e = per.setdefault(n, {"sales": 0, "volume_wei": 0, "prices": [],
                                   "first_block": blk, "last_block": blk})
            e["sales"] += 1
            e["volume_wei"] += paid
            e["prices"].append((blk, paid))
            e["first_block"] = min(e["first_block"], blk)
            e["last_block"] = max(e["last_block"], blk)

    out = {}
    for col, e in per.items():
        pr = sorted(e["prices"])
        vals = [p for _, p in pr]
        k = max(1, len(vals) // 5)
        out[col] = {
            "sales": e["sales"],
            "volume_eth": round(e["volume_wei"] / 1e18, 8),
            "avg_price_eth": round(e["volume_wei"] / e["sales"] / 1e18, 8),
            "median_price_eth": round(statistics.median(vals) / 1e18, 8),
            "min_price_eth": round(min(vals) / 1e18, 8),
            "max_price_eth": round(max(vals) / 1e18, 8),
            # realised proxies for "floor then" and "floor now"
            "early_median_eth": round(statistics.median(vals[:k]) / 1e18, 8),
            "recent_median_eth": round(statistics.median(vals[-k:]) / 1e18, 8),
        }
    pathlib.Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    print(f"{len(out)} collections with realised sales -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
