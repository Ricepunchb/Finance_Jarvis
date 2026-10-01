import asyncio

import aiosqlite

from core import db


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    await db._migrate_add_missing_columns(conn)
    return conn


def test_recently_removed_symbols_excludes_active_and_re_added():
    async def run():
        conn = await _conn()
        for s in ("035900", "005930", "000660"):
            await db.add_portfolio_symbol(conn, s)
        await db.finish_symbol_removal(conn, "035900")
        await db.finish_symbol_removal(conn, "000660")
        assert set(await db.list_recently_removed_symbols(conn, 30)) == {"035900", "000660"}

        await db.add_portfolio_symbol(conn, "000660")  # 다시 담으면 쿨다운 대상에서 빠진다
        assert await db.list_recently_removed_symbols(conn, 30) == ["035900"]

        await conn.execute("UPDATE portfolio_symbols SET disabled_at = disabled_at - 40 * 86400 WHERE symbol = '035900'")
        assert await db.list_recently_removed_symbols(conn, 30) == []
    asyncio.run(run())
