# -*- coding: utf-8 -*-
"""generation_records 归属状态回填脚本（管理入口）。

用法（在 backend 目录下执行）：
  python scripts/backfill_generation_attribution.py --dry-run        # 只统计，不写库
  python scripts/backfill_generation_attribution.py                  # 实际回填
  python scripts/backfill_generation_attribution.py --limit 1000     # 分批处理

规则与统计口径见 app/services/generation_attribution.py 的模块说明。
脚本可重复执行：已归属的行不会被再次改写。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.services.generation_attribution import (  # noqa: E402
    backfill_generation_attribution,
)


async def _run(dry_run: bool, limit: int | None) -> dict:
    async with AsyncSessionLocal() as db:
        return await backfill_generation_attribution(
            db, dry_run=dry_run, limit=limit
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="回填 generation_records.attribution_status"
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    parser.add_argument("--limit", type=int, default=None, help="本批最多处理多少行")
    args = parser.parse_args()

    stats = asyncio.run(_run(args.dry_run, args.limit))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("\n[dry-run] 未写入任何数据。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
