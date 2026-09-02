#!/usr/bin/env python3
"""Pre-commit scrubber: refuse to commit mnemonics, private keys or .env files.

Install:  ln -sf ../../scripts/scrub_secrets.py .git/hooks/pre-commit
"""
import re, subprocess, sys, pathlib

HEX_KEY = re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b")
# 12/15/18/21/24 lowercase words in a row is the shape of a BIP-39 mnemonic
MNEMONIC = re.compile(r"\b(?:[a-z]{3,8}\s+){11,23}[a-z]{3,8}\b")
ALLOW = ("data/fixtures/", "data/drops_", "CHANGELOG", "README", "REPRODUCE",
         "trajectories/", "results/", "data/llm_cache/")

def staged() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only"],
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l.strip()]

def main() -> int:
    bad = []
    for f in staged():
        if f.startswith(".env") and f != ".env.example":
            bad.append((f, "0", ".env file staged"))
            continue
        p = pathlib.Path(f)
        if not p.exists() or any(f.startswith(a) for a in ALLOW):
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "MINT_SEED=" in line and line.split("=", 1)[1].strip():
                bad.append((f, str(i), "MINT_SEED has a value"))
            for var in ("OPENSEA_API_KEY", "ANTHROPIC_API_KEY", "TWITTERAPI_IO_KEY"):
                if f"{var}=" in line and line.split("=", 1)[1].strip():
                    bad.append((f, str(i), f"{var} has a value"))
            if MNEMONIC.search(line.lower()) and "seed" in line.lower():
                bad.append((f, str(i), "looks like a BIP-39 mnemonic"))
            for m in HEX_KEY.finditer(line):
                # topic hashes / tx hashes are 64-hex too; only flag near key words
                if re.search(r"(priv|secret|key|seed)", line, re.I):
                    bad.append((f, str(i), "looks like a private key"))
                    break
    if bad:
        print("BLOCKED: potential secret material staged for commit\n")
        for f, ln, why in bad:
            print(f"  {f}:{ln}  {why}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
