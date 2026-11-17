# 软删除回收站（2026-11-15 第一档升级）：
# 1) 文件 SQLite 引擎必须开启外键（PRAGMA foreign_keys=ON）；
# 2) 删除作品 → 进回收站（列表不可见、trash 可见、restore 可恢复）；
# 3) purge 彻底删除 → 外键级联清理子表（不再产生孤儿）。
import pathlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base, enable_sqlite_foreign_keys
from app.models.project import Project
from app.services import project_service

pytestmark = pytest.mark.asyncio


async def _make_file_engine(tmp_path: pathlib.Path, name: str = "recycle.db"):
    db_file = tmp_path / f"{name}-{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    enable_sqlite_foreign_keys(engine)  # 与正式引擎同一行为路径
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    return factory, engine, str(db_file)


async def test_file_engine_enables_foreign_keys(tmp_path):
    """文件库每条连接必须 PRAGMA foreign_keys=ON（内存库除外）。"""
    _factory, engine, _db_file = await _make_file_engine(tmp_path)
    try:
        async with engine.connect() as conn:
            fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        assert fk == 1, f"文件库外键未开启：{fk}"
    finally:
        await engine.dispose()


async def test_delete_project_moves_to_trash_and_restore(tmp_path):
    """删除 → 回收站可见、常规列表不可见；restore 后恢复可见。"""
    from app.models.user import User

    factory, engine, _db_file = await _make_file_engine(tmp_path)
    try:
        user_id = uuid.uuid4()
        async with factory() as db:
            # 外键开启后 projects.user_id 必须指向真实用户
            db.add(User(
                id=user_id,
                username=f"u_{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@test.com",
                password_hash="x",
            ))
            await db.flush()
            project = Project(user_id=user_id, title="回收站测试作品")
            db.add(project)
            await db.commit()
            await db.refresh(project)
            pid = project.id

            await project_service.delete_project(db, user_id, pid)

            # 常规列表不可见
            visible = await project_service.list_projects(db, user_id)
            assert all(p.id != pid for p in visible), "软删除后仍出现在常规列表"
            # 回收站可见
            trash = await project_service.list_trash_projects(db, user_id)
            assert any(p.id == pid for p in trash), "回收站列表缺失软删除作品"
            # 详情 404
            with pytest.raises(Exception):
                await project_service.get_project(db, user_id, pid)
            # 恢复
            restored = await project_service.restore_project(db, user_id, pid)
            assert restored.deleted_at is None
            visible_after = await project_service.list_projects(db, user_id)
            assert any(p.id == pid for p in visible_after), "恢复后未出现在常规列表"
    finally:
        await engine.dispose()


async def test_purge_project_cascades_to_children(tmp_path):
    """彻底删除后子表（outlines）被外键级联清空，不再产生孤儿。"""
    from app.models.outline import Outline
    from app.models.user import User

    factory, engine, _db_file = await _make_file_engine(tmp_path)
    try:
        user_id = uuid.uuid4()
        async with factory() as db:
            db.add(User(
                id=user_id,
                username=f"u_{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@test.com",
                password_hash="x",
            ))
            await db.flush()
            project = Project(user_id=user_id, title="级联删除测试作品")
            db.add(project)
            await db.commit()
            await db.refresh(project)
            pid = project.id
            db.add(Outline(
                project_id=pid,
                title="第一章 测试",
                outline_type="chapter",
                sort_order=1,
            ))
            await db.commit()

            # 先软删
            await project_service.delete_project(db, user_id, pid)
            # 再彻底删除
            await project_service.purge_project(db, user_id, pid)

            orphan_outlines = (
                await db.execute(
                    text("SELECT COUNT(*) FROM outlines WHERE project_id = :pid"),
                    {"pid": str(pid)},
                )
            ).scalar()
            assert orphan_outlines == 0, f"purge 后 outlines 残留 {orphan_outlines} 行（外键未级联）"

            # before_delete 快照已落独立文件（project_backups 有级联外键，挂库会被真删除连带删掉）
            from pathlib import Path

            # 本测试文件在 backend/tests/ 下：parents[1]=backend，快照目录 backend/trash_snapshots
            snap_dir = Path(__file__).resolve().parents[1] / "trash_snapshots"
            snap_files = list(snap_dir.glob(f"before_delete_{pid}_*.json"))
            assert len(snap_files) >= 1, "purge 前未落 before_delete 文件快照"
    finally:
        await engine.dispose()
