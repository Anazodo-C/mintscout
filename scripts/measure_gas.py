"""Measure the real cost of a SeaDrop mint from live receipts.

The whole EIP-7702 framing turns on this number, so it is measured here rather
than quoted. Writes data/fixtures/gas_measurement.json.
"""
import json, pathlib, statistics, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from concurrent.futures import ThreadPoolExecutor
from mintscout.rpc import client
from mintscout import constants as C

OUT = pathlib.Path(__file__).resolve().parents[1] / "data/fixtures/gas_measurement.json"

def main(chain="robinhood", n=60):
    c = client(chain)
    tip = c.block_number()
    logs = c.get_logs_chunked(C.SEADROP, [C.TOPIC_SEADROP_MINT], tip - 40_000, tip)
    txs = list(dict.fromkeys(l["transactionHash"] for l in logs))[:n]
    print(f"{chain}: sampling {len(txs)} mint transactions")
    rows = []
    def one(h):
        try:
            r = c.get_receipt(h)
            if not r: return
            rows.append({"gas_used": int(r["gasUsed"], 16),
                         "effective_gas_price": int(r.get("effectiveGasPrice", "0x0"), 16),
                         "status": int(r.get("status", "0x1"), 16)})
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(one, txs))
    ok = [r for r in rows if r["status"] == 1]
    gas = [r["gas_used"] for r in ok]
    price = [r["effective_gas_price"] for r in ok]
    fee = [g * p for g, p in zip(gas, price)]
    res = {
        "chain": chain, "n_receipts": len(ok),
        "median_gas_used": int(statistics.median(gas)),
        "median_effective_gas_price_wei": int(statistics.median(price)),
        "median_fee_wei": int(statistics.median(fee)),
        "median_fee_eth": statistics.median(fee) / 1e18,
        "reverted_in_sample": len(rows) - len(ok),
        "note": ("Basis for the 'gas is not the value proposition' claim. "
                 "Batching N mints saves roughly (N-1) x 21000 gas of intrinsic "
                 "transaction overhead, which at this gas price is a fraction of "
                 "a cent -- see CHANGELOG removed-experiment #1."),
    }
    b = 21000 * statistics.median(price)
    res["intrinsic_overhead_saved_per_extra_mint_eth"] = b / 1e18
    res["batching_5_mints_saves_eth"] = 4 * b / 1e18
    OUT.write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    main(*(sys.argv[1:] or []))
