#!/usr/bin/env python3

import argparse
import os
import queue
import threading
from datetime import datetime, timedelta, timezone

from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.boot_manager import BootManager
from p3lib.helper import get_app_data_path
from p3lib.helper import get_program_version
from p3lib.launcher import Launcher

from icon.icon_db import MODULE_NAME, DB_FILENAME, DEFAULT_HOST, open_db

# NiceGUI & Plotly imports
from nicegui import ui, app
import plotly.graph_objects as go

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMER_INTERVAL_S  = 0.1          # GUI timer period (100 ms)
DEFAULT_HOURS     = 24            # Default look-back window shown on load
REFRESH_INTERVAL  = 30            # seconds between background DB re-reads
DEFAULT_PORT      = 8100          # TCP port the NiceGUI web server listens on
MAX_POINTS_PER_TRACE = 2000       # downsample traces above this length before sending to browser

# ---------------------------------------------------------------------------
# Shared state (written by worker thread, read by GUI timer)
# ---------------------------------------------------------------------------

_gui_queue: queue.Queue = queue.Queue()

# ---------------------------------------------------------------------------
# Database read helpers
# ---------------------------------------------------------------------------

def _load_hops_from_db(db_path: str,
                        host: str,
                        hours: float = DEFAULT_HOURS) -> list[dict]:
    """Return a bucket-aggregated list of hop records from the last *hours* hours.

    Rows are divided into at most MAX_POINTS_PER_TRACE equal-width time buckets
    per hop.  Within each bucket the SQL query computes:
        - the midpoint timestamp  (used as the X axis value)
        - MIN(avg_ms)             (true minimum RTT seen in the bucket)
        - AVG(avg_ms)             (mean RTT across the bucket)
        - MAX(avg_ms)             (true maximum RTT seen in the bucket)
        - MIN(min_ms)             (best individual probe in the bucket)
        - MAX(max_ms)             (worst individual probe in the bucket)
        - the most recent hop_host seen in the bucket

    This guarantees that no latency spike or dip is ever silently discarded —
    the worst and best values within every bucket are always preserved in the
    hover tooltip, regardless of how many raw rows were aggregated.

    A bucket where every row has avg_ms IS NULL is recorded with avg_ms=NULL
    so unreachable periods are still shown as red markers on the plot.

    Each returned record is a dict with keys:
        timestamp   str    bucket midpoint (ISO-like, UTC)
        hop_number  int
        hop_host    str | None
        avg_ms      float | None
        min_ms      float | None   true min probe RTT across bucket
        max_ms      float | None   true max probe RTT across bucket
    """
    if not os.path.exists(db_path):
        return []

    conn = open_db(db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    # Count rows per hop to calculate the bucket size (stride)
    counts = conn.execute(
        """
        SELECT h.hop_number, COUNT(*) AS n
        FROM   hop_results h
        JOIN   traceroute_runs r ON h.run_id = r.id
        WHERE  r.host = ?
          AND  r.timestamp >= ?
        GROUP  BY h.hop_number
        """,
        (host, cutoff),
    ).fetchall()

    if not counts:
        conn.close()
        return []

    records = []
    for hop_number, n in counts:
        stride = max(1, n // MAX_POINTS_PER_TRACE)

        # Fetch all rows for this hop ordered by timestamp, then bucket them
        # in Python.  This avoids ROW_NUMBER() window functions which require
        # SQLite >= 3.25 and are not available on all deployments.
        #
        # Within each bucket of `stride` consecutive rows we compute:
        #   timestamp : midpoint (average epoch)
        #   avg_ms    : mean of non-NULL values; None if all NULL
        #   min_ms    : smallest min_ms in the bucket
        #   max_ms    : largest max_ms in the bucket
        #   hop_host  : last non-NULL host in the bucket
        raw = conn.execute(
            """
            SELECT r.timestamp, h.hop_number, h.hop_host,
                   h.avg_ms, h.min_ms, h.max_ms
            FROM   hop_results h
            JOIN   traceroute_runs r ON h.run_id = r.id
            WHERE  r.host       = ?
              AND  r.timestamp >= ?
              AND  h.hop_number  = ?
            ORDER  BY r.timestamp
            """,
            (host, cutoff, hop_number),
        ).fetchall()

        for i in range(0, len(raw), stride):
            bucket = raw[i : i + stride]

            # Midpoint timestamp
            epochs = [int(datetime.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S")
                          .replace(tzinfo=timezone.utc).timestamp())
                      for row in bucket]
            mid_epoch = int(sum(epochs) / len(epochs))
            mid_ts    = datetime.fromtimestamp(mid_epoch, tz=timezone.utc) \
                                .strftime("%Y-%m-%d %H:%M:%S")

            avgs = [row[3] for row in bucket if row[3] is not None]
            mins = [row[4] for row in bucket if row[4] is not None]
            maxs = [row[5] for row in bucket if row[5] is not None]

            hop_host = next((row[2] for row in reversed(bucket) if row[2]), None)

            records.append({
                "timestamp":  mid_ts,
                "hop_number": hop_number,
                "hop_host":   hop_host,
                "avg_ms":     sum(avgs) / len(avgs) if avgs else None,
                "min_ms":     min(mins) if mins else None,
                "max_ms":     max(maxs) if maxs else None,
            })

    conn.close()
    records.sort(key=lambda r: (r["timestamp"], r["hop_number"]))
    return records


def _build_figure(records: list[dict], host: str) -> go.Figure | None:
    """Build a single Plotly figure with one trace per hop.

    Each hop is identified in the legend by its IP address (or hop number if
    the address is unknown).  Unreachable samples are plotted as large red ✕
    markers at y=0 so outages are immediately obvious against the RTT lines.
    """
    if not records:
        return None

    # Group by hop_number
    hop_groups: dict[int, list[dict]] = {}
    for rec in records:
        hop_groups.setdefault(rec["hop_number"], []).append(rec)

    # One colour per hop, cycling through a palette
    PALETTE = [
        "#2196F3", "#4CAF50", "#FF9800", "#9C27B0",
        "#00BCD4", "#F44336", "#795548", "#607D8B",
    ]

    fig = go.Figure()
    unreachable_legend_shown = False

    for idx, hop_num in enumerate(sorted(hop_groups.keys())):
        rows      = hop_groups[hop_num]
        colour    = PALETTE[idx % len(PALETTE)]

        hop_label = next(
            (r["hop_host"] for r in reversed(rows) if r["hop_host"]),
            f"hop {hop_num}",
        )
        trace_name = f"Hop {hop_num}: {hop_label}"

        ts_ok, ms_ok, min_ok, max_ok = [], [], [], []
        ts_err                        = []

        for r in rows:
            if r["avg_ms"] is not None:
                ts_ok.append(r["timestamp"])
                ms_ok.append(r["avg_ms"])
                min_ok.append(r["min_ms"] if r["min_ms"] is not None else r["avg_ms"])
                max_ok.append(r["max_ms"] if r["max_ms"] is not None else r["avg_ms"])
            else:
                ts_err.append(r["timestamp"])

        # RTT line for this hop
        if ts_ok:
            fig.add_trace(go.Scatter(
                x=ts_ok,
                y=ms_ok,
                mode="lines+markers",
                name=trace_name,
                line=dict(color=colour, width=2),
                marker=dict(size=5, color=colour),
                customdata=list(zip(min_ok, max_ok)),
                hovertemplate=f"{trace_name}<br>%{{x}}<br>avg: %{{y:.2f}} ms  min: %{{customdata[0]:.2f}} ms  max: %{{customdata[1]:.2f}} ms<extra></extra>",
            ))

        # Unreachable markers — all hops share one legend entry to avoid clutter
        if ts_err:
            fig.add_trace(go.Scatter(
                x=ts_err,
                y=[0] * len(ts_err),
                mode="markers",
                name="No reply" if not unreachable_legend_shown else None,
                showlegend=not unreachable_legend_shown,
                legendgroup="no_reply",
                marker=dict(color="#F44336", size=16, symbol="x",
                            line=dict(width=2, color="#F44336")),
                hovertemplate=f"{trace_name}<br>%{{x}}<br>No reply<extra></extra>",
            ))
            unreachable_legend_shown = True

    fig.update_layout(
        title=dict(
            text=f"Traceroute RTT to {host}",
            font=dict(size=16, color="#e0e0e0"),
        ),
        xaxis=dict(
            title="Time (UTC)",
            title_font=dict(color="#a0a0a0"),
            tickfont=dict(color="#a0a0a0"),
            showgrid=True,
            gridcolor="#333333",
            linecolor="#444444",
        ),
        yaxis=dict(
            title="Avg RTT (ms)",
            title_font=dict(color="#a0a0a0"),
            tickfont=dict(color="#a0a0a0"),
            showgrid=True,
            gridcolor="#333333",
            linecolor="#444444",
            rangemode="tozero",
        ),
        legend=dict(
            orientation="v",
            x=0.01,
            xanchor="left",
            y=0.99,
            yanchor="top",
            bgcolor="rgba(20,20,20,0.75)",
            bordercolor="#555555",
            borderwidth=1,
            font=dict(color="#e0e0e0"),
        ),
        margin=dict(l=60, r=40, t=60, b=60),
        plot_bgcolor="#1a1a2e",
        paper_bgcolor="#121212",
        autosize=True,
    )
    return fig


def _load_hosts_from_db(db_path: str) -> list[str]:
    """Return all distinct hosts that have data in the database, oldest first."""
    if not os.path.exists(db_path):
        return []
    conn = open_db(db_path)
    rows = conn.execute(
        """
        SELECT host
        FROM   traceroute_runs
        GROUP  BY host
        ORDER  BY MIN(id)
        """
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Background worker thread
# ---------------------------------------------------------------------------

class _DBPoller(threading.Thread):
    """Periodically reads the SQLite DB and pushes update messages onto the queue."""

    def __init__(self,
                 db_path: str,
                 host: str,
                 hours: float,
                 gui_queue: queue.Queue,
                 stop_event: threading.Event):
        super().__init__(daemon=True)
        self._db_path    = db_path
        self._host       = host
        self._hours      = hours
        self._queue      = gui_queue
        self._stop_event = stop_event

    def run(self):
        while not self._stop_event.is_set():
            try:
                records = _load_hops_from_db(self._db_path, self._host, self._hours)
                hosts   = _load_hosts_from_db(self._db_path)
                self._queue.put({"type": "data", "records": records, "hosts": hosts})
            except Exception as exc:
                self._queue.put({"type": "error", "message": str(exc)})
            # Sleep in small increments so we can honour stop_event quickly
            for _ in range(int(REFRESH_INTERVAL / TIMER_INTERVAL_S)):
                if self._stop_event.is_set():
                    break
                self._stop_event.wait(TIMER_INTERVAL_S)


# ---------------------------------------------------------------------------
# GUI application class
# ---------------------------------------------------------------------------

class _ClientState:
    """Holds all NiceGUI widget references for a single browser connection.

    Each browser tab gets its own _ClientState so widget updates are routed
    to the correct client rather than overwriting a shared instance reference.
    """
    def __init__(self):
        self.plot:             ui.plotly | None  = None
        self.status_label:     ui.label  | None  = None
        self.plots_container:  ui.column | None  = None
        self.host_select:      ui.select | None  = None
        self.hours:            float              = DEFAULT_HOURS
        self.host:             str                = ""


class IConGUI:
    PROGRAM_NAME = "icon"

    def __init__(self, uio: UIO, options):
        """@brief Constructor
           @param uio A UIO instance
           @param options Parsed command-line options"""
        self._uio            = uio
        self._options        = options
        self._config_folder  = get_app_data_path(MODULE_NAME)
        self._db_path        = os.path.join(self._config_folder, DB_FILENAME)
        self._host           = options.host  # None means 'use first host found in DB'
        self._hours          = float(getattr(options, "hours", DEFAULT_HOURS))
        self._port           = int(getattr(options, "port", DEFAULT_PORT))
        self._no_browser     = bool(getattr(options, "no_browser", False))
        self._stop_event     = threading.Event()
        self._poller: _DBPoller | None = None

    # ------------------------------------------------------------------
    # NiceGUI page
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_figure() -> go.Figure:
        """Return a blank dark figure used to clear the plot after deletion."""
        fig = go.Figure()
        fig.update_layout(
            plot_bgcolor="#1a1a2e",
            paper_bgcolor="#121212",
            font=dict(color="#a0a0a0"),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            annotations=[dict(
                text="No data — waiting for icon_db …",
                x=0.5, y=0.5, xref="paper", yref="paper",
                showarrow=False,
                font=dict(size=16, color="#555555"),
            )],
            autosize=True,
            margin=dict(l=60, r=40, t=60, b=60),
        )
        return fig

    def _delete_all_data(self, state: "_ClientState"):
        """Wipe all rows from both tables, then clear the plot in-place."""
        conn = open_db(self._db_path)
        conn.execute("DELETE FROM hop_results")
        conn.execute("DELETE FROM traceroute_runs")
        conn.commit()
        conn.close()
        # Clear the plot in-place — never delete the widget itself, as
        # re-creating it from a button-click context causes NiceGUI client
        # context issues that prevent the new widget rendering correctly.
        if state.plot is not None:
            state.plot.update_figure(self._empty_figure())
        if state.status_label:
            state.status_label.set_text("All data deleted — waiting for new data …")
        self._trigger_refresh(state)

    def _build_page(self):
        """Define the NiceGUI page layout. Called for each new browser connection."""

        # Each browser connection gets its own state so widget references
        # are never shared or overwritten by a second client connecting.
        state = _ClientState()
        state.hours = self._hours
        # Use the CLI --host if given, otherwise defer to first host in DB
        state.host  = self._host or ""

        ui.dark_mode().enable()

        with ui.column().classes("w-full p-4 gap-4").style("height: 100vh; box-sizing: border-box;"):

            # ── Header ──────────────────────────────────────────────
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("ICon — Internet Connectivity Monitor").classes(
                    "text-2xl font-bold text-blue-400"
                )

            # ── Controls ────────────────────────────────────────────
            with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                ui.label("Host:").classes("text-sm text-gray-300")
                # Host dropdown — options are populated dynamically by the
                # poller; an empty placeholder is shown until the first poll.
                state.host_select = ui.select(
                    options={"": "Loading…"},
                    value="",
                    on_change=lambda e: self._on_host_change(e.value, state),
                ).classes("w-48")

                ui.label("Show last:").classes("text-sm text-gray-300")
                ui.select(
                    options={1: "1 hour", 6: "6 hours", 24: "24 hours",
                             48: "48 hours", 168: "7 days"},
                    value=int(state.hours),
                    on_change=lambda e: self._on_hours_change(e.value, state),
                ).classes("w-40")

                ui.button("Refresh now",
                          on_click=lambda: self._trigger_refresh(state)).props(
                    "color=primary"
                )

                # ── Delete button + confirmation dialog ─────────────
                with ui.dialog() as confirm_dialog, ui.card().classes(
                    "bg-gray-800 text-white p-6 rounded-xl"
                ):
                    ui.label("Delete all data?").classes(
                        "text-lg font-semibold text-red-400 mb-2"
                    )
                    ui.label(
                        "This will permanently remove every traceroute record "
                        "from the database. This cannot be undone."
                    ).classes("text-sm text-gray-300 mb-4")
                    with ui.row().classes("gap-3 justify-end w-full"):
                        ui.button("Cancel", on_click=confirm_dialog.close).props(
                            "flat color=white"
                        )
                        ui.button(
                            "Yes, delete all",
                            on_click=lambda: (confirm_dialog.close(),
                                             self._delete_all_data(state)),
                        ).props("color=negative")

                ui.button("Delete all data", on_click=confirm_dialog.open).props(
                    "color=negative outline"
                )

            # ── Status bar ──────────────────────────────────────────
            state.status_label = ui.label("Loading …").classes(
                "text-sm text-gray-400 italic"
            )

            ui.separator()

            # ── Plots area ──────────────────────────────────────────
            state.plots_container = ui.column().classes("w-full gap-4 flex-1").style("min-height: 0;")

        # ── 100 ms GUI timer — closure captures this client's state ─
        ui.timer(TIMER_INTERVAL_S, lambda: self._process_queue(state))

    def _on_host_change(self, value: str, state: "_ClientState"):
        if value:
            state.host = value
            # Clear the existing plot so stale data from the previous host
            # is not visible while the new host's data is being fetched.
            if state.plot is not None:
                state.plot.update_figure(self._empty_figure())
            self._trigger_refresh(state)

    def _on_hours_change(self, value: int, state: "_ClientState"):
        state.hours = float(value)
        self._trigger_refresh(state)

    def _trigger_refresh(self, state: "_ClientState | None" = None):
        """Ask the poller to do an immediate fetch (by restarting it).

        Uses state.host and state.hours when a client state is provided,
        falling back to the instance defaults otherwise.
        """
        host  = (state.host  if state and state.host  else self._host) or DEFAULT_HOST
        hours = (state.hours if state else None) or self._hours
        self._stop_event.set()
        self._stop_event = threading.Event()
        self._poller = _DBPoller(
            self._db_path,
            host,
            hours,
            _gui_queue,
            self._stop_event,
        )
        self._poller.start()

    # ------------------------------------------------------------------
    # Queue processor (runs on GUI thread, 100 ms tick)
    # ------------------------------------------------------------------

    def _process_queue(self, state: "_ClientState"):
        try:
            while True:                     # drain all pending messages
                msg = _gui_queue.get_nowait()
                if msg["type"] == "data":
                    self._update_host_select(msg.get("hosts", []), state)
                    self._update_plots(msg["records"], state)
                elif msg["type"] == "error":
                    if state.status_label:
                        state.status_label.set_text(f"Error: {msg['message']}")
        except queue.Empty:
            pass

    def _update_host_select(self, hosts: list[str], state: "_ClientState"):
        """Refresh the host dropdown options and set a default if not yet chosen."""
        if not hosts or state.host_select is None:
            return
        # Apply CLI --host filter if one was given
        if self._host:
            hosts = [h for h in hosts if h == self._host] or hosts
        options = {h: h for h in hosts}
        state.host_select.set_options(options)
        # Set a default host if none is selected yet
        if not state.host or state.host not in hosts:
            default = self._host if self._host in hosts else hosts[0]
            state.host = default
            state.host_select.set_value(default)
            self._trigger_refresh(state)

    def _update_plots(self, records: list[dict], state: "_ClientState"):
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if state.status_label:
            if records:
                hop_count = len({r["hop_number"] for r in records})
                pts_per_hop = len(records) // hop_count if hop_count else len(records)
                state.status_label.set_text(
                    f"Last updated: {now_str}  |  "
                    f"{len(records)} displayed points  ({pts_per_hop} per hop, {hop_count} hops)"
                )
            else:
                state.status_label.set_text(
                    f"Last updated: {now_str}  |  No data yet — is icon_db running?"
                )

        if state.plots_container is None:
            return

        fig = _build_figure(records, state.host) if records else self._empty_figure()

        if state.plot is None:
            # First data arrival for this client — create the widget inside
            # the container.  Safe here because we are in the ui.timer callback
            # which always holds the correct NiceGUI client context.
            with state.plots_container:
                state.plot = ui.plotly(fig).classes("w-full h-full")
        else:
            state.plot.update_figure(fig)

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def run(self):
        os.makedirs(self._config_folder, exist_ok=True)

        self._uio.info(f"Database path : {self._db_path}")
        self._uio.info(f"Target host   : {self._host}")
        self._uio.info(f"Look-back     : {self._hours} hours")

        # Register the NiceGUI page at '/' — executed at import time semantics
        @ui.page("/")
        def index():
            self._build_page()

        # The poller is started by _update_host_select once the first host
        # is discovered.  Kick off an initial host-list fetch with a
        # temporary poller so the dropdown is populated immediately.
        self._poller = _DBPoller(
            self._db_path,
            self._host or DEFAULT_HOST,
            self._hours,
            _gui_queue,
            self._stop_event,
        )
        self._poller.start()

        # Clean up on shutdown
        app.on_shutdown(self._stop_event.set)

        show_browser = not self._no_browser
        self._uio.info(f"GUI port      : {self._port}")
        self._uio.info(f"Open browser  : {show_browser}")
        ui.run(
            title="ICon — Internet Connectivity Monitor",
            favicon="🌐",
            reload=False,
            dark=True,
            port=self._port,
            show=show_browser,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """@brief Program entry point"""
    uio = UIO()
    prog_version = get_program_version(IConGUI.PROGRAM_NAME)
    uio.info(f"{IConGUI.PROGRAM_NAME}: V{prog_version}")
    options = None
    try:
        parser = argparse.ArgumentParser(
            description="A tool that provides a gui interface to the data collected by the icon_db tool.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("-d", "--debug",        action="store_true", help="Enable debugging.")
        parser.add_argument("-t", "--host",          help="The host address that traceroute will use to check internet connectivity.",
                            default=None, required=False)
        parser.add_argument("-p", "--poll_seconds",  type=float,
                            help="A periodicity of the traceroute command execution, in seconds.")
        parser.add_argument("--hours",               type=float, default=DEFAULT_HOURS,
                            help=f"How many hours of data to display (default: {DEFAULT_HOURS}).")
        parser.add_argument("--port",                type=int,   default=DEFAULT_PORT,
                            help=f"TCP port the GUI web server listens on (default: {DEFAULT_PORT}).")
        parser.add_argument("--no_browser",          action="store_true",
                            help="Do not open a browser tab on startup (useful for headless/systemd deployments).")
        launcher = Launcher("icon.png", app_name="icon")
        launcher.addLauncherArgs(parser)
        BootManager.AddCmdArgs(parser)

        options = parser.parse_args()
        uio.enableDebug(options.debug)

        handled = launcher.handleLauncherArgs(options, uio=uio)
        if not handled:
            handled = BootManager.HandleOptions(uio, options, False)
            if not handled:
                aClass = IConGUI(uio, options)
                aClass.run()

    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logTraceBack(uio)
        if options is None or options.debug:
            raise
        else:
            uio.error(str(ex))


if __name__ == "__main__":
    main()
