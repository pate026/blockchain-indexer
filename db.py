import aiosqlite

# SQLite database file path
DB_PATH = "indexer.db"


async def init_db() -> None:
    # initialize database tables and enable WAL mode for concurrent reads/writes
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")

        # store decoded USDC transfer events (unique constraint prevents duplicates)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                from_address TEXT NOT NULL,
                to_address TEXT NOT NULL,
                amount REAL NOT NULL,
                UNIQUE(tx_hash, log_index)
            );
            """
        )

        # index block_number for fast queries and reorg rollbacks
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_transfers_block
            ON transfers(block_number DESC);
            """
        )

        # store the latest indexed block number and hash for crash recovery
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_block INTEGER NOT NULL,
                last_hash TEXT NOT NULL
            );
            """
        )
        await db.commit()


async def get_checkpoint() -> tuple[int, str] | None:
    # fetch the last saved block number and hash if available
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT last_block, last_hash FROM checkpoints WHERE id = 1;"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return int(row[0]), str(row[1])
            return None


async def save_batch(
    transfers: list[tuple[str, int, int, str, str, float]],
    last_block: int,
    last_hash: str,
) -> None:
    # atomically save all batch events and update the checkpoint in one transaction
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executemany(
            """
            INSERT OR IGNORE INTO transfers
            (tx_hash, log_index, block_number, from_address, to_address, amount)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            transfers,
        )
        await db.execute(
            """
            INSERT INTO checkpoints (id, last_block, last_hash)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_block = excluded.last_block,
                last_hash = excluded.last_hash;
            """,
            (last_block, last_hash),
        )
        await db.commit()


async def rollback_block(invalid_block: int) -> None:
    # delete orphaned events if a chain reorg occurs
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM transfers WHERE block_number >= ?;",
            (invalid_block,),
        )
        await db.commit()


async def get_dashboard_data(
    limit: int = 25,
) -> tuple[int, int, float, list[tuple[int, str, str, float, str]]]:
    # read summary statistics and latest transfers for the TUI without blocking writes
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")

        # fetch latest indexed block from checkpoints
        async with db.execute(
            "SELECT last_block FROM checkpoints WHERE id = 1;"
        ) as cursor:
            cp_row = await cursor.fetchone()
            last_block = int(cp_row[0]) if cp_row else 0

        # fetch total transfer count and total USDC volume
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0.0) FROM transfers;"
        ) as cursor:
            stats_row = await cursor.fetchone()
            total_count = int(stats_row[0]) if stats_row else 0
            total_volume = float(stats_row[1]) if stats_row else 0.0

        # fetch the most recent transfers ordered by newest first
        async with db.execute(
            """
            SELECT block_number, from_address, to_address, amount, tx_hash
            FROM transfers
            ORDER BY block_number DESC, id DESC
            LIMIT ?;
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            recent_transfers = [
                (int(r[0]), str(r[1]), str(r[2]), float(r[3]), str(r[4]))
                for r in rows
            ]

        return last_block, total_count, total_volume, recent_transfers