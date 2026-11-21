import asyncio

import aiosqlite
import pytest

import db

HASH_A = "0x" + "aa" * 32
HASH_B = "0x" + "bb" * 32
HASH_C = "0x" + "cc" * 32


def transfer(tx: str, block: int, amount: int, log_index: int = 0) -> db.TransferRow:
    # build a transfer row with dummy addresses
    return (tx, log_index, block, "0xfrom", "0xto", str(amount))


@pytest.fixture(autouse=True)
async def temp_db(tmp_path, monkeypatch):
    # point the storage layer at a fresh database file for every test
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    await db.init_db()


async def test_save_batch_sets_checkpoint_to_highest_block():
    await db.save_batch(
        [transfer("0x01", 100, 5), transfer("0x02", 102, 7)],
        [(100, HASH_A), (102, HASH_C), (101, HASH_B)],
    )

    assert await db.get_checkpoint() == (102, HASH_C)
    assert await db.get_block_hash(100) == HASH_A
    assert await db.get_block_hash(101) == HASH_B
    assert await db.get_block_hash(999) is None

    last_block, count, volume, rows = await db.get_dashboard_data()
    assert (last_block, count, volume) == (102, 2, 12)
    assert rows[0] == (102, "0xfrom", "0xto", 7, "0x02")


async def test_save_batch_requires_block_hashes():
    with pytest.raises(ValueError):
        await db.save_batch([], [])


async def test_duplicate_inserts_are_ignored():
    rows = [transfer("0x01", 100, 5), transfer("0x01", 100, 5, log_index=1)]
    await db.save_batch(rows, [(100, HASH_A)])
    await db.save_batch(rows, [(100, HASH_A)])

    _, count, volume, _ = await db.get_dashboard_data()
    assert count == 2
    assert volume == 10


async def test_rollback_to_deletes_rows_and_moves_checkpoint():
    await db.save_batch(
        [transfer("0x01", 100, 1), transfer("0x02", 101, 2), transfer("0x03", 102, 3)],
        [(100, HASH_A), (101, HASH_B), (102, HASH_C)],
    )

    await db.rollback_to(101)

    assert await db.get_checkpoint() == (100, HASH_A)
    assert await db.get_block_hash(100) == HASH_A
    assert await db.get_block_hash(101) is None
    assert await db.get_block_hash(102) is None

    last_block, count, volume, rows = await db.get_dashboard_data()
    assert (last_block, count, volume) == (100, 1, 1)
    assert [r[0] for r in rows] == [100]


async def test_rollback_to_clears_checkpoint_without_stored_parent():
    await db.save_batch([transfer("0x01", 100, 1)], [(100, HASH_A)])

    await db.rollback_to(100)

    assert await db.get_checkpoint() is None
    _, count, volume, _ = await db.get_dashboard_data()
    assert (count, volume) == (0, 0)


async def test_dashboard_volume_is_exact_int_for_large_values():
    big = 10**30
    await db.save_batch(
        [transfer("0x01", 100, big), transfer("0x02", 100, 1)],
        [(100, HASH_A)],
    )

    _, _, volume, rows = await db.get_dashboard_data()
    assert isinstance(volume, int)
    assert volume == big + 1
    assert sorted(r[3] for r in rows) == [1, big]


async def recompute_stats() -> tuple[int, int]:
    # recompute count and volume straight from the transfers table
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute("SELECT amount_raw FROM transfers;") as cursor:
            amounts = [int(r[0]) for r in await cursor.fetchall()]
    return len(amounts), sum(amounts)


async def dashboard_stats() -> tuple[int, int]:
    # read count and volume as reported by the dashboard
    _, count, volume, _ = await db.get_dashboard_data()
    return count, volume


async def test_stats_match_transfers_after_several_batches():
    await db.save_batch([transfer("0x01", 100, 5)], [(100, HASH_A)])
    await db.save_batch(
        [transfer("0x02", 101, 7), transfer("0x03", 101, 11)], [(101, HASH_B)]
    )
    await db.save_batch([], [(102, HASH_C)])

    assert await dashboard_stats() == await recompute_stats() == (3, 23)


async def test_duplicates_in_later_batch_do_not_change_stats():
    await db.save_batch(
        [transfer("0x01", 100, 5), transfer("0x02", 100, 7)], [(100, HASH_A)]
    )
    await db.save_batch(
        [transfer("0x01", 100, 5), transfer("0x03", 101, 3)], [(101, HASH_B)]
    )

    assert await dashboard_stats() == await recompute_stats() == (3, 15)


async def test_stats_match_transfers_after_rollback():
    await db.save_batch(
        [transfer("0x01", 100, 1), transfer("0x02", 101, 2), transfer("0x03", 102, 3)],
        [(100, HASH_A), (101, HASH_B), (102, HASH_C)],
    )

    await db.rollback_to(101)

    assert await dashboard_stats() == await recompute_stats() == (1, 1)


async def test_init_db_backfills_missing_stats_row():
    big = 10**30
    await db.save_batch(
        [transfer("0x01", 100, big), transfer("0x02", 101, 4)],
        [(100, HASH_A), (101, HASH_B)],
    )
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("DELETE FROM stats;")
        await conn.commit()
    assert await dashboard_stats() == (0, 0)

    await db.init_db()

    assert await dashboard_stats() == await recompute_stats() == (2, big + 4)


async def test_concurrent_init_db_backfills_once():
    await db.save_batch([transfer("0x01", 100, 9)], [(100, HASH_A)])
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("DELETE FROM stats;")
        await conn.commit()

    await asyncio.gather(db.init_db(), db.init_db())

    assert await dashboard_stats() == await recompute_stats() == (1, 9)


async def test_large_volume_stays_exact_in_stats():
    big = 10**30
    await db.save_batch(
        [transfer("0x01", 100, big), transfer("0x02", 100, big)], [(100, HASH_A)]
    )

    count, volume = await dashboard_stats()
    assert isinstance(volume, int)
    assert (count, volume) == (2, 2 * 10**30)
    assert (count, volume) == await recompute_stats()
