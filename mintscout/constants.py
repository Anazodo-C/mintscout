"""Verified constants for MintScout.

EVERY value in this file that could drift is either (a) measured live and
re-asserted by ``mintscout.verify``, or (b) derived at import time with keccak.

Non-negotiable #1 from BUILD.md: never invent an event topic hash or function
selector. Nothing below is a literal topic hash -- they are all derived from the
canonical signature text at import, then asserted against committed fixtures
captured from real mainnet logs (data/fixtures/topics.json).
"""
from __future__ import annotations

from eth_utils import keccak

# --------------------------------------------------------------------------
# SeaDrop
# --------------------------------------------------------------------------
SEADROP = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"

# MEASURED 2026-08-29 against both mainnets. BUILD.md/guide.md both state 21082;
# the live value is 21081 on Robinhood AND Ink. The spec was off by one.
# verify.py asserts the measured value, not the documented one.
SEADROP_CODE_SIZE = 21081

# The guide claims the bytecode is "byte-for-byte identical" across chains.
# It is not, and the reason is interesting: exactly 34 bytes differ, in two
# runs, and both are deploy-time immutables:
#   offset 14462..14463  -> the 2-byte chain id  (0x1237=4663, 0xdef1=57073)
#   offset 14645..14676  -> the 32-byte cached EIP-712 domain separator, which
#                           is itself derived from that chain id
# So it is the same *implementation* with different cached immutables. We assert
# the much stronger property: identical after masking these two ranges.
SEADROP_IMMUTABLE_RANGES = ((14462, 14464), (14645, 14677))

# --------------------------------------------------------------------------
# Chains
# --------------------------------------------------------------------------
CHAINS: dict[str, dict] = {
    "robinhood": {
        "chain_id": 4663,
        "rpc": "https://rpc.mainnet.chain.robinhood.com",
        "block_time": 0.1,
        "stack": "arbitrum-orbit",   # ArbOS 116; EIP-7702 supported
        "max_concurrency": 6,
        # MEASURED: Robinhood has NO hard block-range cap. Large ranges fail with
        # -32000 "log query timed out" instead. 200k blocks succeed, 1M does not.
        # We start at 120k and halve adaptively on timeout.
        "max_log_range": 120_000,
        "explorer": "https://robinhoodchain.blockscout.com",  # Cloudflare-gated: never used in code
    },
    "ink": {
        "chain_id": 57073,
        "rpc": "https://rpc-gel.inkonchain.com",
        "block_time": 1.0,
        "stack": "op-stack",
        # Ink's public RPC is more fragile than Robinhood's under fan-out: it
        # answers oversized queries with HTTP 500 and rate-limits sooner.
        "max_concurrency": 4,
        # MEASURED: Ink enforces an exact cap -- 10001 blocks returns
        # -32602 "block range greater than 10000 max". 10000 is accepted.
        "max_log_range": 10_000,
        "explorer": "https://explorer.inkonchain.com",
    },
}
# Base (8453) is deliberately EXCLUDED -- see README. Its public RPC returns 429
# for a large fraction of requests at even modest concurrency.

# Legacy/global fallback. The real limit is PER CHAIN (see CHAINS[*]["max_log_range"]):
# BUILD.md states a 10_000 cap "on both chains"; that is true only for Ink.
MAX_LOG_RANGE = 10_000
GLOBAL_RATE_LIMIT_RPS = 7.0   # token bucket, per chain

# The public RPCs 403 requests that omit a User-Agent (python-urllib's default
# is rejected while curl's is accepted). Discovered the hard way; do not remove.
HTTP_USER_AGENT = "mintscout/0.1 (+micro1-frontier-2026)"

# --------------------------------------------------------------------------
# Function selectors -- derived, then asserted against the documented literals
# --------------------------------------------------------------------------
def selector(sig: str) -> str:
    return "0x" + keccak(text=sig).hex().replace("0x", "")[:8]


def topic(sig: str) -> str:
    return "0x" + keccak(text=sig).hex().replace("0x", "")


SIG_MINT_PUBLIC = "mintPublic(address,address,address,uint256)"
SIG_GET_PUBLIC_DROP = "getPublicDrop(address)"

SEL_MINT_PUBLIC = selector(SIG_MINT_PUBLIC)          # 0x161ac21f
SEL_GET_PUBLIC_DROP = selector(SIG_GET_PUBLIC_DROP)  # 0xbc6a629c
SEL_MINT_SIGNED = "0x4b61cd6f"  # documented; full signature not public -- unverified, unused

# ERC-165 / ERC-721 / ERC-1155 read selectors
SEL_SUPPORTS_INTERFACE = selector("supportsInterface(bytes4)")
SEL_TOTAL_SUPPLY = selector("totalSupply()")
SEL_MAX_SUPPLY = selector("maxSupply()")
SEL_OWNER = selector("owner()")
SEL_NAME = selector("name()")
SEL_SYMBOL = selector("symbol()")
SEL_TOKEN_URI = selector("tokenURI(uint256)")
SEL_CONTRACT_URI = selector("contractURI()")
SEL_BALANCE_OF = selector("balanceOf(address)")
SEL_OWNER_OF = selector("ownerOf(uint256)")
SEL_TRANSFER_FROM = selector("transferFrom(address,address,uint256)")

ERC165 = {"erc721": "0x80ac58cd", "erc721_metadata": "0x5b5e139f", "erc1155": "0xd9b67a26"}

# --------------------------------------------------------------------------
# Event topics -- derived at import, asserted against real logs by verify.py
# --------------------------------------------------------------------------
SIG_PUBLIC_DROP_UPDATED = "PublicDropUpdated(address,(uint80,uint48,uint48,uint16,uint16,bool))"
SIG_SEADROP_MINT = "SeaDropMint(address,address,address,address,uint256,uint256,uint256,uint256)"
SIG_DROP_URI_UPDATED = "DropURIUpdated(address,string)"
SIG_TRANSFER = "Transfer(address,address,uint256)"
SIG_CREATOR_PAYOUT_UPDATED = "CreatorPayoutAddressUpdated(address,address)"

TOPIC_PUBLIC_DROP_UPDATED = topic(SIG_PUBLIC_DROP_UPDATED)
TOPIC_SEADROP_MINT = topic(SIG_SEADROP_MINT)
TOPIC_DROP_URI_UPDATED = topic(SIG_DROP_URI_UPDATED)
TOPIC_TRANSFER = topic(SIG_TRANSFER)
TOPIC_CREATOR_PAYOUT_UPDATED = topic(SIG_CREATOR_PAYOUT_UPDATED)

# Observed on the SeaDrop address but not matched to a canonical signature.
# 3 topics + 14 data words; shaped like a token-gated drop stage. MintScout
# discovers *public* drops, so this is recorded, not decoded. Documented as a
# known-unknown rather than guessed at.
TOPIC_UNIDENTIFIED_TOKEN_GATED = "0xcaeb4009c05208df426d15ff50b608287b05d21dee1f790552ea451a540a7be0"

ZERO_ADDRESS = "0x" + "00" * 20
# Arbitrary non-zero address used only as an argument when probing whether a
# contract answers balanceOf(address). Never funded, never signed for.
PROBE_ADDRESS = "0x000000000000000000000000000000000000dEaD"

# --------------------------------------------------------------------------
# Policy defaults (safety -- see README §Safety model)
# --------------------------------------------------------------------------
DUST_THRESHOLD_WEI = 0          # "free" means exactly 0 by default
MAX_WALLETS_DEFAULT = 20
TARGET_PER_COLLECTION = 100
SWEEP_GAS_RESERVE_WEI = 200_000 * 2_000_000_000  # ~200k gas at 2 gwei headroom
