from collections.abc import Awaitable, Callable


class ReorgError(Exception):
    # raised when no common ancestor can be found within the allowed depth
    pass


async def find_common_ancestor(
    start_block: int,
    get_chain_hash: Callable[[int], Awaitable[str]],
    get_stored_hash: Callable[[int], Awaitable[str | None]],
    max_depth: int,
) -> int:
    # walk backwards from start_block until the stored hash matches the canonical chain.
    # a reorg of up to max_depth blocks is allowed, so the ancestor may be as low as
    # start_block - max_depth. raises ReorgError if it is deeper or history is missing.
    for depth in range(max_depth + 1):
        block_number = start_block - depth
        if block_number < 0:
            break

        stored_hash = await get_stored_hash(block_number)
        if stored_hash is None:
            raise ReorgError(
                f"no stored hash for block {block_number}; cannot find a common ancestor"
            )

        if stored_hash == await get_chain_hash(block_number):
            return block_number

    raise ReorgError(
        f"reorg deeper than {max_depth} blocks below block {start_block}"
    )
