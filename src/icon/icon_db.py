#!/usr/bin/env python3

import argparse
import subprocess
import re
import sqlite3
import time
import os
import shutil
from datetime import datetime, timezone, timedelta

from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.boot_manager import BootManager
from p3lib.helper import get_app_data_path
from p3lib.helper import get_program_version

MODULE_NAME          = "icon"

DEFAULT_HOST         = "8.8.8.8"
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_MAX_DAYS     = 30
DB_FILENAME          = "icon.db"
ALERT_LOG_FILENAME   = "alerts.log"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_path(config_folder: str) -> str:
    return os.path.join(config_folder, DB_FILENAME)


def get_alert_log_path(config_folder: str) -> str:
    return os.path.join(config_folder, ALERT_LOG_FILENAME)


def open_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database and ensure the schema exists."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS traceroute_runs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT    NOT NULL,
            host      TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hop_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER NOT NULL REFERENCES traceroute_runs(id),
            hop_number  INTEGER NOT NULL,
            hop_host    TEXT,
            avg_ms      REAL,
            min_ms      REAL,
            max_ms      REAL,
            probe_count INTEGER NOT NULL DEFAULT 3,
            reply_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS annotations (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            host      TEXT NOT NULL,
            note      TEXT NOT NULL
        )
    """)
    # Create index on timestamp for efficient retention purges
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_runs_timestamp
        ON traceroute_runs(timestamp)
    """)
    # Migrate existing databases
    existing = {row[1] for row in conn.execute("PRAGMA table_info(hop_results)")}
    migrations = {
        "min_ms":      "REAL",
        "max_ms":      "REAL",
        "probe_count": "INTEGER NOT NULL DEFAULT 3",
        "reply_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for col, col_type in migrations.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE hop_results ADD COLUMN {col} {col_type}")
    conn.commit()


def purge_old_data(conn: sqlite3.Connection, max_days: int, uio: UIO) -> int:
    """Delete traceroute runs (and their hop results) older than max_days.

    Returns the number of runs deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)) \
                .strftime("%Y-%m-%d %H:%M:%S")
    # Delete hop_results for old runs first (no CASCADE on SQLite by default)
    conn.execute("""
        DELETE FROM hop_results
        WHERE run_id IN (
            SELECT id FROM traceroute_runs WHERE timestamp < ?
        )
    """, (cutoff,))
    cur = conn.execute(
        "DELETE FROM traceroute_runs WHERE timestamp < ?", (cutoff,)
    )
    deleted = cur.rowcount
    conn.commit()
    if deleted:
        uio.info(f"  Retention: purged {deleted} runs older than {max_days} days.")
    return deleted


def _hops_ever_replied(conn: sqlite3.Connection,
                       host: str,
                       hop_numbers: list[int]) -> set[int]:
    """Return the subset of hop_numbers that have at least one non-NULL avg_ms
    recorded in the DB for this host."""
    if not hop_numbers:
        return set()
    placeholders = ",".join("?" * len(hop_numbers))
    rows = conn.execute(
        f"""
        SELECT DISTINCT h.hop_number
        FROM   hop_results h
        JOIN   traceroute_runs r ON h.run_id = r.id
        WHERE  r.host = ?
          AND  h.hop_number IN ({placeholders})
          AND  h.avg_ms IS NOT NULL
        """,
        [host, *hop_numbers],
    ).fetchall()
    return {row[0] for row in rows}


def save_traceroute(conn: sqlite3.Connection,
                    host: str,
                    hops: list[dict]) -> tuple[int, int]:
    """Persist one traceroute result, skipping hops that have never replied.

    hops is a list of dicts with keys:
        hop_number   int
        hop_host     str | None
        avg_ms       float | None
        min_ms       float | None
        max_ms       float | None
        probe_count  int
        reply_count  int

    Returns (run_id, saved_hop_count).
    """
    silent_hop_nums = [h["hop_number"] for h in hops if h.get("avg_ms") is None]
    previously_seen = _hops_ever_replied(conn, host, silent_hop_nums)

    hops_to_save = [
        h for h in hops
        if h.get("avg_ms") is not None
        or h["hop_number"] in previously_seen
    ]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO traceroute_runs (timestamp, host) VALUES (?, ?)",
        (ts, host),
    )
    run_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO hop_results
           (run_id, hop_number, hop_host, avg_ms, min_ms, max_ms,
            probe_count, reply_count)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(run_id, h["hop_number"], h.get("hop_host"),
          h.get("avg_ms"), h.get("min_ms"), h.get("max_ms"),
          h.get("probe_count", 3), h.get("reply_count", 0))
         for h in hops_to_save],
    )
    conn.commit()
    return run_id, len(hops_to_save)


# ---------------------------------------------------------------------------
# Traceroute runner & parser
# ---------------------------------------------------------------------------

# Number of probes sent per hop — must match the -q argument below
PROBE_COUNT = 3

def run_traceroute(host: str, uio: UIO) -> list[dict]:
    """Run the system traceroute command and return a list of hop dicts."""
    try:
        result = subprocess.run(
            ["traceroute", "-n", "-w", "2", "-q", str(PROBE_COUNT), host],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout
    except FileNotFoundError:
        uio.error("traceroute command not found. Install it with: sudo apt install traceroute")
        return []
    except subprocess.TimeoutExpired:
        uio.warn("traceroute timed out.")
        return []

    return _parse_traceroute(output, uio)


_HOP_RE  = re.compile(r"^\s*(?P<hop>\d+)\s+(?P<rest>.+)$")
_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ms")


def _parse_traceroute(output: str, uio: UIO) -> list[dict]:
    hops = []
    for line in output.splitlines():
        m = _HOP_RE.match(line)
        if not m:
            continue
        hop_number = int(m.group("hop"))
        rest       = m.group("rest").strip()

        times = [float(t) for t in _TIME_RE.findall(rest)]

        hop_host = None
        for token in rest.split():
            if token != "*" and not _TIME_RE.match(token) and token != "ms":
                hop_host = token
                break

        avg_ms = (sum(times) / len(times)) if times else None
        min_ms = min(times) if times else None
        max_ms = max(times) if times else None

        # Count asterisks to determine how many probes got no reply
        star_count  = rest.count("*")
        reply_count = PROBE_COUNT - star_count

        hops.append({
            "hop_number":  hop_number,
            "hop_host":    hop_host,
            "avg_ms":      avg_ms,
            "min_ms":      min_ms,
            "max_ms":      max_ms,
            "probe_count": PROBE_COUNT,
            "reply_count": reply_count,
        })

        uio.debug(
            f"  hop {hop_number:>2}  host={hop_host or '?':>15}  "
            f"min={min_ms:.2f} avg={avg_ms:.2f} max={max_ms:.2f} ms  "
            f"loss={100*(PROBE_COUNT-reply_count)/PROBE_COUNT:.0f}%"
            if avg_ms is not None
            else f"  hop {hop_number:>2}  host={hop_host or '?':>15}  "
                 f"avg=* (no reply)  loss=100%"
        )

    return hops


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

class AlertState:
    """Tracks per-hop alert state to debounce transitions."""

    def __init__(self):
        # Maps hop_number -> True if currently in alert state
        self._alerted: dict[int, bool] = {}

    def check(self,
              hops:       list[dict],
              host:       str,
              alert_rtt:  float | None,
              alert_loss: float | None,
              alert_log:  str,
              uio:        UIO) -> None:
        """Evaluate thresholds and log transitions only (ok→alert, alert→ok)."""
        dest = hops[-1] if hops else None
        if dest is None:
            return

        hop_num     = dest["hop_number"]
        avg_ms      = dest.get("avg_ms")
        probe_count = dest.get("probe_count", PROBE_COUNT)
        reply_count = dest.get("reply_count", 0)
        loss_pct    = 100.0 * (probe_count - reply_count) / probe_count \
                      if probe_count else 100.0

        in_alert = False
        reasons  = []

        if alert_rtt is not None and avg_ms is not None and avg_ms > alert_rtt:
            in_alert = True
            reasons.append(f"RTT {avg_ms:.1f} ms > threshold {alert_rtt} ms")

        if alert_loss is not None and loss_pct > alert_loss:
            in_alert = True
            reasons.append(f"loss {loss_pct:.0f}% > threshold {alert_loss:.0f}%")

        if avg_ms is None:
            in_alert = True
            reasons.append("destination unreachable")

        was_alerted = self._alerted.get(hop_num, False)

        if in_alert and not was_alerted:
            _write_alert(alert_log, host, hop_num,
                         f"ALERT: {'; '.join(reasons)}", uio)
        elif not in_alert and was_alerted:
            _write_alert(alert_log, host, hop_num, "RECOVERED", uio)

        self._alerted[hop_num] = in_alert


def _write_alert(alert_log: str, host: str, hop_num: int,
                 message: str, uio: UIO) -> None:
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  host={host}  hop={hop_num}  {message}\n"
    try:
        with open(alert_log, "a") as f:
            f.write(line)
        uio.warn(line.rstrip())
    except OSError as e:
        uio.error(f"Could not write alert log: {e}")


# ---------------------------------------------------------------------------
# Main application class
# ---------------------------------------------------------------------------

class IConDB(object):
    PROGRAM_NAME = "icon"

    def __init__(self, uio: UIO, options):
        """@brief Constructor
           @param uio A UIO instance handling user input and output
           @param options Parsed command-line options"""
        self._uio = uio
        self._options = options
        self._config_folder = get_app_data_path(MODULE_NAME)
        os.makedirs(self._config_folder, exist_ok=True)

    @staticmethod
    def _check_traceroute():
        """Raise RuntimeError if the traceroute binary cannot be found on PATH."""
        if shutil.which("traceroute") is None:
            raise RuntimeError(
                "'traceroute' was not found on PATH. "
                "Install it with:  sudo apt install traceroute"
            )

    def run(self):
        self._check_traceroute()
        host         = self._options.host
        poll_seconds = max(1.0, self._options.poll_seconds)
        if poll_seconds != self._options.poll_seconds:
            self._uio.warn("poll_seconds raised to minimum of 1.0s")
        max_days     = self._options.max_days
        alert_rtt    = self._options.alert_rtt
        alert_loss   = self._options.alert_loss
        alert_log    = get_alert_log_path(self._config_folder)

        db_path = get_db_path(self._config_folder)
        self._uio.info(f"Database     : {db_path}")
        self._uio.info(f"Target host  : {host}  |  Poll interval: {poll_seconds}s")
        self._uio.info(f"Retention    : {max_days} days")
        if alert_rtt:
            self._uio.info(f"Alert RTT    : > {alert_rtt} ms")
        if alert_loss:
            self._uio.info(f"Alert loss   : > {alert_loss}%")

        conn        = open_db(db_path)
        alert_state = AlertState()

        try:
            while True:
                # Purge old data at the start of each cycle
                purge_old_data(conn, max_days, self._uio)

                self._uio.info(f"Running traceroute to {host} …")
                hops = run_traceroute(host, self._uio)

                if hops:
                    run_id, saved = save_traceroute(conn, host, hops)
                    reachable = [h for h in hops if h["avg_ms"] is not None]
                    skipped   = len(hops) - saved
                    self._uio.info(
                        f"  Saved run #{run_id}: {saved} hops stored "
                        f"({len(reachable)} reachable"
                        + (f", {skipped} always-silent hops skipped" if skipped else "")
                        + ")."
                    )
                    last = hops[-1]
                    if last["hop_host"] and last["avg_ms"] is not None:
                        loss = 100 * (last["probe_count"] - last["reply_count"]) \
                               / last["probe_count"]
                        self._uio.info(
                            f"  Destination {last['hop_host']} reachable, "
                            f"avg RTT = {last['avg_ms']:.2f} ms  "
                            f"loss = {loss:.0f}%"
                        )
                    else:
                        self._uio.warn(f"  Destination {host} NOT reachable.")

                    if alert_rtt or alert_loss:
                        alert_state.check(hops, host, alert_rtt, alert_loss,
                                          alert_log, self._uio)
                else:
                    self._uio.warn("  traceroute returned no hops.")

                self._uio.info(f"  Sleeping {poll_seconds}s …")
                time.sleep(poll_seconds)

        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """@brief Program entry point"""
    uio = UIO()
    prog_version = get_program_version(IConDB.PROGRAM_NAME)
    uio.info(f"{IConDB.PROGRAM_NAME}: V{prog_version}")

    options = None
    try:
        parser = argparse.ArgumentParser(
            description="A tool that repeatedly performs checks on internet "
                        "connectivity and stores the data to a local sqlite database.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("-d", "--debug",
                            action="store_true", help="Enable debugging.")
        parser.add_argument("-t", "--host",
                            help=f"Target host for traceroute (default: {DEFAULT_HOST}).",
                            default=DEFAULT_HOST, required=False)
        parser.add_argument("-p", "--poll_seconds",
                            type=float, default=DEFAULT_POLL_SECONDS,
                            help=f"Poll interval in seconds "
                                 f"(default: {DEFAULT_POLL_SECONDS}, minimum: 1).")
        parser.add_argument("--max_days",
                            type=int, default=DEFAULT_MAX_DAYS,
                            help=f"Delete data older than this many days "
                                 f"(default: {DEFAULT_MAX_DAYS}).")
        parser.add_argument("--alert_rtt",
                            type=float, default=None,
                            help="Alert when destination RTT exceeds this value in ms.")
        parser.add_argument("--alert_loss",
                            type=float, default=None,
                            help="Alert when destination packet loss exceeds this "
                                 "percentage (0-100).")
        BootManager.AddCmdArgs(parser)

        options = parser.parse_args()
        uio.enableDebug(options.debug)

        handled = BootManager.HandleOptions(uio, options, False)
        if not handled:
            aClass = IConDB(uio, options)
            aClass.run()

    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logTraceBack(uio)
        if options is not None and options.debug:
            raise
        else:
            uio.error(str(ex))


if __name__ == "__main__":
    main()
