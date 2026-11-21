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
