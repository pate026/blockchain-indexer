import asyncio
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static
from db import get_dashboard_data, init_db


class IndexerTUI(App):
    # clean terminal styling for stats cards and data table
    CSS = """
    #stats-bar {
        height: 5;
        margin: 1 1 0 1;
    }
    .stat-card {
        width: 1fr;
        height: 100%;
        border: round $accent;
        content-align: center middle;
        text-style: bold;
    }
    DataTable {
        height: 1fr;
        margin: 1;
        border: round $primary;
    }
    """

    # keyboard shortcut to quit the TUI
    BINDINGS = [("q", "quit", "quit")]

    def compose(self) -> ComposeResult:
        # build the UI layout: header, three metric cards, event table, and footer
        yield Header(show_clock=True)
        with Horizontal(id="stats-bar"):
            yield Static("latest block: -", id="card-block", classes="stat-card")
            yield Static("total transfers: 0", id="card-count", classes="stat-card")
            yield Static("total volume: 0.00 USDC", id="card-volume", classes="stat-card")
        yield DataTable(id="transfers-table")
        yield Footer()

    async def on_mount(self) -> None:
        # configure table columns and start periodic refresh timer
        self.title = "Ethereum USDC blockchain indexer"
        await init_db()

        table = self.query_one("#transfers-table", DataTable)
        table.add_columns("block", "from", "to", "amount (USDC)", "tx hash")
        table.cursor_type = "row"

        # load initial data immediately and refresh every 2 seconds
        await self.refresh_data()
        self.set_interval(2.0, self.refresh_data)

    async def refresh_data(self) -> None:
        # query SQLite asynchronously and update widgets without blocking UI
        try:
            last_block, total_count, total_volume, rows = await get_dashboard_data(limit=30)

            self.query_one("#card-block", Static).update(f"latest block\n{last_block:,}")
            self.query_one("#card-count", Static).update(f"indexed transfers\n{total_count:,}")
            self.query_one("#card-volume", Static).update(f"total volume\n{total_volume:,.2f} USDC")

            table = self.query_one("#transfers-table", DataTable)
            table.clear()

            for block_num, sender, receiver, amount, tx_hash in rows:
                short_from = f"{sender[:8]}...{sender[-6:]}"
                short_to = f"{receiver[:8]}...{receiver[-6:]}"
                short_tx = f"{tx_hash[:10]}..."
                table.add_row(
                    str(block_num),
                    short_from,
                    short_to,
                    f"{amount:,.2f}",
                    short_tx,
                )
        except Exception as e:
            self.notify(f"database read error: {e}", severity="error")


if __name__ == "__main__":
    app = IndexerTUI()
    app.run()