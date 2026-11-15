import asyncio
from web3 import AsyncHTTPProvider, AsyncWeb3
from db import get_checkpoint, init_db, rollback_block, save_batch

# free public Ethereum RPC endpoint for testing without an API key
RPC_URL = "https://ethereum-rpc.publicnode.com"

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


async def main() -> None:
    print("indexer starting...")

    # initialize SQLite database and WAL mode
    await init_db()

    # initialize asynchronous Web3 client
    w3 = AsyncWeb3(AsyncHTTPProvider(RPC_URL))

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

        # resume from checkpoint or start a few blocks behind chain head
        chain_head = await w3.eth.block_number
        if last_indexed_block is None:
            last_indexed_block = chain_head - 5
            print(f"connected to Ethereum. starting fresh from block {last_indexed_block + 1}...")
        else:
            print(f"connected to Ethereum. resuming from checkpoint block {last_indexed_block + 1}...")

        # continuous indexing loop
        while True:
            chain_head = await w3.eth.block_number

            # wait if we are already caught up with the latest block
            if last_indexed_block >= chain_head:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            # calculate target block range using current adaptive batch size
            from_block = last_indexed_block + 1
            to_block = min(from_block + batch_size - 1, chain_head)

            try:
                # reorg check: verify that from_block points to our last known block hash
                if last_block_hash is not None:
                    first_block_data = await w3.eth.get_block(from_block)
                    parent_hash = first_block_data["parentHash"].hex()

                    if parent_hash != last_block_hash:
                        print(f"chain reorg detected at block {from_block}! stepping back...")
                        await rollback_block(last_indexed_block)
                        last_indexed_block -= 1
                        last_block_hash = None
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

                # decode each raw log and prepare rows for SQLite insertion
                decoded_transfers: list[tuple[str, int, int, str, str, float]] = []
                for log in logs:
                    decoded = contract.events.Transfer().process_log(log)
                    args = decoded["args"]
                    amount = float(args["value"] / 10**6)  # USDC has 6 decimals
                    decoded_transfers.append(
                        (
                            w3.to_hex(log["transactionHash"]),
                            int(log["logIndex"]),
                            int(log["blockNumber"]),
                            str(args["from"]),
                            str(args["to"]),
                            amount,
                        )
                    )

                # fetch the hash of to_block and save batch atomically to SQLite
                end_block_data = await w3.eth.get_block(to_block)
                new_block_hash = end_block_data["hash"].hex()
                await save_batch(decoded_transfers, to_block, new_block_hash)

                last_block_hash = new_block_hash
                last_indexed_block = to_block

                print(f"saved blocks {from_block}..{to_block} ({len(decoded_transfers)} transfers, batch={batch_size})")

                # gradually increase batch size after successful requests
                if batch_size < MAX_BATCH_SIZE:
                    batch_size = min(MAX_BATCH_SIZE, batch_size + 2)

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