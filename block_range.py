def next_range(
    last_indexed: int, chain_head: int, batch_size: int, confirmations: int
) -> tuple[int, int] | None:
    # only index blocks that are at least `confirmations` blocks behind the tip
    safe_head = chain_head - confirmations

    # caught up (or ahead, e.g. checkpoint from an older version): nothing to do
    if last_indexed >= safe_head:
        return None

    # next batch, capped by the batch size and the safe head
    return last_indexed + 1, min(last_indexed + batch_size, safe_head)
