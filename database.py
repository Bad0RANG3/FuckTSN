from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool
from loguru import logger
import os

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite+aiosqlite:///./tsn_data.db')
engine = create_async_engine(DATABASE_URL, echo=False, poolclass=NullPool, future=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autocommit=False, autoflush=False)
Base = declarative_base()


async def get_db():
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def _migrate(conn) -> None:
    """轻量迁移：只处理「老库缺列」，不引入 alembic。

    SQLite 的 ALTER TABLE ADD COLUMN 是廉价的；其它数据库请自行迁移。
    """
    if conn.dialect.name != 'sqlite':
        return
    columns = {row[1] for row in conn.exec_driver_sql('PRAGMA table_info(tsn_account)').fetchall()}
    if columns and 'token_updated_at' not in columns:
        conn.exec_driver_sql('ALTER TABLE tsn_account ADD COLUMN token_updated_at DATETIME')
        logger.info('数据库迁移：tsn_account 增加 token_updated_at')


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)


async def close_db():
    await engine.dispose()
