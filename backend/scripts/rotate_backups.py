"""备份轮转（2026-11-15 第一档升级）：把 backend 下的历史备份移出到集中目录并按保留策略清理。

用法：
    python -m scripts.rotate_backups                    # dry-run：只打印计划
    python -m scripts.rotate_backups --apply            # 执行移动与清理
    python -m scripts.rotate_backups --backup-dir D:/x  # 自定义集中目录

策略：
- backend/ 目录只保留最新的 2 份备份（含 -shm/-wal sidecar 一起处理）；
- 集中备份目录按文件修改时间保留：最近 7 天全部 + 之后每 7 天保留 1 份（最多 4 周）；
- 更早的备份打印删除计划；--apply 时真正删除。
"""

from __future__ import annotations

import argparse
import os
import shutil
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = Path.home() / "NovelCraft-backups" / "database"


def _is_backup_file(name: str) -> bool:
    return ".bak-" in name


def _group_key(path: Path) -> str:
    """把 xxx.db.bak-20261011-222756 / xxx.db.bak-20261011-222756-wal 归到同一备份组。"""
    name = path.name
    for suffix in ("-shm", "-wal"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    parser.add_argument("--backup-dir", type=str, default=str(DEFAULT_BACKUP_DIR))
    args = parser.parse_args()
    backup_dir = Path(args.backup_dir)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[rotate] {mode} backend={BACKEND_DIR} backup_dir={backup_dir}")

    # 1. backend 下的备份文件按组排序（新→旧）
    local = [p for p in BACKEND_DIR.iterdir() if p.is_file() and _is_backup_file(p.name)]
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in local:
        groups[_group_key(p)].append(p)
    group_times = sorted(
        ((max(_mtime(p) for p in ps), key) for key, ps in groups.items()),
        reverse=True,
    )
    keep_local = {key for _t, key in group_times[:2]}
    print(f"[rotate] backend 下共 {len(groups)} 组备份，保留最新 2 组：{sorted(keep_local)}")

    # 2. 移出（除保留的 2 组外全部移入集中目录）
    moved = 0
    for key, paths in groups.items():
        if key in keep_local:
            continue
        for p in paths:
            dest = backup_dir / p.name
            if dest.exists() and dest.stat().st_size == p.stat().st_size:
                if args.apply:
                    p.unlink()
                print(f"[rotate] 已存在同名同大小，删除本地副本: {p.name}")
                moved += 1
                continue
            if args.apply:
                backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dest))
            print(f"[rotate] 移出: {p.name} -> {dest}")
            moved += 1
    print(f"[rotate] 移出/清理 {moved} 个文件")

    # 3. 集中目录保留策略：7 天全留；之后每周留 1 份；更早删除
    if not backup_dir.exists():
        print("[rotate] 集中目录不存在，跳过清理")
        return
    remote = [p for p in backup_dir.iterdir() if p.is_file() and _is_backup_file(p.name)]
    remote_groups: dict[str, list[Path]] = defaultdict(list)
    for p in remote:
        remote_groups[_group_key(p)].append(p)
    now = datetime.now()
    keep_set: set[str] = set()
    remove_set: set[str] = set()
    for key, paths in remote_groups.items():
        t = max(_mtime(p) for p in paths)
        if now - t <= timedelta(days=7):
            keep_set.add(key)
            continue
        # 每周窗口：按 ISO 周编号，同一周只留最新一份
        week = t.isocalendar()[:2]
        for other_key, other_paths in remote_groups.items():
            if other_key == key:
                continue
            ot = max(_mtime(p) for p in other_paths)
            if ot.isocalendar()[:2] == week and now - ot > timedelta(days=7):
                if ot >= t:
                    remove_set.add(key)
                    break
        else:
            keep_set.add(key)
    print(f"[rotate] 集中目录 {len(remote_groups)} 组：保留 {len(keep_set)} 组")
    for key in sorted(remove_set):
        for p in remote_groups[key]:
            if args.apply:
                p.unlink(missing_ok=True)
            print(f"[rotate] 删除过期备份: {p.name}")
    print("[rotate] 完成")


if __name__ == "__main__":
    main()
