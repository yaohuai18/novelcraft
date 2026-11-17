"""部署演练编排：备份 → 副本迁移回放 → 恢复校验，一条命令跑完。

第三批 d 项。它就是 Runbook 里「副本最终演练」那一步的可执行版本，
避免每次靠人手敲三段脚本、再靠肉眼比对输出。

用法：
    python -m scripts.deploy_drill --source %DATABASE_URL%的路径 --out reports/drill
    python -m scripts.deploy_drill --db copy.db --skip-backup

安全边界：
- 只用副本：`--source` 存在时先做一致性备份，后续所有操作都在**备份文件**上执行；
  源库以 `mode=ro` 打开，备份前后校验 SHA-256 未变。
- 不改生产库、不启动服务、不发 HTTP 请求。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import consistent_backup  # noqa: E402


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NovelCraft 部署演练（备份 + 副本迁移 + 恢复校验）")
    parser.add_argument("--source", help="生产库路径（默认 DATABASE_URL）")
    parser.add_argument("--db", help="已有副本路径（配合 --skip-backup）")
    parser.add_argument("--out", default="reports/drill", help="输出目录")
    parser.add_argument("--skip-backup", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report: dict = {"started_at": stamp, "steps": []}

    if args.skip_backup:
        if not args.db:
            raise SystemExit("--skip-backup 需要 --db 指定副本")
        copy_path = Path(args.db)
        report["steps"].append({"step": "backup", "skipped": True, "path": str(copy_path)})
    else:
        source = args.source
        if not source:
            url = os.environ.get("DATABASE_URL", "")
            source = url.split("///")[-1] if "///" in url else ""
        if not source:
            raise SystemExit("未提供 --source，且 DATABASE_URL 不是 sqlite 文件路径")
        backup_path = out_dir / f"drill-{stamp}.db"
        backup = consistent_backup.backup_database(Path(source), backup_path)
        backup["restore_drill"] = consistent_backup.restore_drill(backup_path)
        report["steps"].append({"step": "backup", **backup})
        copy_path = backup_path

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    code, output = _run(
        [sys.executable, "-m", "scripts.verify_migration", "--db", str(copy_path)]
    )
    report["steps"].append(
        {
            "step": "verify_migration",
            "exit_code": code,
            "ok": code == 0,
            "output_tail": output[-4000:],
        }
    )

    recovered = consistent_backup.restore_drill(Path(copy_path))
    report["steps"].append({"step": "restore_drill_after_migration", **recovered})

    ok = (
        report["steps"][0].get("source_unchanged", True)
        and report["steps"][1]["ok"]
        and recovered["ok"]
    )
    report["ok"] = bool(ok)
    report_path = out_dir / f"deploy-drill-{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(report_path)}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
