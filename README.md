Ethereum blockchain indexer

a lightweight, three-layer local blockchain indexer built with Python. it fetches EVM event logs asynchronously, stores decoded data in SQLite using WAL mode, and displays live metrics in a terminal user interface.

architecture

data ingestion (indexer.py): uses asyncio and web3.py to fetch ERC-20 Transfer logs (eth_getLogs) in adaptive batches, decodes ABI payloads, verifies chain integrity against reorgs, and tracks progress via checkpoints.

storage (db.py): uses aiosqlite with Write-Ahead Logging (WAL) enabled to allow simultaneous writes from the indexer and reads from the UI without database locks.

user interface (tui.py): a Textual terminal dashboard that polls the database independently to display live block progress, volume metrics, and recent transfers.

quick start

# create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# install dependencies
pip install -r requirements.txt

# run the background indexer in terminal 1
python indexer.py

# run the live terminal dashboard in terminal 2
python tui.py
