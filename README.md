# ICon — Internet Connectivity Recorder

ICon is a two-part tool for monitoring and visualising internet connectivity
from a Linux server. It uses the system `traceroute` command to periodically
probe a target host, records every hop's round-trip time in a local SQLite
database, and presents the results as an interactive time-series chart in a
web browser.

---

## How it works

```
┌─────────────────────────────────────────────────────────┐
│  icon_db  (background process / systemd service)        │
│                                                         │
│  Every N seconds:                                       │
│    traceroute -n -w 2 -q 3 <host>                       │
│        │                                                │
│        └─► parse hops ──► SQLite DB (~/.config/icon/)   │
└─────────────────────────────────────────────────────────┘
                          │
                          │  reads
                          ▼
┌─────────────────────────────────────────────────────────┐
│  icon_gui  (NiceGUI web app)                            │
│                                                         │
│  Browser → http://localhost:8100                        │
│    Interactive Plotly chart — one trace per hop         │
│    Hover: timestamp, avg / min / max RTT                │
│    Unreachable hops shown as red ✕ markers              │
└─────────────────────────────────────────────────────────┘
```

---

## Requirements

- Linux (uses the system `traceroute` binary)
- Python ≥ 3.10.12
- [Poetry](https://python-poetry.org/) ≥ 2.0

Install `traceroute` if it is not already present:

```bash
sudo apt install traceroute
```

---

## Installation


```bash
./install.py linux/linux/icon-<version>-py3-none-any.whl
```

---

## Project layout

```
icon/
├── pyproject.toml
├── README.md
├── src/
│   └── icon/
│       ├── __init__.py
│       ├── icon_db.py      # data collection daemon
│       └── icon_gui.py     # web GUI
└── tests/
    ├── __init__.py
    ├── test_icon_db.py
    └── test_icon_gui.py
```

---

## Usage

### icon_db — data collector

Runs continuously, executing `traceroute` at the configured interval and
storing the results in the database.

```bash
icon_db [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `-t`, `--host HOST` | `8.8.8.8` | Target host for traceroute |
| `-p`, `--poll_seconds N` | `2.0` | Probe interval in seconds (minimum: 1) |
| `-d`, `--debug` | off | Enable debug logging |
| `--enable_auto_start` | — | Register as a systemd service that starts on boot |
| `--disable_auto_start` | — | Remove the systemd service |
| `--restart_service` | — | Restart the running service |
| `--check_auto_start` | — | Show the current service status |

**Examples**

```bash
# Use defaults (poll 8.8.8.8 every 2 seconds)
icon_db

# Poll a custom host every 10 seconds
icon_db --host 1.1.1.1 --poll_seconds 10

# Run as a systemd service that survives reboots (must e installed as root user)
sudo icon_db --enable_auto_start
```

---

### icon_gui — web interface

Opens a browser to an interactive chart showing RTT over time for every
reachable hop in the path to the target host.

```bash
icon_gui [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `-t`, `--host HOST` | `8.8.8.8` | Host whose data is displayed |
| `--hours N` | `24` | Look-back window in hours |
| `--port PORT` | `8100` | TCP port the web server listens on |
| `--no_browser` | off | Start the server without opening a browser tab |
| `-d`, `--debug` | off | Enable debug logging |
| `--enable_auto_start` | — | Register as a systemd service |
| `--disable_auto_start` | — | Remove the systemd service |
| `--restart_service` | — | Restart the running service |
| `--check_auto_start` | — | Show the current service status |

**Examples**

```bash
# Open browser automatically (default)
icon_gui

# Headless server — connect from another machine
icon_gui --no_browser --port 8100

# Show the last 7 days of data
icon_gui --hours 168
```

Once running, open your browser at:

```
http://localhost:8100
```

#### GUI controls

| Control | Action |
|---|---|
| **Show last** dropdown | Switch the look-back window (1 h → 7 days) |
| **Refresh now** | Force an immediate re-read of the database |
| **Delete all data** | Permanently wipe all records (confirmation required) |

Hover over any point on the chart to see the timestamp and the
**min / avg / max** RTT for that probe.

---

## Running both tools together

`icon_db` and `icon_gui` are independent processes that communicate only
through the shared SQLite database.  The simplest way to run them
side-by-side is in two terminal windows:

```bash
# Terminal 1
icon_db

# Terminal 2
icon_gui
```

For a permanent deployment, register both as systemd services:

```bash
icon_db  --enable_auto_start
icon_gui --no_browser --enable_auto_start
```

---

## Database

The database is stored at:

```
~/.config/icon/icon.db
```

It contains two tables:

**`traceroute_runs`** — one row per probe execution.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Primary key |
| `timestamp` | TEXT | UTC time of the run (`YYYY-MM-DD HH:MM:SS`) |
| `host` | TEXT | Target host |

**`hop_results`** — one row per hop per run.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Primary key |
| `run_id` | INTEGER | Foreign key → `traceroute_runs.id` |
| `hop_number` | INTEGER | Position in the path (1 = first router) |
| `hop_host` | TEXT | IP address of the hop (NULL if unknown) |
| `avg_ms` | REAL | Average RTT across probes (NULL = no reply) |
| `min_ms` | REAL | Minimum RTT across probes |
| `max_ms` | REAL | Maximum RTT across probes |

### Storage efficiency

Hops that **never** respond (e.g. routers that silently drop UDP probes) are
not stored, avoiding unbounded NULL accumulation. A hop that has responded at
least once but goes silent in a later run **is** stored with `avg_ms = NULL`,
so intermittent outages are captured accurately.

Older databases that predate the `min_ms` / `max_ms` columns are migrated
automatically on first run.

---

## Running the tests

```bash
./run_tests.sh
```

The test suite uses in-memory SQLite databases and mocks NiceGUI and
`subprocess` so no network access or browser is required.

---

## Dependencies

| Package | Purpose |
|---|---|
| `nicegui` | Web UI framework |
| `plotly` | Interactive charting |
| `p3lib` | CLI helpers, boot manager, launcher |
| `rich` | Terminal output formatting |
| `psutil` | Process/system utilities |
| `pillow` | Image handling (desktop launcher icon) |

---

## License

MIT

## Author
Paul Austen — [pjaos@gmail.com](mailto:pjaos@gmail.com)

## Acknowledgements
Development of this project was assisted by [Claude](https://claude.ai) (Anthropic's AI assistant),
which contributed to code review, bug identification, test generation, and this documentation.
