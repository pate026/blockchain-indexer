from block_range import next_range


def test_caught_up_at_safe_head() -> None:
    # last indexed block is exactly the safe head
    assert next_range(97, 100, 10, 3) is None


def test_ahead_of_safe_head() -> None:
    # checkpoint from an older version that indexed up to the tip
    assert next_range(100, 100, 10, 3) is None
    assert next_range(99, 100, 10, 3) is None


def test_capped_by_batch_size() -> None:
    # gap to the safe head is larger than the batch size
    assert next_range(50, 100, 10, 3) == (51, 60)


def test_capped_by_safe_head() -> None:
    # batch size is larger than the gap to the safe head
    assert next_range(90, 100, 25, 3) == (91, 97)


def test_zero_confirmations_matches_old_logic() -> None:
    # without confirmations the chain head itself is the limit
    assert next_range(100, 100, 10, 0) is None
    assert next_range(95, 100, 10, 0) == (96, 100)
    assert next_range(50, 100, 10, 0) == (51, 60)
