"""Constants are derived, never trusted."""
from eth_utils import keccak

from mintscout import constants as C


def test_selectors_are_derived_not_literal():
    assert C.SEL_MINT_PUBLIC == "0x" + keccak(text=C.SIG_MINT_PUBLIC).hex().replace("0x", "")[:8]
    assert C.SEL_MINT_PUBLIC == "0x161ac21f"
    assert C.SEL_GET_PUBLIC_DROP == "0xbc6a629c"


def test_topics_match_committed_fixture():
    import json, pathlib
    fx = json.loads((pathlib.Path(__file__).resolve().parents[1]
                     / "data/fixtures/topics.json").read_text())
    for name, ent in fx["events"].items():
        assert C.topic(ent["signature"]) == ent["topic0"], name
        # and against topic0 of a real captured log
        assert C.topic(ent["signature"]) == ent["sample"]["topics"][0], name


def test_seadrop_immutable_ranges_cover_the_diff():
    # 2-byte chain id + 32-byte domain separator = 34 bytes, the exact number
    # of bytes measured to differ between the two chains' deployments.
    total = sum(hi - lo for lo, hi in C.SEADROP_IMMUTABLE_RANGES)
    assert total == 34
