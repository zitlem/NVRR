import aiosqlite
import os

DB_PATH = os.environ.get("NVRR_DB_PATH", "/opt/nvrr/data/nvrr.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS nvrs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                ip TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 80,
                sdk_port INTEGER NOT NULL DEFAULT 8000,
                channels INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                cols INTEGER NOT NULL DEFAULT 4,
                cameras TEXT NOT NULL DEFAULT '[]',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nvr_id INTEGER NOT NULL,
                channel INTEGER NOT NULL,
                name TEXT NOT NULL,
                rtsp_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                ptz_enabled INTEGER NOT NULL DEFAULT 0,
                onvif_host TEXT,
                onvif_port INTEGER DEFAULT 80,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (nvr_id) REFERENCES nvrs(id) ON DELETE CASCADE,
                UNIQUE(nvr_id, channel)
            );
        """)
        # Migrate: add sdk_port column if missing
        cursor = await db.execute("PRAGMA table_info(nvrs)")
        cols = [row[1] for row in await cursor.fetchall()]
        if "sdk_port" not in cols:
            await db.execute("ALTER TABLE nvrs ADD COLUMN sdk_port INTEGER NOT NULL DEFAULT 8000")

        await db.commit()
    finally:
        await db.close()
