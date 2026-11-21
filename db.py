import aiosqlite

# SQLite database file path
DB_PATH = "indexer.db"

# transfer row: (tx_hash, log_index, block_number, from_address, to_address, amount_raw)
TransferRow = tuple[str, int, int, str, str, str]


async def init_db() -> None:
    # initialize database tables and enable WAL mode for concurrent reads/writes
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")

        # refuse to run on the old float schema: those amounts cannot be recovered exactly
        async with db.execute("PRAGMA table_info(transfers);") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        if "amount" in columns and "amount_raw" not in columns:
            raise RuntimeError(
                f"{DB_PATH} uses the old float 'amount' schema. "
                "delete it (and its -wal/-shm files) and re-index."
            )

        # store decoded USDC transfer events (unique constraint prevents duplicates)
        # amount_raw is the raw uint256 value as a decimal string, never a float
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                from_address TEXT NOT NULL,
                to_address TEXT NOT NULL,
                amount_raw TEXT NOT NULL,
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

        # store the hash of every indexed block for reorg detection
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS blocks (
                block_number INTEGER PRIMARY KEY,
                block_hash TEXT NOT NULL
            );
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


async def get_block_hash(block_number: int) -> str | None:
    # fetch the stored hash of one indexed block if available
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT block_hash FROM blocks WHERE block_number = ?;",
            (block_number,),
        ) as cursor:
            row = await cursor.fetchone()
            return str(row[0]) if row else None


async def save_batch(
    transfers: list[TransferRow],
    block_hashes: list[tuple[int, str]],
) -> None:
    # atomically save batch events, block hashes and the checkpoint in one transaction
    if not block_hashes:
        raise ValueError("save_batch needs at least one block hash")
    last_block, last_hash = max(block_hashes, key=lambda item: item[0])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("BEGIN;")
        await db.executemany(
            """
            INSERT OR IGNORE INTO transfers
            (tx_hash, log_index, block_number, from_address, to_address, amount_raw)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            transfers,
        )
        await db.executemany(
            "INSERT OR REPLACE INTO blocks (block_number, block_hash) VALUES (?, ?);",
            block_hashes,
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


async def rollback_to(fork_block: int) -> None:
    # atomically drop all data from fork_block upwards and move the checkpoint back
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("BEGIN;")
        await db.execute(
            "DELETE FROM transfers WHERE block_number >= ?;",
            (fork_block,),
        )
        await db.execute(
            "DELETE FROM blocks WHERE block_number >= ?;",
            (fork_block,),
        )

        # point the checkpoint at the last surviving block, or clear it if unknown
        async with db.execute(
            "SELECT block_hash FROM blocks WHERE block_number = ?;",
            (fork_block - 1,),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db.execute(
                """
                INSERT INTO checkpoints (id, last_block, last_hash)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_block = excluded.last_block,
                    last_hash = excluded.last_hash;
                """,
                (fork_block - 1, str(row[0])),
            )
        else:
            await db.execute("DELETE FROM checkpoints WHERE id = 1;")
        await db.commit()


async def get_dashboard_data(
    limit: int = 25,
) -> tuple[int, int, int, list[tuple[int, str, str, int, str]]]:
    # read summary statistics and latest transfers for the TUI without blocking writes
    # all amounts are returned as exact ints in raw token units
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")

        # fetch latest indexed block from checkpoints
        async with db.execute(
            "SELECT last_block FROM checkpoints WHERE id = 1;"
        ) as cursor:
            cp_row = await cursor.fetchone()
            last_block = int(cp_row[0]) if cp_row else 0

        # sum volume in python with exact ints (SQL SUM would overflow or use floats)
        total_count = 0
        total_volume = 0
        async with db.execute("SELECT amount_raw FROM transfers;") as cursor:
            async for (amount_raw,) in cursor:
                total_count += 1
                total_volume += int(amount_raw)

        # fetch the most recent transfers ordered by newest first
        async with db.execute(
            """
            SELECT block_number, from_address, to_address, amount_raw, tx_hash
            FROM transfers
            ORDER BY block_number DESC, id DESC
            LIMIT ?;
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            recent_transfers = [
                (int(r[0]), str(r[1]), str(r[2]), int(r[3]), str(r[4]))
                for r in rows
            ]

        return last_block, total_count, total_volume, recent_transfers
