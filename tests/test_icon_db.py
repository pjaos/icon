"""Tests for icon.icon_db"""

import sqlite3
import shutil
import pytest

from unittest.mock import MagicMock, patch
from icon.icon_db import (
    DEFAULT_HOST,
    DEFAULT_POLL_SECONDS,
    DEFAULT_MAX_DAYS,
    DB_FILENAME,
    ALERT_LOG_FILENAME,
    get_db_path,
    get_alert_log_path,
    open_db,
    _ensure_schema,
    _hops_ever_replied,
    _parse_traceroute,
    save_traceroute,
    purge_old_data,
    AlertState,
    IConDB,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_db() -> sqlite3.Connection:
    """In-memory SQLite database with the full schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


@pytest.fixture
def uio() -> MagicMock:
    """Silent mock UIO so debug/info/warn output is suppressed during tests."""
    return MagicMock()


# Realistic traceroute output (8 hops, hops 2/4/7 non-responsive)
SAMPLE_TRACEROUTE = """\
traceroute to 8.8.8.8 (8.8.8.8), 30 hops max, 60 byte packets
 1  192.168.0.1  4.123 ms  5.456 ms  3.789 ms
 2  * * *
 3  80.255.193.198  20.161 ms  19.432 ms  21.543 ms
 4  * * *
 5  80.255.204.85  25.071 ms  24.832 ms  25.310 ms
 6  213.104.85.174  24.333 ms  23.991 ms  24.675 ms
 7  * * *
 8  8.8.8.8  23.401 ms  22.876 ms  24.123 ms
"""

UNREACHABLE_TRACEROUTE = """\
traceroute to 8.8.8.8 (8.8.8.8), 30 hops max, 60 byte packets
 1  192.168.0.1  4.0 ms  4.1 ms  4.0 ms
 2  * * *
 3  * * *
"""

PARTIAL_REPLY_TRACEROUTE = """\
traceroute to 8.8.8.8 (8.8.8.8), 30 hops max, 60 byte packets
 1  192.168.0.1  4.0 ms  * 5.0 ms
 2  8.8.8.8  20.0 ms  21.0 ms  22.0 ms
"""


# ---------------------------------------------------------------------------
# get_db_path / get_alert_log_path
# ---------------------------------------------------------------------------

class TestGetPaths:
    def test_get_db_path_joins_folder_and_filename(self, tmp_path):
        result = get_db_path(str(tmp_path))
        assert result == str(tmp_path / DB_FILENAME)

    def test_get_alert_log_path_joins_folder_and_filename(self, tmp_path):
        result = get_alert_log_path(str(tmp_path))
        assert result == str(tmp_path / ALERT_LOG_FILENAME)


# ---------------------------------------------------------------------------
# open_db / _ensure_schema
# ---------------------------------------------------------------------------

class TestOpenDb:
    def test_creates_file(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = open_db(db_path)
        conn.close()
        assert (tmp_path / "test.db").exists()

    def test_schema_tables_exist(self, mem_db):
        tables = {
            row[0] for row in
            mem_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "traceroute_runs" in tables
        assert "hop_results" in tables
        assert "annotations" in tables

    def test_hop_results_has_all_columns(self, mem_db):
        cols = {row[1] for row in mem_db.execute("PRAGMA table_info(hop_results)")}
        for col in ("min_ms", "max_ms", "probe_count", "reply_count"):
            assert col in cols

    def test_schema_is_idempotent(self, mem_db):
        _ensure_schema(mem_db)
        tables = [
            row[0] for row in
            mem_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        assert tables.count("traceroute_runs") == 1
        assert tables.count("hop_results") == 1
        assert tables.count("annotations") == 1

    def test_timestamp_index_exists(self, mem_db):
        indices = {
            row[0] for row in
            mem_db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_runs_timestamp" in indices

    def test_migration_adds_missing_columns(self, tmp_path):
        """A pre-existing DB without new columns is migrated transparently."""
        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE traceroute_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                host TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE hop_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                hop_number INTEGER NOT NULL,
                hop_host TEXT,
                avg_ms REAL
            )
        """)
        conn.commit()
        conn.close()

        conn = open_db(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(hop_results)")}
        conn.close()
        for col in ("min_ms", "max_ms", "probe_count", "reply_count"):
            assert col in cols


# ---------------------------------------------------------------------------
# _parse_traceroute
# ---------------------------------------------------------------------------

class TestParseTraceroute:
    def test_correct_hop_count(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        assert len(hops) == 8

    def test_hop_numbers_are_sequential(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        assert [h["hop_number"] for h in hops] == list(range(1, 9))

    def test_responsive_hop_has_rtt_values(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        hop1 = hops[0]
        assert hop1["hop_host"] == "192.168.0.1"
        assert hop1["avg_ms"] == pytest.approx((4.123 + 5.456 + 3.789) / 3, rel=1e-3)
        assert hop1["min_ms"] == pytest.approx(3.789, rel=1e-3)
        assert hop1["max_ms"] == pytest.approx(5.456, rel=1e-3)

    def test_silent_hop_has_null_rtt(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        hop2 = hops[1]
        assert hop2["hop_number"] == 2
        assert hop2["avg_ms"] is None
        assert hop2["min_ms"] is None
        assert hop2["max_ms"] is None
        assert hop2["hop_host"] is None

    def test_silent_hop_has_zero_reply_count(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        hop2 = hops[1]  # * * *
        assert hop2["reply_count"] == 0
        assert hop2["probe_count"] == 3

    def test_destination_hop_rtt(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        last = hops[-1]
        assert last["hop_host"] == "8.8.8.8"
        assert last["avg_ms"]  == pytest.approx((23.401 + 22.876 + 24.123) / 3, rel=1e-3)
        assert last["min_ms"]  == pytest.approx(22.876, rel=1e-3)
        assert last["max_ms"]  == pytest.approx(24.123, rel=1e-3)

    def test_full_reply_hop_has_correct_counts(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        hop1 = hops[0]
        assert hop1["probe_count"] == 3
        assert hop1["reply_count"] == 3

    def test_min_lt_avg_lt_max(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        for hop in hops:
            if hop["avg_ms"] is not None:
                assert hop["min_ms"] <= hop["avg_ms"] <= hop["max_ms"]

    def test_partial_reply_hop(self, uio):
        hops = _parse_traceroute(PARTIAL_REPLY_TRACEROUTE, uio)
        hop1 = hops[0]
        assert hop1["avg_ms"]     == pytest.approx((4.0 + 5.0) / 2, rel=1e-3)
        assert hop1["min_ms"]     == pytest.approx(4.0, rel=1e-3)
        assert hop1["max_ms"]     == pytest.approx(5.0, rel=1e-3)
        assert hop1["reply_count"] == 2
        assert hop1["probe_count"] == 3

    def test_empty_output_returns_empty_list(self, uio):
        assert _parse_traceroute("", uio) == []

    def test_header_line_is_ignored(self, uio):
        hops = _parse_traceroute(SAMPLE_TRACEROUTE, uio)
        assert all(isinstance(h["hop_number"], int) for h in hops)


# ---------------------------------------------------------------------------
# _hops_ever_replied
# ---------------------------------------------------------------------------

class TestHopsEverReplied:
    def _seed(self, conn, host, hop_number, avg_ms):
        conn.execute(
            "INSERT INTO traceroute_runs (timestamp, host) VALUES ('2026-01-01 00:00:00', ?)",
            (host,),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO hop_results (run_id, hop_number, hop_host, avg_ms) VALUES (?,?,?,?)",
            (run_id, hop_number, None, avg_ms),
        )
        conn.commit()

    def test_empty_list_returns_empty_set(self, mem_db):
        assert _hops_ever_replied(mem_db, "8.8.8.8", []) == set()

    def test_hop_with_reply_is_returned(self, mem_db):
        self._seed(mem_db, "8.8.8.8", 1, 10.0)
        assert _hops_ever_replied(mem_db, "8.8.8.8", [1]) == {1}

    def test_hop_with_only_nulls_is_not_returned(self, mem_db):
        self._seed(mem_db, "8.8.8.8", 2, None)
        assert _hops_ever_replied(mem_db, "8.8.8.8", [2]) == set()

    def test_different_host_is_not_matched(self, mem_db):
        self._seed(mem_db, "1.1.1.1", 1, 10.0)
        assert _hops_ever_replied(mem_db, "8.8.8.8", [1]) == set()

    def test_mixed_hops_returns_only_replied(self, mem_db):
        self._seed(mem_db, "8.8.8.8", 1, 10.0)
        self._seed(mem_db, "8.8.8.8", 2, None)
        self._seed(mem_db, "8.8.8.8", 3, 30.0)
        result = _hops_ever_replied(mem_db, "8.8.8.8", [1, 2, 3])
        assert result == {1, 3}


# ---------------------------------------------------------------------------
# save_traceroute
# ---------------------------------------------------------------------------

class TestSaveTraceroute:
    def _make_hop(self, hop_number, hop_host=None, avg_ms=10.0,
                  min_ms=8.0, max_ms=12.0, probe_count=3, reply_count=3):
        return {
            "hop_number":  hop_number,
            "hop_host":    hop_host,
            "avg_ms":      avg_ms,
            "min_ms":      min_ms,
            "max_ms":      max_ms,
            "probe_count": probe_count,
            "reply_count": reply_count,
        }

    def _make_silent_hop(self, hop_number):
        return {
            "hop_number":  hop_number,
            "hop_host":    None,
            "avg_ms":      None,
            "min_ms":      None,
            "max_ms":      None,
            "probe_count": 3,
            "reply_count": 0,
        }

    def test_returns_run_id_and_count(self, mem_db):
        hops = [self._make_hop(1), self._make_hop(2)]
        run_id, count = save_traceroute(mem_db, "8.8.8.8", hops)
        assert isinstance(run_id, int)
        assert count == 2

    def test_run_row_is_inserted(self, mem_db):
        save_traceroute(mem_db, "8.8.8.8", [self._make_hop(1)])
        row = mem_db.execute("SELECT host FROM traceroute_runs").fetchone()
        assert row["host"] == "8.8.8.8"

    def test_hop_values_are_persisted(self, mem_db):
        hops = [self._make_hop(1, hop_host="192.168.0.1",
                               avg_ms=10.0, min_ms=8.0, max_ms=12.0,
                               probe_count=3, reply_count=2)]
        save_traceroute(mem_db, "8.8.8.8", hops)
        row = mem_db.execute("SELECT * FROM hop_results").fetchone()
        assert row["hop_number"]  == 1
        assert row["hop_host"]    == "192.168.0.1"
        assert row["avg_ms"]      == pytest.approx(10.0)
        assert row["min_ms"]      == pytest.approx(8.0)
        assert row["max_ms"]      == pytest.approx(12.0)
        assert row["probe_count"] == 3
        assert row["reply_count"] == 2

    def test_always_silent_hop_is_skipped_on_first_run(self, mem_db):
        hops = [self._make_hop(1), self._make_silent_hop(2)]
        _, count = save_traceroute(mem_db, "8.8.8.8", hops)
        assert count == 1
        rows = mem_db.execute("SELECT hop_number FROM hop_results").fetchall()
        assert [r[0] for r in rows] == [1]

    def test_previously_seen_silent_hop_is_saved(self, mem_db):
        save_traceroute(mem_db, "8.8.8.8",
                        [self._make_hop(1), self._make_hop(2)])
        _, count = save_traceroute(mem_db, "8.8.8.8",
                                   [self._make_hop(1), self._make_silent_hop(2)])
        assert count == 2
        null_rows = mem_db.execute(
            "SELECT hop_number FROM hop_results WHERE avg_ms IS NULL"
        ).fetchall()
        assert len(null_rows) == 1
        assert null_rows[0][0] == 2

    def test_multiple_runs_increment_run_id(self, mem_db):
        hops = [self._make_hop(1)]
        run_id_1, _ = save_traceroute(mem_db, "8.8.8.8", hops)
        run_id_2, _ = save_traceroute(mem_db, "8.8.8.8", hops)
        assert run_id_2 == run_id_1 + 1

    def test_different_hosts_are_independent(self, mem_db):
        save_traceroute(mem_db, "8.8.8.8", [self._make_hop(1)])
        save_traceroute(mem_db, "1.1.1.1", [self._make_hop(1)])
        runs = mem_db.execute("SELECT host FROM traceroute_runs").fetchall()
        hosts = {r[0] for r in runs}
        assert hosts == {"8.8.8.8", "1.1.1.1"}

    def test_empty_hops_list_still_inserts_run(self, mem_db):
        run_id, count = save_traceroute(mem_db, "8.8.8.8", [])
        assert count == 0
        row = mem_db.execute("SELECT id FROM traceroute_runs").fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# purge_old_data
# ---------------------------------------------------------------------------

class TestPurgeOldData:
    def _insert_run(self, conn, host, timestamp):
        conn.execute(
            "INSERT INTO traceroute_runs (timestamp, host) VALUES (?, ?)",
            (timestamp, host),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO hop_results "
            "(run_id, hop_number, hop_host, avg_ms) VALUES (?,?,?,?)",
            (run_id, 1, None, 5.0),
        )
        conn.commit()
        return run_id

    def test_old_runs_are_deleted(self, mem_db, uio):
        from datetime import datetime, timezone, timedelta
        old_ts  = (datetime.now(timezone.utc) - timedelta(days=40)) \
                      .strftime("%Y-%m-%d %H:%M:%S")
        self._insert_run(mem_db, "8.8.8.8", old_ts)
        deleted = purge_old_data(mem_db, max_days=30, uio=uio)
        assert deleted == 1
        assert mem_db.execute(
            "SELECT COUNT(*) FROM traceroute_runs"
        ).fetchone()[0] == 0

    def test_recent_runs_are_kept(self, mem_db, uio):
        from datetime import datetime, timezone, timedelta
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=5)) \
                        .strftime("%Y-%m-%d %H:%M:%S")
        self._insert_run(mem_db, "8.8.8.8", recent_ts)
        deleted = purge_old_data(mem_db, max_days=30, uio=uio)
        assert deleted == 0
        assert mem_db.execute(
            "SELECT COUNT(*) FROM traceroute_runs"
        ).fetchone()[0] == 1

    def test_hop_results_are_also_deleted(self, mem_db, uio):
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)) \
                     .strftime("%Y-%m-%d %H:%M:%S")
        self._insert_run(mem_db, "8.8.8.8", old_ts)
        purge_old_data(mem_db, max_days=30, uio=uio)
        assert mem_db.execute(
            "SELECT COUNT(*) FROM hop_results"
        ).fetchone()[0] == 0

    def test_returns_count_of_deleted_runs(self, mem_db, uio):
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)) \
                     .strftime("%Y-%m-%d %H:%M:%S")
        for _ in range(3):
            self._insert_run(mem_db, "8.8.8.8", old_ts)
        deleted = purge_old_data(mem_db, max_days=30, uio=uio)
        assert deleted == 3

    def test_zero_returned_when_nothing_to_purge(self, mem_db, uio):
        deleted = purge_old_data(mem_db, max_days=30, uio=uio)
        assert deleted == 0


# ---------------------------------------------------------------------------
# AlertState
# ---------------------------------------------------------------------------

class TestAlertState:
    def _make_hop(self, avg_ms, reply_count=3, probe_count=3):
        return {
            "hop_number":  1,
            "hop_host":    "8.8.8.8",
            "avg_ms":      avg_ms,
            "probe_count": probe_count,
            "reply_count": reply_count,
        }

    def test_alert_written_on_rtt_breach(self, tmp_path, uio):
        alert_log = str(tmp_path / "alerts.log")
        state = AlertState()
        state.check([self._make_hop(200.0)], "8.8.8.8",
                    alert_rtt=100.0, alert_loss=None,
                    alert_log=alert_log, uio=uio)
        content = open(alert_log).read()
        assert "ALERT" in content

    def test_recovery_written_on_rtt_recovery(self, tmp_path, uio):
        alert_log = str(tmp_path / "alerts.log")
        state = AlertState()
        # First call: enters alert
        state.check([self._make_hop(200.0)], "8.8.8.8",
                    alert_rtt=100.0, alert_loss=None,
                    alert_log=alert_log, uio=uio)
        # Second call: recovers
        state.check([self._make_hop(20.0)], "8.8.8.8",
                    alert_rtt=100.0, alert_loss=None,
                    alert_log=alert_log, uio=uio)
        content = open(alert_log).read()
        assert "RECOVERED" in content

    def test_no_duplicate_alert_while_sustained(self, tmp_path, uio):
        alert_log = str(tmp_path / "alerts.log")
        state = AlertState()
        for _ in range(3):
            state.check([self._make_hop(200.0)], "8.8.8.8",
                        alert_rtt=100.0, alert_loss=None,
                        alert_log=alert_log, uio=uio)
        lines = [l for l in open(alert_log).read().splitlines() if l]
        assert len(lines) == 1  # only one ALERT entry

    def test_alert_written_on_loss_breach(self, tmp_path, uio):
        alert_log = str(tmp_path / "alerts.log")
        state = AlertState()
        # 1 reply out of 3 probes = 66.7% loss
        state.check([self._make_hop(20.0, reply_count=1, probe_count=3)],
                    "8.8.8.8",
                    alert_rtt=None, alert_loss=50.0,
                    alert_log=alert_log, uio=uio)
        content = open(alert_log).read()
        assert "ALERT" in content

    def test_no_alert_when_below_thresholds(self, tmp_path, uio):
        alert_log = str(tmp_path / "alerts.log")
        state = AlertState()
        state.check([self._make_hop(20.0)], "8.8.8.8",
                    alert_rtt=100.0, alert_loss=50.0,
                    alert_log=alert_log, uio=uio)
        import os
        assert not os.path.exists(alert_log)

    def test_unreachable_destination_triggers_alert(self, tmp_path, uio):
        alert_log = str(tmp_path / "alerts.log")
        state = AlertState()
        state.check([self._make_hop(None)], "8.8.8.8",
                    alert_rtt=100.0, alert_loss=None,
                    alert_log=alert_log, uio=uio)
        content = open(alert_log).read()
        assert "ALERT" in content
        assert "unreachable" in content


# ---------------------------------------------------------------------------
# IConDB._check_traceroute
# ---------------------------------------------------------------------------

class TestCheckTraceroute:
    def test_passes_when_traceroute_found(self):
        with patch("shutil.which", return_value="/usr/bin/traceroute"):
            IConDB._check_traceroute()

    def test_raises_when_traceroute_missing(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="traceroute"):
                IConDB._check_traceroute()


# ---------------------------------------------------------------------------
# run_traceroute (subprocess integration — mocked)
# ---------------------------------------------------------------------------

class TestRunTraceroute:
    def test_returns_hops_on_success(self, uio):
        mock_result = MagicMock()
        mock_result.stdout = SAMPLE_TRACEROUTE
        with patch("subprocess.run", return_value=mock_result):
            hops = __import__(
                "icon.icon_db", fromlist=["run_traceroute"]
            ).run_traceroute("8.8.8.8", uio)
        assert len(hops) == 8

    def test_returns_empty_list_when_not_found(self, uio):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            from icon.icon_db import run_traceroute
            result = run_traceroute("8.8.8.8", uio)
        assert result == []

    def test_returns_empty_list_on_timeout(self, uio):
        import subprocess
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(
                       cmd="traceroute", timeout=120)):
            from icon.icon_db import run_traceroute
            result = run_traceroute("8.8.8.8", uio)
        assert result == []


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_host(self):
        assert DEFAULT_HOST == "8.8.8.8"

    def test_default_poll_seconds(self):
        assert DEFAULT_POLL_SECONDS == 2.0

    def test_default_max_days(self):
        assert DEFAULT_MAX_DAYS == 30

    def test_db_filename(self):
        assert DB_FILENAME == "icon.db"

    def test_alert_log_filename(self):
        assert ALERT_LOG_FILENAME == "alerts.log"
