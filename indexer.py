import asyncio
import os
from web3 import AsyncHTTPProvider, AsyncWeb3
from block_range import next_range
from db import TransferRow, get_block_hash, get_checkpoint, init_db, rollback_to, save_batch
from reorg import ReorgError, find_common_ancestor

# free public RPC that also serves older logs; override with RPC_URL env var
RPC_URL = os.environ.get("RPC_URL", "https://eth.drpc.org")

# USDC token contract address on Ethereum mainnet
CONTRACT_ADDRESS = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

# minimal ABI containing only the Transfer event definition
ERC20_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    }
]

# batch size and polling configuration
INITIAL_BATCH_SIZE = 5
MAX_BATCH_SIZE = 25
POLL_INTERVAL_SECONDS = 4

# stay this many blocks behind the tip: a load-balanced RPC may route get_logs
# to a lagging node that returns empty logs for blocks it does not have yet
CONFIRMATIONS = 3

# deepest chain reorg we are willing to roll back automatically
MAX_REORG_DEPTH = 64


async def main() -> None:
    print("indexer starting...")

    # initialize SQLite database and WAL mode
    await init_db()

    # initialize asynchronous Web3 client
    w3 = AsyncWeb3(AsyncHTTPProvider(RPC_URL, request_kwargs={"headers": {"User-Agent": "Mozilla/5.0"}}))

    # load saved checkpoint from database if one exists
    checkpoint = await get_checkpoint()
    last_indexed_block: int | None = checkpoint[0] if checkpoint else None
    last_block_hash: str | None = checkpoint[1] if checkpoint else None
    batch_size = INITIAL_BATCH_SIZE

    try:
        is_connected = await w3.is_connected()
        if not is_connected:
            print("failed to connect to the RPC node.")
            return

        # initialize contract helper and event topic signature for RPC filtering
        checksum_address = w3.to_checksum_address(CONTRACT_ADDRESS)
        contract = w3.eth.contract(address=checksum_address, abi=ERC20_ABI)
        transfer_topic = w3.to_hex(w3.keccak(text="Transfer(address,address,uint256)"))

        # resume from checkpoint or start a few blocks behind the safe head
        chain_head = await w3.eth.block_number
        if last_indexed_block is None:
            last_indexed_block = chain_head - CONFIRMATIONS - 5
            print(f"connected to Ethereum. starting fresh from block {last_indexed_block + 1}...")
        else:
            print(f"connected to Ethereum. resuming from checkpoint block {last_indexed_block + 1}...")

        # continuous indexing loop
        while True:
            chain_head = await w3.eth.block_number

            # calculate target block range using current adaptive batch size
            block_range = next_range(last_indexed_block, chain_head, batch_size, CONFIRMATIONS)

            # wait if we are already caught up with the safe head
            if block_range is None:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            from_block, to_block = block_range

            try:
                # fetch every block header in the batch to get their hashes
                headers = await asyncio.gather(
                    *(w3.eth.get_block(n) for n in range(from_block, to_block + 1))
                )
                block_hashes: list[tuple[int, str]] = [
                    (int(h["number"]), w3.to_hex(h["hash"])) for h in headers
                ]

                # reorg check: from_block must build on our last stored block hash
                first_parent_hash = w3.to_hex(headers[0]["parentHash"])
                if last_block_hash is not None and first_parent_hash != last_block_hash:
                    print(f"chain reorg detected at block {from_block}! searching for common ancestor...")

                    async def get_chain_hash(block_number: int) -> str:
                        block = await w3.eth.get_block(block_number)
                        return w3.to_hex(block["hash"])

                    ancestor = await find_common_ancestor(
                        last_indexed_block, get_chain_hash, get_block_hash, MAX_REORG_DEPTH
                    )
                    await rollback_to(ancestor + 1)
                    last_indexed_block = ancestor
                    last_block_hash = await get_block_hash(ancestor)
                    print(f"rolled back to common ancestor block {ancestor}, re-indexing from {ancestor + 1}")
                    continue

                # headers fetched concurrently must form one chain, else a reorg hit mid-fetch
                is_linked = all(
                    w3.to_hex(headers[i]["parentHash"]) == block_hashes[i - 1][1]
                    for i in range(1, len(headers))
                )
                if not is_linked:
                    print(f"block headers {from_block}..{to_block} are not linked, retrying...")
                    await asyncio.sleep(1)
                    continue

                # fetch only Transfer event logs using the topic filter
                logs = await w3.eth.get_logs(
                    {
                        "fromBlock": from_block,
                        "toBlock": to_block,
                        "address": checksum_address,
                        "topics": [transfer_topic],
                    }
                )

                # every log must come from the same block we fetched the header for
                hash_by_block = dict(block_hashes)
                mismatched = [
                    log for log in logs
                    if hash_by_block.get(int(log["blockNumber"])) != w3.to_hex(log["blockHash"])
                ]
                if mismatched:
                    print(f"log block hashes do not match headers in {from_block}..{to_block}, retrying...")
                    await asyncio.sleep(1)
                    continue

                # decode each raw log and prepare rows for SQLite insertion
                decoded_transfers: list[TransferRow] = []
                for log in logs:
                    decoded = contract.events.Transfer().process_log(log)
                    args = decoded["args"]
                    decoded_transfers.append(
                        (
                            w3.to_hex(log["transactionHash"]),
                            int(log["logIndex"]),
                            int(log["blockNumber"]),
                            str(args["from"]),
                            str(args["to"]),
                            str(args["value"]),  # raw uint256 as exact decimal string
                        )
                    )

                # save transfers, block hashes and checkpoint atomically to SQLite
                await save_batch(decoded_transfers, block_hashes)

                last_indexed_block, last_block_hash = block_hashes[-1]

                print(f"saved blocks {from_block}..{to_block} ({len(decoded_transfers)} transfers, batch={batch_size})")

                # gradually increase batch size after successful requests
                if batch_size < MAX_BATCH_SIZE:
                    batch_size = min(MAX_BATCH_SIZE, batch_size + 2)

            except ReorgError as reorg_error:
                # too deep to repair automatically: stop instead of indexing a wrong chain
                print(f"fatal: {reorg_error}. max reorg depth is {MAX_REORG_DEPTH} blocks.")
                print("stopping indexer. inspect the chain and the database before restarting.")
                return

            except Exception as batch_error:
                # adaptive backoff: shrink batch size if RPC node rejects large queries
                print(f"batch error on {from_block}..{to_block}: {batch_error}")
                if batch_size > 1:
                    batch_size = max(1, batch_size // 2)
                    print(f"reducing batch size to {batch_size} and retrying...")
                await asyncio.sleep(2)

    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        print("\nshutdown signal received.")
    finally:
        # properly close the underlying aiohttp session to prevent resource warnings
        await w3.provider.disconnect()
        print("indexer stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nindexer stopped.")