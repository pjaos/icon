#!/usr/bin/env python3

import argparse
import subprocess
import re
import sqlite3
import time
import os
import shutil
from datetime import datetime, timezone

from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.boot_manager import BootManager
from p3lib.helper import get_app_data_path
from p3lib.helper import get_program_version

MODULE_NAME = "icon"

DEFAULT_HOST          = "8.8.8.8"
DEFAULT_POLL_SECONDS  = 2.0
DB_FILENAME           = "icon.db"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_path(config_folder: str) -> str:
    return os.path.join(config_folder, DB_FILENAME)


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
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id     INTEGER NOT NULL REFERENCES traceroute_runs(id),
            hop_number INTEGER NOT NULL,
            hop_host   TEXT,
            avg_ms     REAL,           -- NULL means unreachable / timed-out
            min_ms     REAL,
            max_ms     REAL
        )
    """)
    # Migrate existing databases that predate the min_ms/max_ms columns
    existing = {row[1] for row in conn.execute("PRAGMA table_info(hop_results)")}
    for col in ("min_ms", "max_ms"):
        if col not in existing:
            conn.execute(f"ALTER TABLE hop_results ADD COLUMN {col} REAL")
    conn.commit()


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

    A hop with avg_ms=NULL in *this* run is saved only if it has replied in at
    least one previous run — that way intermittent loss is captured, but hops
    that permanently refuse to respond (e.g. routers that silently drop probe
    packets) don't accumulate NULL rows indefinitely.

    hops is a list of dicts with keys:
        hop_number  int
        hop_host    str | None
        avg_ms      float | None
        min_ms      float | None
        max_ms      float | None

    Returns (run_id, saved_hop_count).
    """
    # Of the silent hops in this run, keep only those that have replied before
    silent_hop_nums = [h["hop_number"] for h in hops if h.get("avg_ms") is None]
    previously_seen = _hops_ever_replied(conn, host, silent_hop_nums)

    hops_to_save = [
        h for h in hops
        if h.get("avg_ms") is not None          # replied this run — always save
        or h["hop_number"] in previously_seen   # was seen before — capture the loss
    ]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO traceroute_runs (timestamp, host) VALUES (?, ?)",
        (ts, host),
    )
    run_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO hop_results (run_id, hop_number, hop_host, avg_ms, min_ms, max_ms) VALUES (?,?,?,?,?,?)",
        [(run_id, h["hop_number"], h.get("hop_host"),
          h.get("avg_ms"), h.get("min_ms"), h.get("max_ms"))
         for h in hops_to_save],
    )
    conn.commit()
    return run_id, len(hops_to_save)


# ---------------------------------------------------------------------------
# Traceroute runner & parser
# ---------------------------------------------------------------------------

def run_traceroute(host: str, uio: UIO) -> list[dict]:
    """Run the system traceroute command and return a list of hop dicts."""
    try:
        result = subprocess.run(
            ["traceroute", "-n", "-w", "2", "-q", "3", host],
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


# Matches lines like:
#  1  192.168.1.1  1.234 ms  1.345 ms  1.456 ms
#  2  * * *
#  3  10.0.0.1  5.6 ms  * 6.7 ms
_HOP_RE = re.compile(
    r"^\s*(?P<hop>\d+)\s+(?P<rest>.+)$"
)
_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ms")


def _parse_traceroute(output: str, uio: UIO) -> list[dict]:
    hops = []
    for line in output.splitlines():
        m = _HOP_RE.match(line)
        if not m:
            continue
        hop_number = int(m.group("hop"))
        rest = m.group("rest").strip()

        # Extract numeric RTT values
        times = [float(t) for t in _TIME_RE.findall(rest)]

        # Try to grab an IP/hostname from the first non-* token
        hop_host = None
        for token in rest.split():
            if token != "*" and not _TIME_RE.match(token) and token != "ms":
                hop_host = token
                break

        avg_ms = (sum(times) / len(times)) if times else None
        min_ms = min(times) if times else None
        max_ms = max(times) if times else None

        hops.append({
            "hop_number": hop_number,
            "hop_host":   hop_host,
            "avg_ms":     avg_ms,
            "min_ms":     min_ms,
            "max_ms":     max_ms,
        })

        uio.debug(
            f"  hop {hop_number:>2}  host={hop_host or '?':>15}  "
            f"min={min_ms:.2f} avg={avg_ms:.2f} max={max_ms:.2f} ms" if avg_ms is not None
            else f"  hop {hop_number:>2}  host={hop_host or '?':>15}  avg=* (no reply)"
        )

    return hops


# ---------------------------------------------------------------------------
# Main application class
# ---------------------------------------------------------------------------

class IConDB(object):
    PROGRAM_NAME = "icon"

    def __init__(self, uio: UIO, options):
        """@brief Constructor
           @param uio A UIO instance handling user input and output (E.G stdin/stdout or a GUI)
           @param options An instance of the OptionParser command line options."""
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

        db_path = get_db_path(self._config_folder)
        self._uio.info(f"Database: {db_path}")
        self._uio.info(f"Target host: {host}  |  Poll interval: {poll_seconds}s")

        conn = open_db(db_path)

        try:
            while True:
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
                    # Log the final hop (destination) status
                    last = hops[-1]
                    if last["hop_host"] and last["avg_ms"] is not None:
                        self._uio.info(
                            f"  Destination {last['hop_host']} reachable, "
                            f"avg RTT = {last['avg_ms']:.2f} ms"
                        )
                    else:
                        self._uio.warn(f"  Destination {host} NOT reachable in this run.")
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
            description="A tool that repeatedly performs checks on internet connectivity "
                        "and stores the data to a local sqllite database.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("-d", "--debug",        action="store_true", help="Enable debugging.")
        parser.add_argument("-t", "--host",          help=f"The host address that traceroute will use to check internet connectivity (default: {DEFAULT_HOST}).",
                            default=DEFAULT_HOST, required=False)
        parser.add_argument("-p", "--poll_seconds",  type=float, default=DEFAULT_POLL_SECONDS,
                            help=f"Periodicity of the traceroute command execution in seconds (default: {DEFAULT_POLL_SECONDS}, minimum: 1).")
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
