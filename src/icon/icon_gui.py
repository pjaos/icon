#!/usr/bin/env python3

import argparse
import csv
import io
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

from icon.icon_db import (
    MODULE_NAME, DB_FILENAME,
    DEFAULT_HOST, open_db, get_alert_log_path,
)

# NiceGUI & Plotly imports
from nicegui import ui, app
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMER_INTERVAL_S     = 0.1    # GUI timer period (100 ms)
DEFAULT_HOURS        = 24     # Default look-back window
REFRESH_INTERVAL     = 30     # seconds between background DB re-reads
DEFAULT_PORT         = 8100   # TCP port the NiceGUI web server listens on
MAX_POINTS_PER_TRACE = 2000   # max points per trace sent to the browser

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_gui_queue: queue.Queue = queue.Queue()

# ---------------------------------------------------------------------------
# Database read helpers
# ---------------------------------------------------------------------------

def _load_hops_from_db(db_path: str,
                        host: str,
                        hours: float = DEFAULT_HOURS) -> list[dict]:
    """Return bucket-aggregated hop records for *host* over the last *hours*.

    Bucketing is done in Python (no window functions) for SQLite compatibility.
    Each record dict has keys:
        timestamp, hop_number, hop_host,
        avg_ms, min_ms, max_ms,
        probe_count, reply_count
    """
    if not os.path.exists(db_path):
        return []

    conn   = open_db(db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)) \
                .strftime("%Y-%m-%d %H:%M:%S")

    counts = conn.execute(
        """
        SELECT h.hop_number, COUNT(*) AS n
        FROM   hop_results h
        JOIN   traceroute_runs r ON h.run_id = r.id
        WHERE  r.host = ? AND r.timestamp >= ?
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

        raw = conn.execute(
            """
            SELECT r.timestamp, h.hop_number, h.hop_host,
                   h.avg_ms, h.min_ms, h.max_ms,
                   h.probe_count, h.reply_count
            FROM   hop_results h
            JOIN   traceroute_runs r ON h.run_id = r.id
            WHERE  r.host = ? AND r.timestamp >= ? AND h.hop_number = ?
            ORDER  BY r.timestamp
            """,
            (host, cutoff, hop_number),
        ).fetchall()

        for i in range(0, len(raw), stride):
            bucket = raw[i : i + stride]

            epochs = [int(datetime.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S")
                          .replace(tzinfo=timezone.utc).timestamp())
                      for row in bucket]
            mid_ts = datetime.fromtimestamp(
                int(sum(epochs) / len(epochs)), tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")

            avgs    = [row[3] for row in bucket if row[3] is not None]
            mins    = [row[4] for row in bucket if row[4] is not None]
            maxs    = [row[5] for row in bucket if row[5] is not None]
            probes  = sum(row[6] for row in bucket if row[6] is not None)
            replies = sum(row[7] for row in bucket if row[7] is not None)
            hop_host = next((row[2] for row in reversed(bucket) if row[2]), None)

            records.append({
                "timestamp":   mid_ts,
                "hop_number":  hop_number,
                "hop_host":    hop_host,
                "avg_ms":      sum(avgs) / len(avgs) if avgs else None,
                "min_ms":      min(mins) if mins else None,
                "max_ms":      max(maxs) if maxs else None,
                "probe_count": probes,
                "reply_count": replies,
            })

    conn.close()
    records.sort(key=lambda r: (r["timestamp"], r["hop_number"]))
    return records


def _load_hosts_from_db(db_path: str) -> list[str]:
    """Return all distinct hosts that have data, in insertion order."""
    if not os.path.exists(db_path):
        return []
    conn = open_db(db_path)
    rows = conn.execute(
        "SELECT host FROM traceroute_runs GROUP BY host ORDER BY MIN(id)"
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def _load_annotations_from_db(db_path: str,
                                host: str,
                                hours: float) -> list[dict]:
    """Return annotations for *host* within the look-back window."""
    if not os.path.exists(db_path):
        return []
    conn    = open_db(db_path)
    cutoff  = (datetime.now(timezone.utc) - timedelta(hours=hours)) \
                 .strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        SELECT timestamp, note FROM annotations
        WHERE  host = ? AND timestamp >= ?
        ORDER  BY timestamp
        """,
        (host, cutoff),
    ).fetchall()
    conn.close()
    return [{"timestamp": row[0], "note": row[1]} for row in rows]


def _save_annotation(db_path: str, host: str, note: str) -> None:
    """Write a new annotation to the database."""
    conn = open_db(db_path)
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO annotations (timestamp, host, note) VALUES (?,?,?)",
        (ts, host, note),
    )
    conn.commit()
    conn.close()


def _load_recent_alerts(alert_log: str, max_lines: int = 50) -> list[str]:
    """Return the last *max_lines* lines from the alert log."""
    if not os.path.exists(alert_log):
        return []
    try:
        with open(alert_log) as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-max_lines:]]
    except OSError:
        return []


def _db_size_str(db_path: str) -> str:
    """Return a human-readable string for the current DB file size."""
    try:
        size = os.path.getsize(db_path)
    except OSError:
        return "unknown"
    if size >= 1_048_576:
        return f"{size / 1_048_576:.1f} MB"
    return f"{size / 1024:.1f} KB"


# ---------------------------------------------------------------------------
# Figure builder
# ---------------------------------------------------------------------------

def _build_figure(records: list[dict],
                  annotations: list[dict],
                  host: str) -> go.Figure | None:
    """Build a two-subplot Plotly figure:
       - Top subplot : RTT lines (avg, with min/max in hover)
       - Bottom subplot: packet loss % per hop
    Annotation notes appear as vertical dashed lines across both subplots.
    """
    if not records:
        return None

    hop_groups: dict[int, list[dict]] = {}
    for rec in records:
        hop_groups.setdefault(rec["hop_number"], []).append(rec)

    PALETTE = [
        "#2196F3", "#4CAF50", "#FF9800", "#9C27B0",
        "#00BCD4", "#F44336", "#795548", "#607D8B",
    ]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.06,
    )

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
        ts_err = []
        ts_loss, loss_vals = [], []

        for r in rows:
            pc = r.get("probe_count") or 3
            rc = r.get("reply_count") or 0
            loss_pct = 100.0 * (pc - rc) / pc if pc else 100.0

            if r["avg_ms"] is not None:
                ts_ok.append(r["timestamp"])
                ms_ok.append(r["avg_ms"])
                min_ok.append(r["min_ms"] if r["min_ms"] is not None else r["avg_ms"])
                max_ok.append(r["max_ms"] if r["max_ms"] is not None else r["avg_ms"])
            else:
                ts_err.append(r["timestamp"])

            ts_loss.append(r["timestamp"])
            loss_vals.append(loss_pct)

        # ── RTT line ────────────────────────────────────────────────
        if ts_ok:
            fig.add_trace(go.Scatter(
                x=ts_ok, y=ms_ok,
                mode="lines+markers",
                name=trace_name,
                legendgroup=trace_name,
                line=dict(color=colour, width=2),
                marker=dict(size=5, color=colour),
                customdata=list(zip(min_ok, max_ok)),
                hovertemplate=(
                    f"{trace_name}<br>%{{x}}<br>"
                    "avg: %{y:.2f} ms  "
                    "min: %{customdata[0]:.2f} ms  "
                    "max: %{customdata[1]:.2f} ms"
                    "<extra></extra>"
                ),
            ), row=1, col=1)

        # ── Unreachable markers ─────────────────────────────────────
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
            ), row=1, col=1)
            unreachable_legend_shown = True

        # ── Packet loss % ────────────────────────────────────────────
        if ts_loss:
            fig.add_trace(go.Scatter(
                x=ts_loss, y=loss_vals,
                mode="lines",
                name=trace_name,
                legendgroup=trace_name,
                showlegend=False,
                line=dict(color=colour, width=1.5),
                hovertemplate=(
                    f"{trace_name}<br>%{{x}}<br>"
                    "loss: %{y:.0f}%<extra></extra>"
                ),
            ), row=2, col=1)

    # ── Annotation vertical lines ────────────────────────────────────
    shapes, annot_labels = [], []
    for ann in annotations:
        shapes.append(dict(
            type="line",
            x0=ann["timestamp"], x1=ann["timestamp"],
            y0=0, y1=1,
            xref="x", yref="paper",
            line=dict(color="#FFD700", width=1.5, dash="dash"),
        ))
        annot_labels.append(dict(
            x=ann["timestamp"], y=1,
            xref="x", yref="paper",
            text=ann["note"],
            showarrow=False,
            font=dict(color="#FFD700", size=11),
            xanchor="left",
            bgcolor="rgba(0,0,0,0.5)",
        ))

    _AXIS = dict(
        title_font=dict(color="#a0a0a0"),
        tickfont=dict(color="#a0a0a0"),
        showgrid=True,
        gridcolor="#333333",
        linecolor="#444444",
    )

    fig.update_layout(
        title=dict(
            text=f"Traceroute RTT to {host}",
            font=dict(size=16, color="#e0e0e0"),
        ),
        xaxis=dict(**_AXIS),
        xaxis2=dict(title="Time (UTC)", **_AXIS),
        yaxis=dict(title="Avg RTT (ms)", rangemode="tozero", **_AXIS),
        yaxis2=dict(title="Loss %", rangemode="tozero",
                    range=[0, 100], **_AXIS),
        legend=dict(
            orientation="v",
            x=1.01, xanchor="left",
            y=1,    yanchor="top",
            bgcolor="rgba(20,20,20,0.75)",
            bordercolor="#555555", borderwidth=1,
            font=dict(color="#e0e0e0"),
        ),
        margin=dict(l=60, r=180, t=60, b=60),
        plot_bgcolor="#1a1a2e",
        paper_bgcolor="#121212",
        autosize=True,
        shapes=shapes,
        annotations=annot_labels,
    )
    return fig


# ---------------------------------------------------------------------------
# Statistics helper
# ---------------------------------------------------------------------------

def _compute_stats(records: list[dict]) -> list[dict]:
    """Return one stats dict per hop_number from the records list."""
    hop_groups: dict[int, list[dict]] = {}
    for r in records:
        hop_groups.setdefault(r["hop_number"], []).append(r)

    stats = []
    for hop_num in sorted(hop_groups.keys()):
        rows = hop_groups[hop_num]
        hop_label = next(
            (r["hop_host"] for r in reversed(rows) if r["hop_host"]),
            f"hop {hop_num}",
        )
        avgs       = [r["avg_ms"] for r in rows if r["avg_ms"] is not None]
        all_probes = sum(r.get("probe_count") or 3 for r in rows)
        all_replies= sum(r.get("reply_count") or 0 for r in rows)
        loss_pct   = 100.0 * (all_probes - all_replies) / all_probes \
                     if all_probes else 100.0

        stats.append({
            "hop":      hop_num,
            "host":     hop_label,
            "samples":  len(rows),
            "min_ms":   f"{min(avgs):.2f}" if avgs else "—",
            "avg_ms":   f"{sum(avgs)/len(avgs):.2f}" if avgs else "—",
            "max_ms":   f"{max(avgs):.2f}" if avgs else "—",
            "loss_pct": f"{loss_pct:.1f}%",
        })
    return stats


# ---------------------------------------------------------------------------
# CSV export helper
# ---------------------------------------------------------------------------

def _records_to_csv(records: list[dict]) -> str:
    """Serialise records to a CSV string."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "timestamp", "hop_number", "hop_host",
        "avg_ms", "min_ms", "max_ms",
        "probe_count", "reply_count",
    ])
    writer.writeheader()
    writer.writerows(records)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Background worker thread
# ---------------------------------------------------------------------------

class _DBPoller(threading.Thread):
    """Periodically reads the DB and pushes update messages onto the queue."""

    def __init__(self, db_path, host, hours, gui_queue, stop_event):
        super().__init__(daemon=True)
        self._db_path    = db_path
        self._host       = host
        self._hours      = hours
        self._queue      = gui_queue
        self._stop_event = stop_event

    def run(self):
        while not self._stop_event.is_set():
            try:
                records     = _load_hops_from_db(self._db_path,
                                                  self._host, self._hours)
                hosts       = _load_hosts_from_db(self._db_path)
                annotations = _load_annotations_from_db(
                    self._db_path, self._host, self._hours)
                self._queue.put({
                    "type":        "data",
                    "records":     records,
                    "hosts":       hosts,
                    "annotations": annotations,
                })
            except Exception as exc:
                self._queue.put({"type": "error", "message": str(exc)})
            for _ in range(int(REFRESH_INTERVAL / TIMER_INTERVAL_S)):
                if self._stop_event.is_set():
                    break
                self._stop_event.wait(TIMER_INTERVAL_S)


# ---------------------------------------------------------------------------
# GUI application class
# ---------------------------------------------------------------------------

class _ClientState:
    """Per-browser-connection widget references."""
    def __init__(self):
        self.plot:              ui.plotly | None = None
        self.status_label:      ui.label  | None = None
        self.plots_container:   ui.column | None = None
        self.host_select:       ui.select | None = None
        self.stats_rows:        ui.element| None = None
        self.alert_rows:        ui.element| None = None
        self.annotation_input:  ui.input  | None = None
        self.hours:             float             = DEFAULT_HOURS
        self.host:              str               = ""
        # Last records/annotations received — used for CSV export
        self.last_records:      list[dict]        = []
        self.last_annotations:  list[dict]        = []


class IConGUI:
    PROGRAM_NAME = "icon"

    def __init__(self, uio: UIO, options):
        self._uio           = uio
        self._options       = options
        self._config_folder = get_app_data_path(MODULE_NAME)
        self._db_path       = os.path.join(self._config_folder, DB_FILENAME)
        self._alert_log     = get_alert_log_path(self._config_folder)
        self._host          = options.host
        self._hours         = float(getattr(options, "hours", DEFAULT_HOURS))
        self._port          = int(getattr(options, "port", DEFAULT_PORT))
        self._no_browser    = bool(getattr(options, "no_browser", False))
        self._stop_event    = threading.Event()
        self._poller: _DBPoller | None = None

    # ------------------------------------------------------------------
    # Static figure helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_figure() -> go.Figure:
        fig = go.Figure()
        fig.update_layout(
            plot_bgcolor="#1a1a2e", paper_bgcolor="#121212",
            font=dict(color="#a0a0a0"),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            annotations=[dict(
                text="No data — waiting for icon_db …",
                x=0.5, y=0.5, xref="paper", yref="paper",
                showarrow=False, font=dict(size=16, color="#555555"),
            )],
            autosize=True, margin=dict(l=60, r=180, t=60, b=60),
        )
        return fig

    # ------------------------------------------------------------------
    # Page builder
    # ------------------------------------------------------------------

    def _build_page(self):
        state       = _ClientState()
        state.hours = self._hours
        state.host  = self._host or ""

        ui.dark_mode().enable()

        with ui.column().classes("w-full p-4 gap-2").style(
            "height: 100vh; box-sizing: border-box;"
        ):
            # ── Header ──────────────────────────────────────────────
            ui.label("ICon — Internet Connectivity Monitor").classes(
                "text-2xl font-bold text-blue-400"
            )

            # ── Controls row ────────────────────────────────────────
            with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                ui.label("Host:").classes("text-sm text-gray-300")
                state.host_select = ui.select(
                    options={"": "Loading…"}, value="",
                    on_change=lambda e: self._on_host_change(e.value, state),
                ).classes("w-48")

                ui.label("Show last:").classes("text-sm text-gray-300")
                ui.select(
                    options={1: "1 hour", 6: "6 hours", 24: "24 hours",
                             48: "48 hours", 168: "7 days"},
                    value=int(state.hours),
                    on_change=lambda e: self._on_hours_change(e.value, state),
                ).classes("w-40")

                ui.button("Refresh",
                          on_click=lambda: self._trigger_refresh(state)).props(
                    "color=primary"
                )
                ui.button("Export CSV",
                          on_click=lambda: self._export_csv(state)).props(
                    "color=secondary outline"
                )

                # Delete dialog — title/body update dynamically with
                # the selected host when the dialog is opened.
                with ui.dialog() as confirm_dialog, ui.card().classes(
                    "bg-gray-800 text-white p-6 rounded-xl"
                ):
                    delete_title = ui.label("").classes(
                        "text-lg font-semibold text-red-400 mb-2"
                    )
                    delete_body = ui.label("").classes(
                        "text-sm text-gray-300 mb-4"
                    )
                    with ui.row().classes("gap-3 justify-end w-full"):
                        ui.button("Cancel",
                                  on_click=confirm_dialog.close).props(
                            "flat color=white"
                        )
                        ui.button(
                            "Yes, delete",
                            on_click=lambda: (confirm_dialog.close(),
                                             self._delete_host_data(state)),
                        ).props("color=negative")

                def _open_delete_dialog():
                    delete_title.set_text(
                        f"Delete data for {state.host or 'selected host'}?"
                    )
                    delete_body.set_text(
                        f"This will permanently remove all traceroute records "
                        f"and annotations for {state.host or 'this host'}. "
                        f"This cannot be undone."
                    )
                    confirm_dialog.open()

                ui.button("Delete", on_click=_open_delete_dialog).props(
                    "color=negative outline"
                )

            # ── Status bar ──────────────────────────────────────────
            state.status_label = ui.label("Loading …").classes(
                "text-sm text-gray-400 italic"
            )

            ui.separator()

            # ── Main plot ───────────────────────────────────────────
            state.plots_container = ui.column().classes(
                "w-full flex-1"
            ).style("min-height: 0;")

            # ── Annotation input ─────────────────────────────────────
            with ui.row().classes("w-full items-center gap-2"):
                ui.label("Add note:").classes("text-sm text-gray-300")
                state.annotation_input = ui.input(
                    placeholder="Annotation text …"
                ).classes("flex-1")
                ui.button("Add",
                          on_click=lambda: self._add_annotation(state)).props(
                    "color=primary"
                )

            ui.separator()

            # ── Statistics panel (collapsible) ───────────────────────
            with ui.expansion("Statistics", icon="bar_chart").classes(
                "w-full text-gray-300"
            ):
                with ui.element("div").classes("w-full overflow-auto"):
                    state.stats_rows = ui.element("div")

            # ── Alerts panel (collapsible) ───────────────────────────
            with ui.expansion("Recent alerts", icon="warning").classes(
                "w-full text-gray-300"
            ):
                state.alert_rows = ui.element("div")

        ui.timer(TIMER_INTERVAL_S, lambda: self._process_queue(state))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_host_change(self, value: str, state: "_ClientState"):
        if value:
            state.host = value
            if state.plot is not None:
                state.plot.update_figure(self._empty_figure())
            self._trigger_refresh(state)

    def _on_hours_change(self, value: int, state: "_ClientState"):
        state.hours = float(value)
        self._trigger_refresh(state)

    def _add_annotation(self, state: "_ClientState"):
        if not state.annotation_input:
            return
        note = state.annotation_input.value.strip()
        if note and state.host:
            _save_annotation(self._db_path, state.host, note)
            state.annotation_input.set_value("")
            self._trigger_refresh(state)

    def _export_csv(self, state: "_ClientState"):
        if not state.last_records:
            ui.notify("No data to export.", type="warning")
            return
        csv_str  = _records_to_csv(state.last_records)
        filename = (f"icon_{state.host}_"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv")
        ui.download(csv_str.encode(), filename=filename)

    def _delete_host_data(self, state: "_ClientState"):
        """Delete all data for the currently selected host only."""
        host = state.host
        if not host:
            return
        conn = open_db(self._db_path)
        conn.execute(
            """DELETE FROM hop_results
               WHERE run_id IN (
                   SELECT id FROM traceroute_runs WHERE host = ?
               )""",
            (host,),
        )
        conn.execute("DELETE FROM traceroute_runs WHERE host = ?", (host,))
        conn.execute("DELETE FROM annotations WHERE host = ?", (host,))
        conn.commit()
        conn.close()
        state.last_records     = []
        state.last_annotations = []
        if state.plot is not None:
            state.plot.update_figure(self._empty_figure())
        if state.status_label:
            state.status_label.set_text(
                f"Data for {host} deleted — waiting for new data …"
            )
        self._trigger_refresh(state)

    def _trigger_refresh(self, state: "_ClientState | None" = None):
        host  = (state.host  if state and state.host  else self._host) \
                or DEFAULT_HOST
        hours = (state.hours if state else None) or self._hours
        self._stop_event.set()
        self._stop_event = threading.Event()
        self._poller = _DBPoller(
            self._db_path, host, hours, _gui_queue, self._stop_event
        )
        self._poller.start()

    # ------------------------------------------------------------------
    # Queue processor
    # ------------------------------------------------------------------

    def _process_queue(self, state: "_ClientState"):
        try:
            while True:
                msg = _gui_queue.get_nowait()
                if msg["type"] == "data":
                    self._update_host_select(msg.get("hosts", []), state)
                    self._update_plots(
                        msg["records"], msg.get("annotations", []), state
                    )
                    self._update_stats(msg["records"], state)
                    self._update_alerts(state)
                elif msg["type"] == "error":
                    if state.status_label:
                        state.status_label.set_text(
                            f"Error: {msg['message']}"
                        )
        except queue.Empty:
            pass

    def _update_host_select(self, hosts: list[str], state: "_ClientState"):
        if not hosts or state.host_select is None:
            return
        if self._host:
            hosts = [h for h in hosts if h == self._host] or hosts
        state.host_select.set_options({h: h for h in hosts})
        if not state.host or state.host not in hosts:
            default = self._host if self._host in hosts else hosts[0]
            state.host = default
            state.host_select.set_value(default)
            self._trigger_refresh(state)

    def _update_plots(self, records: list[dict],
                      annotations: list[dict],
                      state: "_ClientState"):
        state.last_records     = records
        state.last_annotations = annotations

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        db_size = _db_size_str(self._db_path)

        if state.status_label:
            if records:
                hop_count   = len({r["hop_number"] for r in records})
                pts_per_hop = len(records) // hop_count if hop_count else len(records)
                state.status_label.set_text(
                    f"Updated: {now_str}  |  "
                    f"{len(records)} points  ({pts_per_hop}/hop, {hop_count} hops)  |  "
                    f"DB: {db_size}"
                )
            else:
                state.status_label.set_text(
                    f"Updated: {now_str}  |  No data yet — is icon_db running?  |  "
                    f"DB: {db_size}"
                )

        if state.plots_container is None:
            return

        fig = (_build_figure(records, annotations, state.host)
               if records else self._empty_figure())

        if state.plot is None:
            with state.plots_container:
                state.plot = ui.plotly(fig).classes("w-full h-full")
        else:
            state.plot.update_figure(fig)

    def _update_stats(self, records: list[dict], state: "_ClientState"):
        if state.stats_rows is None or not records:
            return
        stats = _compute_stats(records)
        state.stats_rows.clear()
        with state.stats_rows:
            with ui.element("table").classes(
                "w-full text-sm text-gray-300 border-collapse"
            ):
                with ui.element("thead"):
                    with ui.element("tr").classes("border-b border-gray-600"):
                        for heading in ["Hop", "Host", "Samples",
                                        "Min (ms)", "Avg (ms)", "Max (ms)",
                                        "Loss"]:
                            ui.element("th").classes(
                                "text-left px-3 py-1 text-gray-400"
                            ).text = heading
                with ui.element("tbody"):
                    for s in stats:
                        with ui.element("tr").classes(
                            "border-b border-gray-700 hover:bg-gray-800"
                        ):
                            for val in [s["hop"], s["host"], s["samples"],
                                        s["min_ms"], s["avg_ms"], s["max_ms"],
                                        s["loss_pct"]]:
                                ui.element("td").classes(
                                    "px-3 py-1"
                                ).text = str(val)

    def _update_alerts(self, state: "_ClientState"):
        if state.alert_rows is None:
            return
        lines = _load_recent_alerts(self._alert_log)
        state.alert_rows.clear()
        with state.alert_rows:
            if lines:
                for line in reversed(lines):
                    colour = "text-red-400" if "ALERT" in line else "text-green-400"
                    ui.label(line).classes(f"text-xs font-mono {colour}")
            else:
                ui.label("No alerts recorded.").classes(
                    "text-sm text-gray-500 italic"
                )

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def run(self):
        os.makedirs(self._config_folder, exist_ok=True)

        self._uio.info(f"Database path : {self._db_path}")
        self._uio.info(f"Target host   : {self._host}")
        self._uio.info(f"Look-back     : {self._hours} hours")

        @ui.page("/")
        def index():
            self._build_page()

        self._poller = _DBPoller(
            self._db_path,
            self._host or DEFAULT_HOST,
            self._hours,
            _gui_queue,
            self._stop_event,
        )
        self._poller.start()

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
            description="A tool that provides a GUI to the data collected "
                        "by icon_db.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("-d", "--debug",
                            action="store_true", help="Enable debugging.")
        parser.add_argument("-t", "--host",
                            help="Host to display (default: all hosts in DB).",
                            default=None, required=False)
        parser.add_argument("-p", "--poll_seconds", type=float,
                            help="Poll interval in seconds.")
        parser.add_argument("--hours", type=float, default=DEFAULT_HOURS,
                            help=f"Hours of data to display "
                                 f"(default: {DEFAULT_HOURS}).")
        parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                            help=f"TCP port for the web server "
                                 f"(default: {DEFAULT_PORT}).")
        parser.add_argument("--no_browser", action="store_true",
                            help="Do not open a browser on startup.")
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
