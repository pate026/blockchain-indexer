import asyncio
from decimal import Decimal, localcontext
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static
from db import get_dashboard_data, init_db

# USDC token decimals (raw amounts are stored in the smallest unit)
USDC_DECIMALS = 6


def format_usdc(raw: int) -> str:
    # convert raw units to USDC with exact decimal math, 2 decimals and thousands separator
    with localcontext() as ctx:
        ctx.prec = 100  # enough digits for any uint256 value
        return f"{Decimal(raw) / Decimal(10**USDC_DECIMALS):,.2f}"


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

    # keyboard shortcuts to pause live updates or quit the TUI
    BINDINGS = [
        ("p", "toggle_pause", "pause/resume"),
        ("q", "quit", "quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.paused = False

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

    def action_toggle_pause(self) -> None:
        # toggle live table updates on or off so rows can be read comfortably
        self.paused = not self.paused
        if self.paused:
            self.sub_title = "PAUSED (press 'p' to resume)"
        else:
            self.sub_title = ""

    async def refresh_data(self) -> None:
        # skip refresh while paused by the user
        if self.paused:
            return

        # query SQLite asynchronously and update widgets without blocking UI
        try:
            last_block, total_count, total_volume, rows = await get_dashboard_data(limit=50)

            self.query_one("#card-block", Static).update(f"latest block\n{last_block:,}")
            self.query_one("#card-count", Static).update(f"indexed transfers\n{total_count:,}")
            self.query_one("#card-volume", Static).update(f"total volume\n{format_usdc(total_volume)} USDC")

            table = self.query_one("#transfers-table", DataTable)

            # save current cursor and scroll position before clearing rows
            saved_row = table.cursor_row
            saved_scroll_y = table.scroll_y

            table.clear()

            for block_num, sender, receiver, amount, tx_hash in rows:
                short_from = f"{sender[:8]}...{sender[-6:]}"
                short_to = f"{receiver[:8]}...{receiver[-6:]}"
                short_tx = f"{tx_hash[:10]}..."
                table.add_row(
                    str(block_num),
                    short_from,
                    short_to,
                    format_usdc(amount),
                    short_tx,
                )

            # restore cursor and scroll position so the view does not jump to the top
            if rows:
                target_row = min(saved_row, len(rows) - 1)
                table.move_cursor(row=target_row, animate=False)
                table.scroll_to(y=saved_scroll_y, animate=False)

        except Exception as e:
            self.notify(f"database read error: {e}", severity="error")


if __name__ == "__main__":
    app = IndexerTUI()
    app.run()