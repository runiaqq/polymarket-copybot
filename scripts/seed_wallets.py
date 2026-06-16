"""
Seed the tracked_wallets whitelist with an initial set of profitable wallets.
Run AFTER migration 007. Idempotent (re-running just re-activates them).

    python scripts/seed_wallets.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import add_tracked_wallet

# Top profitable + actively-trading wallets (from scripts/find_wallets.py, 7d & 30d leaders).
WALLETS = [
    "0x204f72f35326db932158cba6adff0b9a1da95e14",
    "0xf0318c32136c2db7fec88b84869aee6a1106c80c",
    "0xbaa2bcb5439e985ce4ccf815b4700027d1b92c73",
    "0x4bff30af91642dc7d2b19a8664378fe55c45fc26",
    "0x96cfcb0c30942cfcd1cdf76c7d408794d66b1acb",
    "0x8cb4ca5af7d9361322340bb307a828d288c91057",
    "0xed64a7bf029040aa331abc87902434d815ef217d",
    "0xc41d736bded9ed1accd6a44235039266219774fd",
    "0xfd22b8843ae03a33a8a4c5e39ef1e5ff33ebad91",
    "0xd1c537b2a7cba8d365e111bffb9de7b205e2cbd0",
    "0x4761ecf3578e388a9b16c43f874efe32ee855ae8",
    "0x162f6fff88a52864f2ecc9833e58089d5254798d",
    "0xc84f7e76ec28ef20e7773b7b4926bfb7378be0c5",
    "0xc660ae71765d0d9eaf5fa8328c1c959841d2bd28",
    "0x1b47e9b128e6b671edebfb2cac23dd3efc40d814",
    "0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7",
    "0x5e4c3b5b81171e2ca4ab776ac0d6bba787f9dba2",
    "0x408fe71e6b5401ecd6733970cb6f1a25e984b2f4",
]


def main() -> None:
    for w in WALLETS:
        try:
            add_tracked_wallet(w, label="seed")
            print("added", w)
        except Exception as e:
            print("FAILED", w, e)
    print(f"\nDone — {len(WALLETS)} wallets seeded.")


if __name__ == "__main__":
    main()
