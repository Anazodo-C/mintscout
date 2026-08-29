"""Log decoders for the SeaDrop events MintScout consumes.

Layouts were confirmed against live mainnet logs, not read off a spec.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


def _w(data_hex: str, i: int) -> int:
    """Word i of an 0x-prefixed ABI blob, as int."""
    h = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return int(h[i * 64:(i + 1) * 64] or "0", 16)


def _addr_from_topic(t: str) -> str:
    return "0x" + t[-40:].lower()


@dataclass
class PublicDrop:
    chain: str
    collection: str
    mint_price: int          # wei (uint80). 0 == free -- this IS the filter
    start_time: int          # unix (uint48), known BEFORE the mint opens
    end_time: int            # unix (uint48)
    max_per_wallet: int      # uint16
    fee_bps: int             # uint16
    restrict_fee_recipients: bool
    block_number: int
    log_index: int
    tx_hash: str
    block_timestamp: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def is_free(self) -> bool:
        return self.mint_price == 0

    @property
    def duration_s(self) -> int:
        return max(0, self.end_time - self.start_time)


def decode_public_drop_updated(log: dict, chain: str) -> PublicDrop:
    """PublicDropUpdated(address indexed nftContract, PublicDrop drop).

    The struct is fully static (uint80,uint48,uint48,uint16,uint16,bool) so it is
    inlined as 6 words in `data` -- no offset word.
    """
    d = log["data"]
    return PublicDrop(
        chain=chain,
        collection=_addr_from_topic(log["topics"][1]),
        mint_price=_w(d, 0),
        start_time=_w(d, 1),
        end_time=_w(d, 2),
        max_per_wallet=_w(d, 3),
        fee_bps=_w(d, 4),
        restrict_fee_recipients=bool(_w(d, 5)),
        block_number=int(log["blockNumber"], 16),
        log_index=int(log.get("logIndex", "0x0"), 16),
        tx_hash=log["transactionHash"],
    )


@dataclass
class SeaDropMintEvent:
    chain: str
    collection: str
    minter: str
    fee_recipient: str
    payer: str
    quantity: int
    unit_mint_price: int
    fee_bps: int
    drop_stage_index: int
    block_number: int
    tx_hash: str
    block_timestamp: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def decode_seadrop_mint(log: dict, chain: str) -> SeaDropMintEvent:
    """SeaDropMint(address indexed nftContract, address indexed minter,
    address indexed feeRecipient, address payer, uint256 quantityMinted,
    uint256 unitMintPrice, uint256 feeBps, uint256 dropStageIndex)"""
    d = log["data"]
    return SeaDropMintEvent(
        chain=chain,
        collection=_addr_from_topic(log["topics"][1]),
        minter=_addr_from_topic(log["topics"][2]),
        fee_recipient=_addr_from_topic(log["topics"][3]),
        payer="0x" + f"{_w(d, 0):040x}"[-40:],
        quantity=_w(d, 1),
        unit_mint_price=_w(d, 2),
        fee_bps=_w(d, 3),
        drop_stage_index=_w(d, 4),
        block_number=int(log["blockNumber"], 16),
        tx_hash=log["transactionHash"],
    )


def decode_drop_uri_updated(log: dict) -> tuple[str, str]:
    """DropURIUpdated(address indexed nftContract, string newDropURI) -> (collection, uri)"""
    d = log["data"][2:]
    offset = int(d[0:64], 16) * 2
    length = int(d[offset:offset + 64], 16)
    raw = d[offset + 64: offset + 64 + length * 2]
    return _addr_from_topic(log["topics"][1]), bytes.fromhex(raw).decode("utf8", "replace")


def decode_transfer(log: dict) -> dict | None:
    """ERC-721 Transfer(from,to,tokenId) -- 4 topics.

    ERC-20 Transfer shares topic0 but carries only 3 topics with the value in
    data; those are filtered out so token moves are never counted as NFT trades.
    """
    if len(log["topics"]) != 4:
        return None
    return {
        "from": _addr_from_topic(log["topics"][1]),
        "to": _addr_from_topic(log["topics"][2]),
        "token_id": int(log["topics"][3], 16),
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"],
        "contract": log["address"].lower(),
    }
