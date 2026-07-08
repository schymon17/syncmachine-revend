from __future__ import annotations

import argparse
import random
import string
import time
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, load_config, save_config
from .db import Database


EAN_POOL = [
    "5902073707402",
    "5449000335821",
    "5000112651324",
    "5901234123457",
    "5900000000017",
]


@dataclass
class SeedResult:
    transactions: int = 0
    rows: int = 0
    bins: int = 0
    status_rows: int = 0


def random_code(prefix: str, length: int = 10) -> str:
    chars = string.digits
    return prefix + "".join(random.choice(chars) for _ in range(length))


class DevSeeder:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.db = Database(cfg.database)

    def seed_transactions(self, count: int = 1, bottles_min: int = 1, bottles_max: int = 5) -> SeedResult:
        result = SeedResult()
        now = int(time.time())
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                for tx_index in range(count):
                    coupon = random_code("DEV", 12)
                    transaction_id = random_code("tx", 10)
                    bottle_count = random.randint(bottles_min, bottles_max)
                    for bottle_index in range(bottle_count):
                        dateline = now + tx_index * 10 + bottle_index
                        cur.execute(
                            """
                            INSERT INTO user_transaction
                            (transactionid, user, dateline, statecode, barcode, weight, diam, metal,
                             print_barcode, recognitionstatus, rebateordonate, payplatform, bottlevalue,
                             charityname, octreceipt, transactiondone, uploaddone)
                            VALUES
                            (%s, 'unknown', %s, '0', %s, %s, '0', %s,
                             %s, '1', '0', 'RVM', '6',
                             '0', '0', 2, 0)
                            """,
                            (
                                transaction_id,
                                dateline,
                                random.choice(EAN_POOL),
                                str(random.randint(20, 70)),
                                str(random.randint(0, 1)),
                                coupon,
                            ),
                        )
                        result.rows += 1
                    result.transactions += 1
            conn.commit()
        return result

    def seed_bins(self, count: int = 1) -> SeedResult:
        result = SeedResult()
        now = int(time.time())
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                for index in range(count):
                    cur.execute(
                        """
                        INSERT INTO empty_record (mid, dateline, bin_type, barcode)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            self.cfg.machine_id,
                            now + index,
                            random.choice(["PET", "CAN", "MIX"]),
                            random_code("BAG", 10),
                        ),
                    )
                    result.bins += 1
            conn.commit()
        return result

    def seed_status(self) -> SeedResult:
        storage = random.randint(5, 95)
        storageplastic = random.randint(5, 95)
        storagecan = random.randint(5, 95)
        errorcode = random.choice(["0", "0", "0", "E01", "E02"])
        with self.db.connect() as conn:
            Database.execute(
                conn,
                """
                INSERT INTO command (storage, storageplastic, storagecan, errorcode)
                VALUES (%s, %s, %s, %s)
                """,
                (storage, storageplastic, storagecan, errorcode),
            )
            conn.commit()
        return SeedResult(status_rows=1)

    def seed_all(self, transactions: int = 3, bins: int = 2, status: bool = True) -> SeedResult:
        total = SeedResult()
        tx = self.seed_transactions(transactions)
        total.transactions += tx.transactions
        total.rows += tx.rows
        bin_result = self.seed_bins(bins)
        total.bins += bin_result.bins
        if status:
            status_result = self.seed_status()
            total.status_rows += status_result.status_rows
        return total


def apply_dev_defaults(cfg: AppConfig) -> AppConfig:
    cfg.config_done = True
    cfg.machine_id = cfg.machine_id or "DEV_MACHINE_001"
    cfg.integration = cfg.integration or "dev"
    cfg.address = cfg.address or "Docker dev machine"
    cfg.database.host = "127.0.0.1"
    cfg.database.port = 3306
    cfg.database.database = "qcs"
    cfg.database.username = "root"
    cfg.database.password = "chushengfeng123"
    cfg.sync.enabled_eans = False
    cfg.sync.enabled_coupons = False
    cfg.sync.enabled_adverts = False
    cfg.sync.install_triggers = True
    return cfg


def result_to_text(result: SeedResult) -> str:
    return (
        f"transactions={result.transactions}, rows={result.rows}, "
        f"bins={result.bins}, status_rows={result.status_rows}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate fake machine DB data for sync-new-machine.")
    parser.add_argument("--dev-config", action="store_true", help="Save local config for Docker machine DB.")
    parser.add_argument("--transactions", type=int, default=3)
    parser.add_argument("--bins", type=int, default=2)
    parser.add_argument("--no-status", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.dev_config:
        cfg = apply_dev_defaults(cfg)
        save_config(cfg)
        print("Saved Docker dev DB config.")

    result = DevSeeder(cfg).seed_all(args.transactions, args.bins, not args.no_status)
    print(result_to_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
