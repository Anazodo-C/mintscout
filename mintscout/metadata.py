"""Token metadata resolution with an on-disk pin, so the eval runs offline.

Three shapes appear in the wild and all three are handled:
  * data:application/json;base64,...  -- fully on-chain. No network at all.
  * data:application/json,{...}       -- fully on-chain, unencoded.
  * ipfs://CID/...  or  https://...   -- fetched once, then pinned to
                                         data/metadata/ and committed, so the
                                         eval is immune to CID rot and needs no
                                         network on a judge's machine.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIN_DIR = ROOT / "data/metadata"
GATEWAYS = ("https://ipfs.io/ipfs/", "https://cloudflare-ipfs.com/ipfs/",
            "https://dweb.link/ipfs/")


def _key(uri: str) -> str:
    return hashlib.sha256(uri.encode()).hexdigest()[:40]


def _pin_path(uri: str) -> pathlib.Path:
    return PIN_DIR / f"{_key(uri)}.json"


def is_on_chain(uri: str | None) -> bool:
    return bool(uri) and uri.strip().lower().startswith("data:")


def parse_data_uri(uri: str) -> dict | None:
    try:
        head, _, payload = uri.partition(",")
        raw = base64.b64decode(payload).decode("utf8", "replace") \
            if "base64" in head.lower() else urllib.parse.unquote(payload)
        return json.loads(raw)
    except Exception:
        return None


def resolve(uri: str | None, *, allow_network: bool = True,
            timeout: int = 12) -> tuple[dict | None, str]:
    """Return (metadata_dict_or_None, provenance).

    provenance is one of: on_chain, pinned, network, unavailable, absent.
    """
    if not uri:
        return None, "absent"
    uri = uri.strip()
    if is_on_chain(uri):
        return parse_data_uri(uri), "on_chain"

    pin = _pin_path(uri)
    if pin.exists():
        try:
            return json.loads(pin.read_text()), "pinned"
        except ValueError:
            pass
    if not allow_network:
        return None, "unavailable"

    urls = []
    if uri.startswith("ipfs://"):
        path = uri[7:]
        urls = [g + path for g in GATEWAYS]
    elif uri.startswith(("http://", "https://")):
        urls = [uri]
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"user-agent": "mintscout/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(1_000_000)
            data = json.loads(body.decode("utf8", "replace"))
            PIN_DIR.mkdir(parents=True, exist_ok=True)
            pin.write_text(json.dumps(data))
            return data, "network"
        except Exception:
            continue
    return None, "unavailable"


def summarize(meta: dict | None) -> dict:
    """Flatten metadata into the handful of signals the rubric actually uses.

    Two different documents show up behind these URIs and they are not
    interchangeable -- measured across 553 pinned files:

      * 546/553 are OpenSea DROP-STAGE config ({"stages": [...],
        "erc1155TokenMintDetails": ...}). No art, no traits. What it tells you is
        that the operator configured the drop through the standard OpenSea flow,
        and how many phases they set up.
      * 7/553 are true COLLECTION metadata (name/description/image/category),
        which come from contractURI().

    Treating the first kind as "metadata present, 0 attributes" would score a
    properly-configured drop as though it had empty metadata. They are
    distinguished explicitly instead.
    """
    if not meta:
        return {"present": False, "shape": None, "name": None, "description": None,
                "n_attributes": 0, "has_image": False, "token_bound_account": False,
                "n_stages": 0}

    # --- OpenSea drop-stage config -------------------------------------------
    if isinstance(meta.get("stages"), list):
        stages = [s for s in meta["stages"] if isinstance(s, dict)]
        names = [str(s.get("name") or "")[:40] for s in stages]
        return {
            "present": True,
            "shape": "drop_stage_config",
            "name": None, "description": None,
            "n_attributes": 0, "has_image": False, "image_is_on_chain": False,
            "token_bound_account": False,
            "n_stages": len(stages),
            "stage_names": names[:6],
            "has_allowlist_stage": any(
                ("allow" in n.lower() or "presale" in n.lower()) for n in names),
            "has_public_stage": any("public" in n.lower() for n in names),
            "note": "drop-stage configuration, not token art metadata",
        }
    attrs = meta.get("attributes") or []
    if not isinstance(attrs, list):
        attrs = []
    # ERC-6551 token-bound accounts show up as an explicit metadata field on the
    # reference collections; cheap and high-signal.
    tba = any(k for k in meta if "token_bound" in str(k).lower()) or \
        any(str(a.get("trait_type", "")).lower().startswith("token_bound")
            for a in attrs if isinstance(a, dict))
    return {
        "present": True,
        "shape": "collection_metadata",
        "n_stages": 0,
        "name": meta.get("name"),
        "description": (meta.get("description") or "")[:400] or None,
        "n_attributes": len(attrs),
        "attribute_types": [a.get("trait_type") for a in attrs
                            if isinstance(a, dict)][:12],
        "has_image": bool(meta.get("image") or meta.get("image_data")),
        "image_is_on_chain": str(meta.get("image", ""))[:5] == "data:"
                             or bool(meta.get("image_data")),
        "token_bound_account": bool(tba),
    }
