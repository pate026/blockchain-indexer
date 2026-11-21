import pytest

from reorg import ReorgError, find_common_ancestor


def make_chain(start: int, end: int, tag: str) -> dict[int, str]:
    # fake chain: block number -> hash, tagged so forks get different hashes
    return {n: f"0x{tag}{n}" for n in range(start, end + 1)}


def fork(stored: dict[int, str], fork_block: int) -> dict[int, str]:
    # canonical chain that matches stored below fork_block and differs from it on
    chain = dict(stored)
    for n in stored:
        if n >= fork_block:
            chain[n] = f"0xfork{n}"
    return chain


async def run(stored: dict[int, str], chain: dict[int, str], start: int, max_depth: int) -> int:
    async def get_chain_hash(n: int) -> str:
        return chain[n]

    async def get_stored_hash(n: int) -> str | None:
        return stored.get(n)

    return await find_common_ancestor(start, get_chain_hash, get_stored_hash, max_depth)


async def test_no_reorg_returns_start_block():
    stored = make_chain(100, 110, "a")
    assert await run(stored, dict(stored), 110, 64) == 110


async def test_one_block_reorg():
    stored = make_chain(100, 110, "a")
    assert await run(stored, fork(stored, 110), 110, 64) == 109


async def test_three_block_reorg():
    stored = make_chain(100, 110, "a")
    assert await run(stored, fork(stored, 108), 110, 64) == 107


async def test_reorg_at_exactly_max_depth_is_allowed():
    stored = make_chain(100, 110, "a")
    assert await run(stored, fork(stored, 108), 110, 3) == 107


async def test_reorg_deeper_than_max_depth_raises():
    stored = make_chain(100, 110, "a")
    with pytest.raises(ReorgError, match="deeper than 2"):
        await run(stored, fork(stored, 108), 110, 2)


async def test_missing_stored_history_raises():
    stored = make_chain(105, 110, "a")
    chain = fork(make_chain(100, 110, "a"), 103)
    with pytest.raises(ReorgError, match="no stored hash"):
        await run(stored, chain, 110, 64)
