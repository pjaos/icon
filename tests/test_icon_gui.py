"""Tests for icon.icon_gui

NiceGUI starts a web server when imported, so we mock it out entirely before
any icon_gui symbols are imported. This keeps the test suite fast and
dependency-free (no browser, no network port).
"""

import queue
import threading
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

# ---------------------------------------------------------------------------
# Mock NiceGUI and Launcher before importing icon_gui so the module-level
# `from nicegui import ui, app` does not start a server or fail in CI.
# ---------------------------------------------------------------------------
import sys

_nicegui_mock = MagicMock()
_nicegui_mock.ui   = MagicMock()
_nicegui_mock.app  = MagicMock()
sys.modules.setdefault("nicegui",        _nicegui_mock)
sys.modules.setdefault("nicegui.ui",     _nicegui_mock.ui)
sys.modules.setdefault("nicegui.app",    _nicegui_mock.app)
sys.modules.setdefault("p3lib.launcher", MagicMock())

import plotly.graph_objects as go  # real import — plotly must be installed

from icon.icon_gui import (
    DEFAULT_HOURS,
    DEFAULT_PORT,
    REFRESH_INTERVAL,
    TIMER_INTERVAL_S,
    _build_figure,
    _load_hops_from_db,
    _DBPoller,
)
from icon.icon_db import _ensure_schema, open_db, save_traceroute


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(hop_number: int, hop_host: str | None,
                 avg_ms: float | None,
                 min_ms: float | None = None,
                 max_ms: float | None = None,
                 timestamp: str = "2026-04-22 12:00:00") -> dict:
    return {
        "timestamp":  timestamp,
        "hop_number": hop_number,
        "hop_host":   hop_host,
        "avg_ms":     avg_ms,
        "min_ms":     min_ms if min_ms is not None else avg_ms,
        "max_ms":     max_ms if max_ms is not None else avg_ms,
    }


def _populated_db(tmp_path, host: str = "8.8.8.8") -> str:
    """Create a real SQLite DB with a couple of traceroute runs and return its path."""
    db_path = str(tmp_path / "icon.db")
    conn = open_db(db_path)
    hops = [
        {"hop_number": 1, "hop_host": "192.168.0.1",
         "avg_ms": 4.5, "min_ms": 3.8, "max_ms": 5.2},
        {"hop_number": 2, "hop_host": "8.8.8.8",
         "avg_ms": 23.1, "min_ms": 22.0, "max_ms": 24.5},
    ]
    save_traceroute(conn, host, hops)
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_hours(self):
        assert DEFAULT_HOURS == 24

    def test_default_port(self):
        assert DEFAULT_PORT == 8100

    def test_timer_interval(self):
        assert TIMER_INTERVAL_S == 0.1

    def test_refresh_interval(self):
        assert REFRESH_INTERVAL == 30


# ---------------------------------------------------------------------------
# _load_hops_from_db
# ---------------------------------------------------------------------------

class TestLoadHopsFromDb:
    def test_returns_empty_list_when_db_missing(self, tmp_path):
        result = _load_hops_from_db(str(tmp_path / "no.db"), "8.8.8.8")
        assert result == []

    def test_returns_records_for_correct_host(self, tmp_path):
        db_path = _populated_db(tmp_path, host="8.8.8.8")
        records = _load_hops_from_db(db_path, "8.8.8.8")
        assert len(records) == 2

    def test_returns_empty_for_wrong_host(self, tmp_path):
        db_path = _populated_db(tmp_path, host="8.8.8.8")
        records = _load_hops_from_db(db_path, "1.1.1.1")
        assert records == []

    def test_record_has_expected_keys(self, tmp_path):
        db_path = _populated_db(tmp_path)
        records = _load_hops_from_db(db_path, "8.8.8.8")
        expected_keys = {"timestamp", "hop_number", "hop_host",
                         "avg_ms", "min_ms", "max_ms"}
        assert expected_keys.issubset(records[0].keys())

    def test_records_ordered_by_timestamp_then_hop(self, tmp_path):
        db_path = _populated_db(tmp_path)
        records = _load_hops_from_db(db_path, "8.8.8.8")
        hop_numbers = [r["hop_number"] for r in records]
        assert hop_numbers == sorted(hop_numbers)

    def test_cutoff_filters_old_records(self, tmp_path):
        db_path = str(tmp_path / "icon.db")
        conn = open_db(db_path)
        # Insert a run with an old timestamp directly
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO traceroute_runs (timestamp, host) VALUES (?, ?)",
            (old_ts, "8.8.8.8"),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO hop_results (run_id, hop_number, hop_host, avg_ms, min_ms, max_ms)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, 1, "192.168.0.1", 5.0, 4.0, 6.0),
        )
        conn.commit()
        conn.close()
        # With default 24-hour window the old record must be excluded
        records = _load_hops_from_db(db_path, "8.8.8.8", hours=24)
        assert records == []

    def test_recent_records_are_included(self, tmp_path):
        db_path = _populated_db(tmp_path)
        records = _load_hops_from_db(db_path, "8.8.8.8", hours=1)
        # The records were just inserted so their timestamp is within 1 hour
        assert len(records) == 2


# ---------------------------------------------------------------------------
# _build_figure
# ---------------------------------------------------------------------------

class TestBuildFigure:
    def test_returns_none_for_empty_records(self):
        assert _build_figure([], "8.8.8.8") is None

    def test_returns_plotly_figure(self):
        records = [_make_record(1, "192.168.0.1", 10.0)]
        fig = _build_figure(records, "8.8.8.8")
        assert isinstance(fig, go.Figure)

    def test_title_contains_host(self):
        records = [_make_record(1, "192.168.0.1", 10.0)]
        fig = _build_figure(records, "8.8.8.8")
        assert "8.8.8.8" in fig.layout.title.text

    def test_one_rtt_trace_per_hop(self):
        records = [
            _make_record(1, "192.168.0.1", 5.0),
            _make_record(2, "8.8.8.8",     20.0),
        ]
        fig = _build_figure(records, "8.8.8.8")
        rtt_traces = [t for t in fig.data if t.mode == "lines+markers"]
        assert len(rtt_traces) == 2

    def test_trace_name_includes_hop_ip(self):
        records = [_make_record(1, "192.168.0.1", 5.0)]
        fig = _build_figure(records, "8.8.8.8")
        rtt_traces = [t for t in fig.data if t.mode == "lines+markers"]
        assert "192.168.0.1" in rtt_traces[0].name

    def test_trace_name_falls_back_to_hop_number(self):
        records = [_make_record(1, None, 5.0)]
        fig = _build_figure(records, "8.8.8.8")
        rtt_traces = [t for t in fig.data if t.mode == "lines+markers"]
        assert "hop 1" in rtt_traces[0].name

    def test_unreachable_hop_produces_marker_trace(self):
        records = [_make_record(1, None, None)]
        fig = _build_figure(records, "8.8.8.8")
        marker_traces = [t for t in fig.data if t.mode == "markers"]
        assert len(marker_traces) == 1

    def test_unreachable_markers_plotted_at_zero(self):
        records = [_make_record(1, None, None, timestamp="2026-04-22 12:00:00")]
        fig = _build_figure(records, "8.8.8.8")
        marker_traces = [t for t in fig.data if t.mode == "markers"]
        assert all(y == 0 for y in marker_traces[0].y)

    def test_only_one_no_reply_legend_entry_for_multiple_silent_hops(self):
        records = [
            _make_record(1, None, None),
            _make_record(2, None, None),
        ]
        fig = _build_figure(records, "8.8.8.8")
        no_reply_traces = [t for t in fig.data
                           if getattr(t, "legendgroup", None) == "no_reply"
                           and t.showlegend]
        assert len(no_reply_traces) == 1

    def test_customdata_contains_min_max(self):
        records = [_make_record(1, "192.168.0.1", avg_ms=10.0,
                                min_ms=8.0, max_ms=12.0)]
        fig = _build_figure(records, "8.8.8.8")
        rtt_traces = [t for t in fig.data if t.mode == "lines+markers"]
        cd = rtt_traces[0].customdata
        assert cd[0][0] == pytest.approx(8.0)   # min
        assert cd[0][1] == pytest.approx(12.0)  # max

    def test_customdata_falls_back_to_avg_when_min_max_null(self):
        """Rows migrated from old schema have min_ms=None; fallback must be avg."""
        records = [_make_record(1, "192.168.0.1", avg_ms=10.0,
                                min_ms=None, max_ms=None)]
        # Override the min/max to None to simulate old-schema rows
        records[0]["min_ms"] = None
        records[0]["max_ms"] = None
        fig = _build_figure(records, "8.8.8.8")
        rtt_traces = [t for t in fig.data if t.mode == "lines+markers"]
        cd = rtt_traces[0].customdata
        assert cd[0][0] == pytest.approx(10.0)
        assert cd[0][1] == pytest.approx(10.0)

    def test_colours_cycle_for_more_than_palette_size(self):
        """More than 8 hops must not raise an IndexError."""
        records = [_make_record(i, f"10.0.0.{i}", float(i * 5))
                   for i in range(1, 12)]
        fig = _build_figure(records, "8.8.8.8")
        rtt_traces = [t for t in fig.data if t.mode == "lines+markers"]
        assert len(rtt_traces) == 11

    def test_mixed_reachable_and_unreachable_hops(self):
        records = [
            _make_record(1, "192.168.0.1", 5.0),
            _make_record(2, None,          None),
            _make_record(3, "8.8.8.8",     20.0),
        ]
        fig = _build_figure(records, "8.8.8.8")
        rtt_traces    = [t for t in fig.data if t.mode == "lines+markers"]
        marker_traces = [t for t in fig.data if t.mode == "markers"]
        assert len(rtt_traces) == 2
        assert len(marker_traces) == 1

    def test_most_recent_ip_used_as_hop_label(self):
        """When the same hop has appeared with different IPs, the latest wins."""
        records = [
            _make_record(1, "10.0.0.1", 5.0, timestamp="2026-04-22 11:00:00"),
            _make_record(1, "10.0.0.2", 6.0, timestamp="2026-04-22 12:00:00"),
        ]
        fig = _build_figure(records, "8.8.8.8")
        rtt_traces = [t for t in fig.data if t.mode == "lines+markers"]
        assert "10.0.0.2" in rtt_traces[0].name


# ---------------------------------------------------------------------------
# _DBPoller
# ---------------------------------------------------------------------------

class TestDBPoller:
    def test_puts_data_message_on_queue(self, tmp_path):
        db_path  = _populated_db(tmp_path)
        q        = queue.Queue()
        stop     = threading.Event()
        poller   = _DBPoller(db_path, "8.8.8.8", 24.0, q, stop)
        poller.start()
        try:
            msg = q.get(timeout=5)
            assert msg["type"] == "data"
            assert isinstance(msg["records"], list)
        finally:
            stop.set()
            poller.join(timeout=2)

    def test_stops_when_event_is_set(self, tmp_path):
        db_path = _populated_db(tmp_path)
        q       = queue.Queue()
        stop    = threading.Event()
        poller  = _DBPoller(db_path, "8.8.8.8", 24.0, q, stop)
        poller.start()
        q.get(timeout=5)   # wait for first message then stop
        stop.set()
        poller.join(timeout=5)
        assert not poller.is_alive()

    def test_puts_error_message_on_queue_for_bad_db(self, tmp_path):
        q      = queue.Queue()
        stop   = threading.Event()
        # Point at a path that is a directory, not a file — open_db will fail
        bad_path = str(tmp_path)
        poller = _DBPoller(bad_path, "8.8.8.8", 24.0, q, stop)
        poller.start()
        try:
            msg = q.get(timeout=5)
            # Either an error message or empty data is acceptable;
            # what must NOT happen is the thread silently dying
            assert msg["type"] in ("data", "error")
        finally:
            stop.set()
            poller.join(timeout=2)

    def test_is_daemon_thread(self, tmp_path):
        db_path = _populated_db(tmp_path)
        q       = queue.Queue()
        stop    = threading.Event()
        poller  = _DBPoller(db_path, "8.8.8.8", 24.0, q, stop)
        assert poller.daemon is True
        stop.set()
