"""Realised secondary sale prices from Seaport, on-chain.

Why realised prices rather than "floor": floor is a listing (an ask nobody has
accepted) and needs a marketplace API key. Realised sales are what someone
actually paid, they are fully on-chain, and they need no credentials.

Method: find secondary Transfers of the collection (from != 0x0), pull each
transaction's receipt, and decode Seaport's OrderFulfilled consideration. Sales
on this chain settle in WETH (0x0bd7d308f8e1...), so tx.value is 0 and the
amount only appears in the consideration array.

Reports, per collection:
  * early  -- median of the first sales after the mint (a proxy for the floor
              right after mint)
  * recent -- median of the most recent sales (a proxy for the current floor)
"""
import argparse, json, pathlib, statistics, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from concurrent.futures import ThreadPoolExecutor

from eth_utils import keccak

from mintscout import constants as C
from mintscout.rpc import client

ROOT = pathlib.Path(__file__).resolve().parents[1]

ORDER_FULFILLED = "0x" + keccak(text=(
    "OrderFulfilled(bytes32,address,address,address,"
    "(uint8,address,uint256,uint256)[],"
    "(uint8,address,uint256,uint256,address)[])")).hex()

# itemType 0 = native ETH, 1 = ERC-20 (WETH here). 2/3 are the NFT itself.
PAYMENT_ITEM_TYPES = (0, 1)


def decode_sale_total(data_hex: str) -> int:
    """Sum the payment legs of one OrderFulfilled event."""
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    try:
        off_cons = int(d[3 * 64:4 * 64], 16) * 2
        n = int(d[off_cons:off_cons + 64], 16)
        total = 0
        for i in range(n):
            base = off_cons + 64 + i * 5 * 64
            item_type = int(d[base:base + 64], 16)
            amount = int(d[base + 3 * 64:base + 4 * 64], 16)
            if item_type in PAYMENT_ITEM_TYPES:
                total += amount
        return total
    except Exception:
        return 0


def collection_sales(rpc, collection: str, lookback: int = 1_500_000,
                     max_sales: int = 40) -> list[tuple[int, int]]:
    """[(block, price_wei)] for secondary sales, oldest first."""
    tip = rpc.block_number()
    try:
        logs = rpc.get_logs_chunked(collection, [C.TOPIC_TRANSFER],
                                    max(1, tip - lookback), tip)
    except Exception:
        return []
    sec = [l for l in logs
           if len(l.get("topics", [])) == 4 and int(l["topics"][1], 16) != 0]
    # de-duplicate by transaction; one tx can move several tokens
    seen, txs = set(), []
    for l in sec:
        h = l["transactionHash"]
        if h not in seen:
            seen.add(h)
            txs.append((int(l["blockNumber"], 16), h))
    txs.sort()
    if len(txs) > max_sales * 2:          # sample the ends, not the middle
        txs = txs[:max_sales] + txs[-max_sales:]

    out = []

    def one(item):
        blk, h = item
        try:
            r = rpc.get_receipt(h)
        except Exception:
            return
        if not r:
            return
        best = 0
        for l in r.get("logs", []):
            if l["topics"] and l["topics"][0] == ORDER_FULFILLED:
                best = max(best, decode_sale_total(l["data"]))
        if best > 0:
            out.append((blk, best))

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(one, txs))
    out.sort()
    return out


def summarise(sales: list[tuple[int, int]], n: int = 8) -> dict:
    if not sales:
        return {"n_sales": 0, "early_median_eth": None, "recent_median_eth": None,
                "change_pct": None}
    prices = [p for _, p in sales]
    early = statistics.median(prices[:min(n, len(prices))])
    recent = statistics.median(prices[-min(n, len(prices)):])
    return {
        "n_sales": len(sales),
        "early_median_eth": round(early / 1e18, 8),
        "recent_median_eth": round(recent / 1e18, 8),
        "min_eth": round(min(prices) / 1e18, 8),
        "max_eth": round(max(prices) / 1e18, 8),
        "change_pct": round((recent - early) / early * 100, 1) if early else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collections", required=True,
                    help="comma-separated addresses, or a path to a file of them")
    ap.add_argument("--chain", default="robinhood")
    ap.add_argument("--out", default=str(ROOT / "data/secondary_prices.json"))
    a = ap.parse_args()

    p = pathlib.Path(a.collections)
    cols = ([c.strip() for c in p.read_text().split() if c.strip()]
            if p.exists() else
            [c.strip() for c in a.collections.split(",") if c.strip()])
    rpc = client(a.chain)
    res = {}
    for i, col in enumerate(cols, 1):
        s = collection_sales(rpc, col)
        res[col] = summarise(s)
        print(f"[{i}/{len(cols)}] {col[:14]}…  {json.dumps(res[col])}", flush=True)
    pathlib.Path(a.out).write_text(json.dumps(res, indent=1) + "\n")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
