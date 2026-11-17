from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.core.config import settings

# 构造引擎参数：生产环境（PostgreSQL）保持默认。
# SQLite 分两类处理：
# - 内存库（测试用，URL 含 :memory:）：必须共享单连接（StaticPool），
#   否则每次连接都会拿到独立空库，测试数据互相隔离。
# - 文件库（生产本地库，如 sqlite+aiosqlite:///./novelcraft.db）：使用
#   默认连接池（每请求独立连接），并设置 busy timeout=30s。
#   若文件库也强制 StaticPool 单连接，前端「每批 5 章并行保存」时多个
#   AsyncSession 共享同一 DBAPI 连接，事务互相交错，autoflush 的 UPDATE
#   会因行状态被其他会话的 ROLLBACK/COMMIT 干扰而匹配 0 行，
#   抛 StaleDataError 并整体转成 HTTP 500（详见保存并发缺陷回归）。
_is_memory_sqlite = settings.database_url.startswith("sqlite") and ":memory:" in settings.database_url

_engine_kwargs = {
    "echo": settings.sql_echo,
    "pool_pre_ping": True,
}
if _is_memory_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    _engine_kwargs["poolclass"] = StaticPool
elif settings.database_url.startswith("sqlite"):
    # timeout=30 对应 sqlite3 busy timeout：并发写同一文件时最多等待 30 秒
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

def enable_sqlite_foreign_keys(target_engine) -> None:
    """文件 SQLite 引擎每条连接开启外键（SQLite 默认 OFF）。

    删除作品等依赖 ON DELETE CASCADE 的操作，在无外键时只会删主表一行、
    子表全部变成孤儿（2026-11-15 已发生 408 万字孤儿事故）。
    内存库保持默认 OFF（测试夹具依赖无外键语义，开启会造成大面积回归）。
    抽成公共函数以便测试引擎复用同一行为。
    """
    from sqlalchemy import event

    @event.listens_for(target_engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def enable_sqlite_wal_safety(target_engine) -> None:
    """文件 SQLite 引擎开启 WAL + synchronous=NORMAL（2026-11-17 批次 0）。

    实测教训：交接文档写「SQLite WAL」，代码实际未开（app 引擎实测
    journal_mode=delete）；busy_timeout 经 connect_args timeout=30 已生效（实测 30000），
    无需重复设置。
    - journal_mode=WAL：库级持久设置（一次生效）；写不锁读、读不锁写，
      AI 长生成（3~7 分钟）期间保存不再有锁库窗口
    - synchronous=NORMAL：WAL 下断电最多丢最近几笔已提交保存（库本体不损坏），
      换明显写性能；备份链路已用 SQLite backup API（读穿 WAL），不受伴生文件影响
    内存库不开启（WAL 对内存库无意义）。
    """
    from sqlalchemy import event

    @event.listens_for(target_engine.sync_engine, "connect")
    def _enable_sqlite_wal_safety(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


engine = create_async_engine(settings.database_url, **_engine_kwargs)

if settings.database_url.startswith("sqlite") and not _is_memory_sqlite:
    enable_sqlite_foreign_keys(engine)
    enable_sqlite_wal_safety(engine)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
