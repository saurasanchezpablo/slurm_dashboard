#!/usr/bin/env python3
"""
slurm_dashboard.py — Real-time TUI Dashboard for SLURM
Requirements: pip install textual rich
"""

from __future__ import annotations
import subprocess
import shlex
import os
import sys
import re
import json
import shutil
import configparser
from typing import Optional
from datetime import datetime
from collections import defaultdict
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import (
    Header, Footer, DataTable, Static, Label,
    RichLog, Tabs, Tab, Button, TextArea, Input
)
from textual.containers import Vertical, Horizontal, Container, VerticalScroll
from textual.screen import ModalScreen
from textual import work
from textual.timer import Timer
from rich.text import Text
from textual.theme import Theme

# ──────────────────────────────────────────────
#  DESIGN TOKENS
# ──────────────────────────────────────────────
#  One table drives everything: the Textual theme used by the stylesheets
#  ($sq-* variables) and the constants used by Rich when painting table
#  cells and log lines.  Nothing in this file should name a colour that is
#  not in here — tests/test_theme.py enforces that.
#
#  The ramp is a cool graphite with a single azure accent; hue is reserved
#  for meaning (job state, thresholds), never for decoration.
PALETTE = {
    # ── surfaces, darkest to lightest ──
    "bg":          "#0f1319",   # app canvas
    "surface":     "#161b23",   # tables, log bodies, inputs
    "surface_alt": "#1a2029",   # alternating table rows
    "panel":       "#1c222c",   # toolbars, headers, footers, dialogs
    "elevated":    "#242b36",   # buttons, hover states
    "line":        "#29313c",   # hairlines and default borders
    "line_strong": "#3a4553",   # dividers that must read as structure

    # ── type ──
    "fg":        "#dbe2ea",     # primary text
    "fg_muted":  "#93a0b0",     # secondary text, labels
    "fg_faint":  "#6a7583",     # captions, timestamps, rules
    "fg_dim":    "#4a5462",     # disabled

    # ── accent: focus, selection, primary action ──
    "primary":       "#4d8dfb",
    "primary_hover": "#6fa3ff",
    "primary_soft":  "#8fb8ff",

    # ── semantic: state and thresholds only ──
    "ok":          "#46b96a",
    "ok_hover":    "#57cc7c",
    "info":        "#3fb6c9",
    "warn":        "#d8a13c",
    "warn_hover":  "#e8b153",
    "err":         "#e2574f",
    "err_hover":   "#f06a62",
    "violet":      "#9b78e8",
    "violet_hover": "#ae8ff2",
    "amber":       "#dd8a4c",
}


class C:
    """Palette as attributes, for Rich style strings."""
    BG          = PALETTE["bg"]
    SURFACE     = PALETTE["surface"]
    SURFACE_ALT = PALETTE["surface_alt"]
    PANEL       = PALETTE["panel"]
    ELEVATED    = PALETTE["elevated"]
    LINE        = PALETTE["line"]
    LINE_STRONG = PALETTE["line_strong"]
    FG          = PALETTE["fg"]
    FG_MUTED    = PALETTE["fg_muted"]
    FG_FAINT    = PALETTE["fg_faint"]
    FG_DIM      = PALETTE["fg_dim"]
    PRIMARY     = PALETTE["primary"]
    PRIMARY_SOFT = PALETTE["primary_soft"]
    OK          = PALETTE["ok"]
    INFO        = PALETTE["info"]
    WARN        = PALETTE["warn"]
    ERR         = PALETTE["err"]
    VIOLET      = PALETTE["violet"]
    AMBER       = PALETTE["amber"]


def build_theme() -> Theme:
    """Expose the palette to the stylesheets as $sq-* variables.

    The sq- prefix keeps these clear of Textual's own variables, so a future
    Textual release cannot silently redefine one of ours.
    """
    return Theme(
        name="sqdash",
        dark=True,
        primary=PALETTE["primary"],
        secondary=PALETTE["violet"],
        accent=PALETTE["primary"],
        success=PALETTE["ok"],
        warning=PALETTE["warn"],
        error=PALETTE["err"],
        background=PALETTE["bg"],
        surface=PALETTE["surface"],
        panel=PALETTE["panel"],
        foreground=PALETTE["fg"],
        variables={
            "sq-bg":           PALETTE["bg"],
            "sq-surface":      PALETTE["surface"],
            "sq-surface-alt":  PALETTE["surface_alt"],
            "sq-panel":        PALETTE["panel"],
            "sq-elevated":     PALETTE["elevated"],
            "sq-line":         PALETTE["line"],
            "sq-line-strong":  PALETTE["line_strong"],
            "sq-fg":           PALETTE["fg"],
            "sq-fg-muted":     PALETTE["fg_muted"],
            "sq-fg-faint":     PALETTE["fg_faint"],
            "sq-fg-dim":       PALETTE["fg_dim"],
            "sq-primary":      PALETTE["primary"],
            "sq-primary-hover": PALETTE["primary_hover"],
            "sq-ok":           PALETTE["ok"],
            "sq-ok-hover":     PALETTE["ok_hover"],
            "sq-info":         PALETTE["info"],
            "sq-warn":         PALETTE["warn"],
            "sq-warn-hover":   PALETTE["warn_hover"],
            "sq-err":          PALETTE["err"],
            "sq-err-hover":    PALETTE["err_hover"],
            "sq-violet":       PALETTE["violet"],
            "sq-violet-hover": PALETTE["violet_hover"],
            "sq-amber":        PALETTE["amber"],
        },
    )


# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────
MY_USER           = os.environ.get("USER", "")
HISTORY_FILE      = Path.home() / ".slurm_dashboard_history.json"
EVENT_LOG_FILE    = Path.home() / ".slurm_dashboard_events.log"
MAX_EVENT_LOG     = 5000   # max lines kept in the event log file
DEFAULT_PARTITIONS: list[str] = []

CONFIG_DIR  = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "slurm_dashboard"
CONFIG_FILE = CONFIG_DIR / "config.ini"
TEMPLATE_FILE = CONFIG_DIR / "templates.json"
WATCHLIST_FILE = CONFIG_DIR / "watchlist.json"

# Defaults, and the schema used to coerce values read from the INI file.
# INI is used rather than TOML because tomllib only exists on Python 3.11+
# and this tool targets 3.9+ on login nodes we do not control.
CONFIG_DEFAULTS: dict[str, dict] = {
    "general": {
        "refresh_interval":  3,      # seconds between squeue polls
        "log_tail_lines":    200,    # lines shown in the log viewer
        "max_history":       500,    # entries kept in the history file
        "history_only_mine": True,   # only persist jobs owned by $USER
    },
    "monitor": {
        "use_ssh":          True,    # allow SSH to compute nodes for live metrics
        "ssh_timeout":      8,
        "refresh_interval": 8,
        "max_nodes":        8,       # nodes rendered per monitor refresh
    },
    "logs": {
        "live_refresh": 5,           # seconds between auto-tail reads
    },
    "notifications": {
        "bell_on_finish":   True,    # terminal bell when a tracked job ends
        "notify_on_finish": True,    # toast when a tracked job ends
        "hook":             "",      # executable run as: hook <jobid> <state> <name>
    },
}


_TRUE_WORDS  = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


def _coerce(value: str, default):
    """Coerce an INI string to the type of its default.

    An unrecognised value keeps the default rather than silently becoming
    False/0 — a typo in the config should not quietly turn a feature off.
    """
    # Belt and braces: the parser is configured to strip inline comments,
    # but a value may still arrive with one attached from an older file.
    text = str(value).split("#")[0].split(";")[0].strip()
    if isinstance(default, bool):
        low = text.lower()
        if low in _TRUE_WORDS:
            return True
        if low in _FALSE_WORDS:
            return False
        return default
    if isinstance(default, int):
        try:
            return int(text)
        except ValueError:
            return default
    return text


def load_config(path: Path | None = None) -> dict:
    """Read the INI config, falling back to CONFIG_DEFAULTS for anything
    missing or malformed.  A broken config must never stop the dashboard."""
    cfg = {sec: dict(vals) for sec, vals in CONFIG_DEFAULTS.items()}
    path = path or CONFIG_FILE
    try:
        if not path.exists():
            return cfg
        # inline_comment_prefixes is None by default, which would make
        # "use_ssh = true  # explanation" parse as the whole trailing string.
        parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        parser.read(path, encoding="utf-8")
        for section in parser.sections():
            if section not in cfg:
                continue
            for key, raw in parser.items(section):
                if key in cfg[section]:
                    cfg[section][key] = _coerce(raw, CONFIG_DEFAULTS[section][key])
    except Exception:
        return {sec: dict(vals) for sec, vals in CONFIG_DEFAULTS.items()}
    # Guard against values that would make the UI unusable.
    cfg["general"]["refresh_interval"] = max(1, cfg["general"]["refresh_interval"])
    cfg["general"]["log_tail_lines"]   = max(10, cfg["general"]["log_tail_lines"])
    cfg["general"]["max_history"]      = max(10, cfg["general"]["max_history"])
    cfg["monitor"]["refresh_interval"] = max(2, cfg["monitor"]["refresh_interval"])
    cfg["monitor"]["ssh_timeout"]      = max(1, cfg["monitor"]["ssh_timeout"])
    cfg["monitor"]["max_nodes"]        = max(1, cfg["monitor"]["max_nodes"])
    cfg["logs"]["live_refresh"]        = max(1, cfg["logs"]["live_refresh"])
    return cfg


CONFIG_TEMPLATE = """\
# Slurm Dashboard configuration.
# Delete any setting to fall back to its default.
# Comments must start at the beginning of a line or follow a value.

[general]
# seconds between squeue polls
refresh_interval = 3
# lines shown in the log viewer
log_tail_lines = 200
# entries kept in ~/.slurm_dashboard_history.json
max_history = 500
# false also records other users' jobs; they will evict your own
# once max_history is reached
history_only_mine = true

[monitor]
# false = read node usage from `scontrol show node` only, never SSH.
# Set this if your site does not allow logging in to compute nodes.
use_ssh = true
ssh_timeout = 8
refresh_interval = 8
# nodes rendered per monitor refresh
max_nodes = 8

[logs]
# seconds between auto-tail reads in the log viewer
live_refresh = 5

[notifications]
# terminal bell when one of your jobs reaches a final state
bell_on_finish = true
notify_on_finish = true
# optional executable, called as: hook <jobid> <state> <name>
hook =
"""


def write_default_config(path: Path | None = None) -> Path:
    """Create the config file with documented defaults if it is missing."""
    path = path or CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path


CONFIG = load_config()

REFRESH_INTERVAL  = CONFIG["general"]["refresh_interval"]
LOG_TAIL_LINES    = CONFIG["general"]["log_tail_lines"]
MAX_HISTORY       = CONFIG["general"]["max_history"]
HISTORY_ONLY_MINE = CONFIG["general"]["history_only_mine"]

# Job ids and node names are interpolated into the argv of external commands
# (scancel, scontrol, ssh...).  They come from parsed command output, so they
# are validated before use: a value starting with "-" would otherwise be
# swallowed as an option by the target program (argument injection).
_JOBID_RE    = re.compile(r"^\d+(?:\+\d+)?(?:_(?:\d+|\[[0-9,\-:%]+\]))?(?:\.[A-Za-z0-9_]+)?$")
_NODENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_valid_jobid(jobid: str) -> bool:
    """True for '123', '123_4', '123_[0-9]', '123.batch', '123+0'."""
    return bool(jobid) and bool(_JOBID_RE.match(str(jobid).strip()))


def is_valid_nodename(node: str) -> bool:
    return bool(node) and len(node) <= 255 and bool(_NODENAME_RE.match(str(node).strip()))

# ──────────────────────────────────────────────
#  JOB HISTORY
# ──────────────────────────────────────────────
def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    # The file may have been hand-edited or written by an older version:
    # drop anything that is not a usable record instead of crashing later.
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("jobid")]

def save_history(history: list[dict]) -> None:
    """Atomic write: write to a temp file then rename, so the history JSON
    is never left in a truncated/corrupt state if the process is killed.
    Created with mode 0600 — it records job names, working directories and
    log paths, which should not be world-readable on a shared cluster."""
    tmp = HISTORY_FILE.with_suffix(".tmp")
    try:
        payload = json.dumps(history[-MAX_HISTORY:], indent=2)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        tmp.replace(HISTORY_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def append_event_log(ts: str, msg: str) -> None:
    """Append a single event line to the persistent log file."""
    try:
        fd = os.open(EVENT_LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
        # Trim to MAX_EVENT_LOG lines when file exceeds 1 MB
        try:
            if EVENT_LOG_FILE.stat().st_size > 1_000_000:
                lines = EVENT_LOG_FILE.read_text(encoding="utf-8").splitlines()
                EVENT_LOG_FILE.write_text(
                    "\n".join(lines[-MAX_EVENT_LOG:]) + "\n", encoding="utf-8"
                )
        except Exception:
            pass
    except Exception:
        pass


def load_event_log(n: int = 500) -> list[str]:
    """Return the last n lines from the persistent event log file."""
    if not EVENT_LOG_FILE.exists():
        return []
    try:
        lines = EVENT_LOG_FILE.read_text(encoding="utf-8").splitlines()
        return lines[-n:]
    except Exception:
        return []

def upsert_history(history: list[dict], job: dict, paths: dict | None = None) -> list[dict]:
    """
    Insert or update a job record in history.
    Keyed by jobid. Updates state, end time, and log paths if provided.
    """
    entry = next((e for e in history if e.get("jobid") == job["jobid"]), None)
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if entry is None:
        entry = {
            "jobid":     job["jobid"],
            "name":      job.get("name", ""),
            "user":      job.get("user", ""),
            "partition": job.get("partition", ""),
            "cpus":      job.get("cpus", ""),
            "mem":       job.get("mem", ""),
            "gpus":      job.get("gpus", ""),
            "state":     job.get("state", ""),
            "first_seen": now,
            "last_seen":  now,
            "stdout":    "",
            "stderr":    "",
        }
        history.append(entry)
        # Keep the in-memory list bounded too: save_history() only truncates
        # what it writes, so without this the list grew for the whole session.
        if len(history) > MAX_HISTORY:
            del history[:-MAX_HISTORY]
    else:
        entry["state"]     = job.get("state", entry.get("state", ""))
        entry["last_seen"] = now
    if paths:
        entry["stdout"] = paths.get("stdout", entry.get("stdout", ""))
        entry["stderr"] = paths.get("stderr", entry.get("stderr", ""))
    return history

# ──────────────────────────────────────────────
#  SLURM HELPERS
# ──────────────────────────────────────────────
def run(cmd: list[str], timeout: int = 10) -> tuple[str, str]:
    """
    Run a command safely inside a Textual TUI.
    Redirects stdin to /dev/null to avoid BlockingIOError caused by
    Textual setting O_NONBLOCK on stdin, and closes all inherited fds.
    """
    try:
        with open(os.devnull, "r") as devnull:
            r = subprocess.run(
                cmd,
                stdin=devnull,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                close_fds=True,
            )
        return r.stdout, r.stderr
    except Exception as e:
        return "", str(e)

def run_out(cmd: list[str]) -> str:
    return run(cmd)[0]

def state_style(state: str) -> str:
    """Rich style for a job state. Hue means state and nothing else."""
    return {
        "R":   f"bold {C.OK}",     "RUNNING":    f"bold {C.OK}",
        "PD":  f"bold {C.INFO}",   "PENDING":    f"bold {C.INFO}",
        "CG":  C.OK,               "COMPLETING": C.OK,
        "CD":  C.FG_MUTED,         "COMPLETED":  C.FG_MUTED,
        "F":   f"bold {C.ERR}",    "FAILED":     f"bold {C.ERR}",
        "CA":  C.WARN,             "CANCELLED":  C.WARN,
        "TO":  f"bold {C.WARN}",   "TIMEOUT":    f"bold {C.WARN}",
        "NF":  f"bold {C.ERR}",    "NODE_FAIL":  f"bold {C.ERR}",
        "PR":  C.VIOLET,           "PREEMPTED":  C.VIOLET,
        "S":   C.FG_FAINT,         "SUSPENDED":  C.FG_FAINT,
        "OOM": f"bold {C.ERR}",    "OUT_OF_MEMORY": f"bold {C.ERR}",
    }.get(state.upper().strip(), C.FG)

def sacct_final_state(jobids: list[str]) -> dict[str, str]:
    """
    Query sacct for the best final state of each jobid.
    Handles suffixes like 123.batch, 123.extern, array jobs 123_4.
    Prioritises terminal states over live states when multiple rows exist.
    """
    jobids = [j for j in jobids if is_valid_jobid(j)]
    if not jobids:
        return {}
    # sacct is called with every id that vanished from squeue at once; on a
    # busy cluster that list can blow past ARG_MAX, so query it in batches.
    out_chunks = []
    for i in range(0, len(jobids), 50):
        ids_arg = ",".join(jobids[i:i + 50])
        try:
            out_chunks.append(run_out([
                "sacct", "-j", ids_arg, "-n", "-P",
                "--format=JobID,State"
            ]))
        except Exception:
            continue
    out = "\n".join(out_chunks)

    _priority = {
        "OUT_OF_MEMORY": 100, "NODE_FAIL": 95, "FAILED": 90,
        "TIMEOUT": 85, "CANCELLED": 80, "PREEMPTED": 75,
        "COMPLETED": 70, "COMPLETING": 40, "RUNNING": 30,
        "PENDING": 20, "CONFIGURING": 15, "SUSPENDED": 10, "UNKNOWN": 0,
    }
    _aliases = {
        "CANCELED": "CANCELLED", "CANCELLED": "CANCELLED",
        "FAILED": "FAILED", "TIMEOUT": "TIMEOUT",
        "OUT_OF_MEMORY": "OUT_OF_MEMORY", "NODE_FAIL": "NODE_FAIL",
        "COMPLETED": "COMPLETED", "PREEMPTED": "PREEMPTED",
        "COMPLETING": "COMPLETING", "RUNNING": "RUNNING",
        "PENDING": "PENDING", "CONFIGURING": "CONFIGURING",
        "SUSPENDED": "SUSPENDED",
    }

    best: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2:
            continue
        raw_jid   = parts[0].strip()
        raw_state = parts[1].strip().split()[0].upper()
        if not raw_jid or not raw_state:
            continue
        # Normalise "123.batch" → "123", "123_4" → "123"
        base = raw_jid.split(".")[0]
        if "_" in base:
            base = base.split("_")[0]
        state = _aliases.get(raw_state, raw_state)
        cur = best.get(base)
        if cur is None or _priority.get(state, 0) > _priority.get(cur, 0):
            best[base] = state

    return {jid: best[jid] for jid in jobids if jid in best}


def parse_squeue() -> list[dict]:
    # %j (job name) is deliberately LAST: Slurm allows "|" inside a job name,
    # and an unbounded split would then shift every following field (the user
    # column in particular, which the cancel/hold guards rely on).
    # split("|", 11) keeps the remainder — the full name — in the last element.
    fmt = "%i|%P|%u|%T|%M|%L|%C|%m|%b|%R|%N|%j"
    out = run_out(["squeue", f"--format={fmt}", "--noheader"])
    jobs = []
    for line in out.strip().splitlines():
        parts = line.split("|", 11)
        if len(parts) < 12:
            continue
        raw_gpu = parts[8]
        gpu_val = ""
        m = re.search(r"\d+", raw_gpu)
        if m and "gpu" in raw_gpu.lower():
            gpu_val = m.group()
        jobs.append({
            "jobid":     parts[0],
            "partition": parts[1],
            "user":      parts[2],
            "state":     parts[3],
            "time":      parts[4],
            "time_left": parts[5],
            "cpus":      parts[6],
            "mem":       parts[7],
            "gpus":      gpu_val,
            # %R carries the pending reason for queued jobs and the node
            # list for running ones — showing the latter just repeats the
            # NODES column, so it is dropped.
            "reason":    "" if parts[9] in ("None", parts[10]) else parts[9],
            "nodes":     parts[10],
            "name":      parts[11][:20],
            "est_start": "",          # filled in by refresh_data()
        })
    return jobs


def get_start_estimates() -> dict[str, str]:
    """
    Query `squeue --start` for estimated start times of PENDING jobs.
    Returns a dict  {jobid: human_readable_start_string}.

    squeue --start uses the format:
      JOBID PARTITION NAME USER STATE START_TIME ...
    We request only JOBID and START_TIME via --format.
    """
    try:
        out = run_out([
            "squeue", "--start", "--noheader",
            "--format=%i|%S",          # jobid | expected_start
        ])
    except Exception:
        return {}

    estimates: dict[str, str] = {}
    now = datetime.now()
    today = now.date()

    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        jobid = parts[0].strip()
        raw   = parts[1].strip()
        if not jobid or raw in ("", "N/A", "Unknown", "None"):
            continue

        # Slurm emits ISO-8601: "2026-06-22T14:30:00" or "N/A"
        label = _format_start_time(raw, now, today)
        if label:
            estimates[jobid] = label

    return estimates


def _format_start_time(raw: str, now: datetime, today) -> str:
    """Convert a Slurm ISO-8601 start-time string to a compact human label."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    else:
        return ""   # unrecognised format

    delta_mins = int((dt - now).total_seconds() / 60)

    if delta_mins < 0:
        return "now"                          # already overdue / imminent
    if delta_mins < 60:
        return f"~{delta_mins}m"              # "~34m"
    if dt.date() == today:
        return dt.strftime("today %H:%M")     # "today 18:45"
    if (dt.date() - today).days < 7:
        return dt.strftime("%a %H:%M")        # "Wed 14:30"
    return dt.strftime("%b %-d")              # "Jul  3"

def parse_sinfo() -> list[dict]:
    fmt = "%N|%P|%t|%C|%m|%G|%f"
    out = run_out(["sinfo", f"--format={fmt}", "--noheader"])
    nodes = []
    for line in out.strip().splitlines():
        parts = line.split("|", 6)   # %f (features) is last — keep it whole
        if len(parts) < 7:
            continue
        nodes.append({
            "node": parts[0], "partition": parts[1], "state": parts[2],
            "cpu_aiotd": parts[3], "mem": parts[4],
            "gres": parts[5], "features": parts[6][:30],
        })
    return nodes

def compute_stats(jobs: list[dict]) -> dict:
    total = len(jobs)
    mine  = [j for j in jobs if j["user"] == MY_USER]
    by_state: dict[str, int] = defaultdict(int)
    for j in jobs:
        by_state[j["state"]] += 1
    running_gpus = 0
    for j in jobs:
        if j["state"] in ("R", "RUNNING") and j["gpus"]:
            try:
                running_gpus += int(j["gpus"])
            except ValueError:
                pass
    return {"total": total, "mine": len(mine),
            "by_state": dict(by_state), "running_gpus": running_gpus}

def get_job_log_paths(jobid: str) -> dict[str, str]:
    out = run_out(["scontrol", "show", "job", jobid]) if is_valid_jobid(jobid) else ""
    def extract(key: str) -> str:
        m = re.search(rf"{key}=(\S+)", out)
        return m.group(1) if m else ""

    raw_stdout  = extract("StdOut")
    raw_stderr  = extract("StdErr")
    workdir     = extract("WorkDir")
    job_name    = extract("JobName")
    user_name   = extract("UserId").split("(")[0]

    def resolve(path: str) -> str:
        if not path:
            return path
        # Use full resolver so %J (jobid.stepid) is handled correctly
        if not path.startswith("/"):
            path = os.path.join(workdir, path)
        resolved = resolve_existing_slurm_log(path, jobid, job_name, user_name)
        return resolved

    return {
        "stdout":  resolve(raw_stdout),
        "stderr":  resolve(raw_stderr),
        "workdir": workdir,
        "name":    job_name,
        "state":   extract("JobState"),
        "raw":     out,
    }

def tail_file(path: str, n: int = LOG_TAIL_LINES) -> str:
    """
    Read the last n lines of a file using pure Python (no subprocess).
    Avoids BlockingIOError from inherited O_NONBLOCK stdin in Textual.
    """
    if not path:
        return "(No path available)"
    if not os.path.exists(path):
        return f"(File not found: {path})"
    try:
        # Efficient tail: read from end using binary seek
        with open(path, "rb") as f:
            # Get file size
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return "(File is empty)"
            # Read last chunk (max 512KB) to find last n lines
            chunk = min(size, 512 * 1024)
            f.seek(-chunk, 2)
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        # Seeking to a fixed offset usually lands mid-line; that first
        # fragment is not a real line, so drop it (unless we read the
        # whole file, in which case every line is complete).
        if chunk < size and lines:
            lines = lines[1:]
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"(Error reading file: {e})"


def follow_file(path: str, offset: int = 0,
                max_lines: int = LOG_TAIL_LINES) -> tuple[list[str], int, bool]:
    """Incremental tail: read only what was appended since `offset`.

    Returns (complete_new_lines, new_offset, reset).  `reset` means the caller
    should discard what it had — either this is the first read or the file was
    truncated/rotated underneath us.  A trailing partial line is deliberately
    left unconsumed so it is returned whole on the next call.
    """
    if not path or not os.path.exists(path):
        return ([], 0, False)
    try:
        size = os.path.getsize(path)
        if offset > size:          # truncated or rotated
            offset = 0
        if offset <= 0:
            text = tail_file(path, max_lines)
            if text.startswith("(") and not os.path.getsize(path):
                return ([], size, True)
            return (text.splitlines(), size, True)
        if size == offset:
            return ([], offset, False)
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(size - offset)
        cut = data.rfind(b"\n")
        if cut == -1:
            return ([], offset, False)      # no complete line yet
        chunk = data[: cut + 1]
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        return (lines, offset + len(chunk), False)
    except Exception:
        return ([], offset, False)


# ──────────────────────────────────────────────
#  RESOURCE MONITORING
# ──────────────────────────────────────────────
def get_job_nodes(jobid: str) -> list[str]:
    """Return list of nodes assigned to a job."""
    if not is_valid_jobid(jobid):
        return []
    out = run_out(["squeue", "-j", jobid, "-h", "--format=%N"])
    nodelist = out.strip()
    if not nodelist or nodelist in ("(None)", "N/A", ""):
        return []
    try:
        exp = run_out(["scontrol", "show", "hostnames", nodelist])
        names = [n.strip() for n in exp.strip().splitlines() if n.strip()]
    except Exception:
        names = [nodelist]
    # Only hand well-formed hostnames to ssh_cmd().
    return [n for n in names if is_valid_nodename(n)]

def ssh_cmd(node: str, cmd: str, timeout: int | None = None) -> str:
    """Execute a read-only command on a compute node via SSH (key auth only).

    Notes:
      * The node name is validated first — an unvalidated name beginning with
        "-" would be parsed by ssh as an option (e.g. -oProxyCommand=...).
      * stdin is redirected to /dev/null: ssh otherwise reads the terminal
        that Textual owns, stealing keystrokes and tripping BlockingIOError.
      * StrictHostKeyChecking=accept-new trusts a *new* host but still refuses
        a host whose key changed, which is what detects a MITM.  "no" accepted
        changed keys silently.
    """
    if not is_valid_nodename(node) or not CONFIG["monitor"]["use_ssh"]:
        return ""
    timeout = timeout or CONFIG["monitor"]["ssh_timeout"]
    try:
        with open(os.devnull, "r") as devnull:
            r = subprocess.run(
                ["ssh", "-n",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=5",
                 "-o", "BatchMode=yes",
                 "--", node, cmd],
                stdin=devnull,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=timeout, close_fds=True,
            )
        return r.stdout
    except Exception:
        return ""

def get_node_gpu_info(node: str) -> list[dict]:
    """
    Try to get GPU info via nvidia-smi.
    Returns empty list if no GPUs or no access.
    """
    cmd = (
        "nvidia-smi --query-gpu=index,name,utilization.gpu,"
        "memory.used,memory.total,temperature.gpu,power.draw "
        "--format=csv,noheader,nounits 2>/dev/null"
    )
    out = ssh_cmd(node, cmd)
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            gpus.append({
                "index":    parts[0],
                "name":     parts[1][:24],
                "util":     int(parts[2]) if parts[2].isdigit() else 0,
                "mem_used": int(parts[3]) if parts[3].isdigit() else 0,
                "mem_total":int(parts[4]) if parts[4].isdigit() else 0,
                "temp":     parts[5] if parts[5] != "[N/A]" else "N/A",
                "power":    parts[6] if parts[6] not in ("[N/A]", "N/A") else "N/A",
            })
        except (ValueError, IndexError):
            continue
    return gpus

def get_node_cpu_mem(node: str) -> dict:
    """
    Fetches CPU%, memory and load for a node via /proc.
    Works without nvidia-smi — only SSH access required.
    """
    cmd = (
        "awk '/^cpu / {u=$2+$4; t=$2+$3+$4+$5+$6+$7+$8; "
        "printf \"cpu_busy=%d cpu_total=%d\\n\", u, t}' /proc/stat; "
        "awk '/^MemTotal/ {t=$2} /^MemAvailable/ {a=$2} "
        "END {printf \"mem_total=%d mem_avail=%d\\n\", t, a}' /proc/meminfo; "
        "uptime | awk -F\"load average:\" '{print \"load=\"$2}'"
    )
    out = ssh_cmd(node, cmd)
    result = {"cpu_pct": 0, "mem_total_kb": 0, "mem_used_kb": 0,
              "mem_pct": 0, "load": "N/A"}
    for line in out.strip().splitlines():
        line = line.strip()
        if line.startswith("cpu_busy="):
            # Second read to compute delta (approx with a sleep)
            pass
        if line.startswith("mem_total="):
            # split("=", 1): a token like "a=b=c" would otherwise raise
            # ValueError here and kill the whole monitor worker.
            kv = dict(item.split("=", 1) for item in line.split() if "=" in item)
            try:
                mt = int(kv.get("mem_total", 0))
                ma = int(kv.get("mem_avail", 0))
            except ValueError:
                continue
            result["mem_total_kb"] = mt
            result["mem_used_kb"]  = mt - ma
            result["mem_pct"] = int((mt - ma) / mt * 100) if mt > 0 else 0
        if line.startswith("load="):
            result["load"] = line[5:].strip().split(",")[0].strip()
    # CPU% via top -bn1 (mpstat is not available everywhere)
    cpu_out = ssh_cmd(node,
        "top -bn2 -d0.2 | grep '^%Cpu' | tail -1 | "
        "awk '{print 100-$8}' 2>/dev/null || echo 0")
    try:
        result["cpu_pct"] = int(float(cpu_out.strip()))
    except (ValueError, TypeError):
        result["cpu_pct"] = 0
    return result

def get_job_sstat(jobid: str) -> dict:
    """
    Fetches job resource usage via sstat (running jobs only).
    """
    result = {"avg_cpu": "N/A", "max_rss": "N/A", "tasks": "N/A"}
    if not is_valid_jobid(jobid):
        return result
    out = run_out([
        "sstat", "-j", jobid, "--noheader",
        "--format=AveCPU,MaxRSS,MaxVMSize,NTasks"
    ])
    line = out.strip().splitlines()[0] if out.strip() else ""
    if line:
        parts = line.split()
        if len(parts) >= 3:
            result["avg_cpu"] = parts[0]
            result["max_rss"] = parts[1]
            result["tasks"]   = parts[3] if len(parts) > 3 else "N/A"
    return result

def parse_time_to_secs(t: str) -> int:
    """Convert a Slurm time string (DD-HH:MM:SS or HH:MM:SS or MM:SS) to seconds."""
    t = t.strip()
    if t in ("UNLIMITED", "N/A", ""):
        return 0
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        try: days = int(d)
        except ValueError: pass
    parts = t.split(":")
    try:
        if len(parts) == 3:
            return days*86400 + int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
        elif len(parts) == 2:
            return days*86400 + int(parts[0])*60 + int(parts[1])
    except ValueError:
        pass
    return 0

def make_bar(pct: int, width: int = 20, fill: str = "█", empty: str = "░") -> str:
    """Generate an ASCII progress bar."""
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return fill * filled + empty * (width - filled)

def bar_color(pct: int) -> str:
    """Threshold colour for a utilisation bar."""
    if pct >= 90: return f"bold {C.ERR}"
    if pct >= 70: return C.WARN
    return C.OK

# ──────────────────────────────────────────────
#  JOB RERUN / RESUBMIT
# ──────────────────────────────────────────────
def get_submit_line(jobid: str) -> str:
    """
    Tries to recover the original submit line from multiple sources:
    1. sacct --format=SubmitLine  (available on clusters with full accounting)
    2. scontrol show job Command= (available while the job exists in Slurm)
    Returns the .sh script path or the full sbatch line, or "" if not found.
    """
    if not is_valid_jobid(jobid):
        return ""
    # Source 1: sacct SubmitLine
    try:
        out = run_out(["sacct", "-j", jobid, "-X", "-n", "-P", "--format=SubmitLine"])
        for line in out.splitlines():
            line = line.strip()
            if line and line not in ("SubmitLine", "Unknown", "N/A", ""):
                return line
    except Exception:
        pass

    # Source 2: scontrol show job -> Command= field
    try:
        out = run_out(["scontrol", "show", "job", jobid])
        m = re.search(r"Command=(\S+)", out)
        if m:
            script = m.group(1)
            if script and script != "(null)" and os.path.isfile(script):
                return f"sbatch {shlex.quote(script)}"
    except Exception:
        pass

    return ""

def get_job_script_info(jobid: str) -> dict:
    """
    Extract Command and WorkDir from scontrol show job
    to allow building an sbatch command manually.
    """
    info = {"command": "", "workdir": "", "extra": ""}
    if not is_valid_jobid(jobid):
        return info
    try:
        out = run_out(["scontrol", "show", "job", jobid])
        m_cmd  = re.search(r"Command=(\S+)", out)
        m_work = re.search(r"WorkDir=(\S+)", out)
        if m_cmd:  info["command"] = m_cmd.group(1)
        if m_work: info["workdir"] = m_work.group(1)
    except Exception:
        pass
    return info

def build_sbatch_args(submit_line: str) -> list[str] | None:
    """Turn a recovered submit line into a safe argv for sbatch.

    The line comes from `sacct --format=SubmitLine`, from `scontrol` or from
    the on-disk history JSON, so it is not trusted to be an sbatch invocation.
    Only a line that *is* an sbatch call is accepted; anything else returns
    None instead of being "fixed up" by prepending sbatch, which silently
    turned a line such as `/bin/sh -c '...'` into submission arguments.
    """
    try:
        args = shlex.split(submit_line)
    except ValueError:      # unbalanced quotes
        return None
    if not args:
        return None
    if os.path.basename(args[0]) != "sbatch":
        return None
    return ["sbatch"] + args[1:]


def run_sbatch(args: list[str]) -> tuple[bool, str]:
    """Launch sbatch with the given args. Returns (ok, message)."""
    try:
        with open(os.devnull, "r") as devnull:
            proc = subprocess.run(
                args, stdin=devnull, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=30,
                close_fds=True,
            )
        if proc.returncode == 0:
            return True, proc.stdout.strip() or "Job resubmitted"
        return False, proc.stderr.strip() or "sbatch unknown error"
    except Exception as e:
        return False, str(e)

def rerun_history_job(jobid: str) -> tuple[bool, str]:
    """
    Multi-source strategy to resubmit a job:
      1. sacct SubmitLine -> sbatch <original line>
      2. scontrol Command= -> sbatch <script>
      3. scontrol requeue  -> back to PENDING (only jobs still in Slurm)
    """
    if not is_valid_jobid(jobid):
        return False, f"Invalid job id: {jobid!r}"

    # Strategy 1 + 2: get_submit_line already combines both sources
    submit_line = get_submit_line(jobid)
    if submit_line:
        args = build_sbatch_args(submit_line)
        if args:
            return run_sbatch(args)

    # Strategy 3: scontrol requeue
    # run() keeps stdin off the terminal and never raises on timeout /
    # missing binary, unlike the bare subprocess.run() this used to be.
    _, req_err = run(["scontrol", "requeue", jobid], timeout=20)
    if not req_err.strip():
        return True, f"Job {jobid} requeued → back to PENDING"

    return False, (
        "Could not resubmit the job. Possible causes:\n"
        "· sacct has no SubmitLine (cluster accounting config)\n"
        "· The job no longer exists in scontrol (too old)\n"
        "· You lack requeue permissions\n"
        "Fix: submit manually with sbatch <your_script.sh>"
    )

# ──────────────────────────────────────────────
#  HISTORY ANALYTICS
# ──────────────────────────────────────────────
def compute_history_stats(history: list[dict]) -> dict:
    """
    Computes aggregate metrics from the job history.
    """
    from collections import Counter

    if not history:
        return {}

    # ── counts by state ──
    states = Counter(e.get("state", "UNKNOWN") for e in history)
    success_states = {"COMPLETED", "CD"}
    failed_states  = {"FAILED", "F", "CANCELLED", "CA", "NODE_FAIL", "NF",
                      "TIMEOUT", "TO", "OUT_OF_MEMORY", "OOM"}

    total      = len(history)
    succeeded  = sum(v for k, v in states.items() if k in success_states)
    failed     = sum(v for k, v in states.items() if k in failed_states)
    other      = total - succeeded - failed
    success_rt = round(succeeded / total * 100, 1) if total > 0 else 0.0
    fail_rt    = round(failed    / total * 100, 1) if total > 0 else 0.0

    # ── by partition ──
    by_partition = Counter(e.get("partition", "unknown") for e in history)

    # ── approximate GPU-hours (COMPLETED jobs only) ──
    # Uses last_seen - first_seen as a proxy for actual execution time
    gpu_hours = 0.0
    cpu_hours = 0.0
    wall_times = []
    for e in history:
        if e.get("state") not in success_states:
            continue
        try:
            gpus = int(e.get("gpus", 0) or 0)
        except (ValueError, TypeError):
            gpus = 0
        try:
            cpus = int(e.get("cpus", 0) or 0)
        except (ValueError, TypeError):
            cpus = 0
        try:
            t0 = datetime.strptime(e["first_seen"], "%Y-%m-%d %H:%M:%S")
            t1 = datetime.strptime(e["last_seen"],  "%Y-%m-%d %H:%M:%S")
            hrs = (t1 - t0).total_seconds() / 3600.0
            if 0 < hrs < 240:   # discard outliers (> 10 days)
                wall_times.append(hrs)
                gpu_hours += gpus * hrs
                cpu_hours += cpus * hrs
        except Exception:
            continue

    avg_wall = round(sum(wall_times) / len(wall_times), 2) if wall_times else 0.0

    # ── jobs by name (top repeated) ──
    by_name = Counter(e.get("name", "?") for e in history)

    # ── timeline: jobs per day (last 30 days) ──
    from datetime import timedelta
    today = datetime.now().date()
    days_30 = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    jobs_by_day: dict[str, int] = {d: 0 for d in days_30}
    for e in history:
        try:
            day = e["first_seen"][:10]
            if day in jobs_by_day:
                jobs_by_day[day] += 1
        except Exception:
            continue

    return {
        "total":        total,
        "succeeded":    succeeded,
        "failed":       failed,
        "other":        other,
        "success_rt":   success_rt,
        "fail_rt":      fail_rt,
        "states":       dict(states),
        "by_partition": dict(by_partition),
        "by_name":      dict(by_name.most_common(10)),
        "gpu_hours":    round(gpu_hours, 1),
        "cpu_hours":    round(cpu_hours, 1),
        "avg_wall_hrs": avg_wall,
        "jobs_by_day":  jobs_by_day,
    }





def get_partitions() -> list[str]:
    try:
        out = run_out(["sinfo", "-h", "-o", "%P"])
        parts = [p.strip().rstrip("*") for p in out.splitlines() if p.strip()]
        return parts if parts else list(DEFAULT_PARTITIONS)
    except Exception:
        return list(DEFAULT_PARTITIONS)

# Tokens that Slurm expands in the log file name
_SLURM_TOKENS = re.compile(r'%[jJuUxXaAnNtT]')

# ── Slurm log-path resolver  (%J = jobid.stepid variant) ──────────────────

def _split_array_jobid(jobid: str) -> tuple:
    jid = str(jobid).strip()
    if "_" in jid:
        master, task = jid.split("_", 1)
        return master, task
    return jid, ""


def _expand_slurm_log_candidates(
    pattern: str,
    jobid: str,
    job_name: str = "",
    username: str = "",
) -> list:
    """
    Returns a list of concrete paths by expanding all Slurm filename tokens.
    For %J (jobid.stepid) multiple candidates are tried in order.
    """
    if not pattern:
        return []

    pattern = os.path.expanduser(pattern.strip())
    jid_full   = str(jobid).strip()
    jid_master, jid_task = _split_array_jobid(jid_full)

    base_subs = {
        "%%": "%",
        "%j": jid_full,
        "%A": jid_master,
        "%a": jid_task or "0",
        "%u": username or os.environ.get("USER", ""),
        "%x": job_name or "",
        "%N": "",  # first node – unknown here, skip
        "%n": "0",
        "%s": "batch",
        "%t": "0",
    }

    j_variants = [
        f"{jid_full}.0",
        f"{jid_full}.batch",
        jid_full,
    ] if "%J" in pattern else [""]

    seen: set = set()
    out: list = []
    for jv in j_variants:
        p = pattern
        for token, value in base_subs.items():
            p = p.replace(token, value)
        p = p.replace("%J", jv)
        p = os.path.normpath(p)
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    return out


def resolve_existing_slurm_log(
    pattern: str,
    jobid: str,
    job_name: str = "",
    username: str = "",
) -> str:
    """
    Resolves a Slurm log path pattern (may contain %j, %J, %u, etc.) to the
    first path that actually exists on disk.  Falls back to the first candidate
    (already expanded) when nothing exists yet, so the viewer can at least try.
    """
    if not pattern:
        return ""
    if not _SLURM_TOKENS.search(pattern):
        return pattern  # no tokens – return as-is

    candidates = _expand_slurm_log_candidates(pattern, jobid, job_name, username)
    for p in candidates:
        if os.path.exists(p):
            return p
    # Nothing found yet – return best guess (first candidate)
    return candidates[0] if candidates else pattern



def _safe_log_path(raw: str, script: str) -> tuple[str, str]:
    """
    Given the log path written by the user, returns:
      - log_path : safe path to pass to --output/--error
      - dir_to_create : base directory to create before sbatch

    Rules:
      * %j/%J/%u/etc. are valid ONLY in the file name,
        never in a directory component.
      * If the user put a token in the directory (e.g. logs/%j/out.log),
        we flatten it to logs/out.%j.log so Slurm expands it correctly.
      * If the path is relative, it is made relative to the script workdir.
    """
    if not raw:
        return raw, ""

    raw = raw.strip()

    # If relative, make absolute using the script directory
    if not raw.startswith("/"):
        base = os.path.dirname(os.path.abspath(script)) if script else os.getcwd()
        raw = os.path.join(base, raw)

    parent = os.path.dirname(raw)
    fname  = os.path.basename(raw)

    # For paths with Slurm tokens in directory components (e.g. .../%J/out.txt),
    # keep the pattern intact so Slurm expands it correctly at submit time.
    # resolve_existing_slurm_log() will find the actual file when opening logs.
    # dir_to_create = deepest static ancestor that contains no tokens.
    log_path = os.path.join(parent, fname)

    # Walk down until we hit the first directory component holding a token:
    # everything above it is static and can safely be created ahead of sbatch.
    parts_list = list(Path(parent).parts)
    clean_parts = []
    for p in parts_list:
        if _SLURM_TOKENS.search(p):
            break
        clean_parts.append(p)
    static_parent = str(Path(*clean_parts)) if clean_parts else os.sep

    return log_path, static_parent


def submit_job(template: dict) -> tuple[bool, str]:
    script = template.get("script", "")
    if not script:
        return False, "No script specified"

    # Resolve and sanitise log paths BEFORE calling sbatch
    out_raw = template.get("output", "")
    err_raw = template.get("error", "")
    out_path, out_dir = _safe_log_path(out_raw, script)
    err_path, err_dir = _safe_log_path(err_raw, script)

    # Create base directories (no tokens) so Slurm can write
    for d in (out_dir, err_dir):
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:
                return False, f"Could not create log directory '{d}': {e}"

    args = ["sbatch"]
    if template.get("job_name"):  args += [f"--job-name={template['job_name']}"]
    if template.get("partition"): args += [f"--partition={template['partition']}"]
    if template.get("nodes"):     args += [f"--nodes={template['nodes']}"]
    if template.get("ntasks"):    args += [f"--ntasks={template['ntasks']}"]
    if template.get("cpus"):      args += [f"--cpus-per-task={template['cpus']}"]
    if template.get("gpus"):      args += [f"--gres=gpu:{template['gpus']}"]
    if template.get("mem"):       args += [f"--mem={template['mem']}"]
    if template.get("time"):      args += [f"--time={template['time']}"]
    if template.get("account"):   args += [f"--account={template['account']}"]
    if out_path: args += [f"--output={out_path}"]
    if err_path: args += [f"--error={err_path}"]
    if template.get("extra"):
        for tok in shlex.split(template["extra"]):
            args.append(tok)
    args.append(script)
    return run_sbatch(args)

def expand_array_job(jobid_base: str) -> list[dict]:
    if not is_valid_jobid(jobid_base):
        return []
    try:
        # JobName last: it is user-supplied and may contain "|".
        out = run_out([
            "sacct", "-j", jobid_base, "-X", "-n", "-P",
            "--format=JobID,State,ExitCode,Elapsed,NodeList,Start,End,JobName"
        ])
    except Exception:
        return []
    tasks = []
    for line in out.splitlines():
        parts = line.strip().split("|", 7)
        if len(parts) < 8:
            continue
        jid = parts[0].strip()
        if not jid or jid == jobid_base:
            continue
        raw_state = parts[1].strip().split()
        tasks.append({
            "jobid": jid, "name": parts[7].strip(),
            "state": raw_state[0] if raw_state else "UNKNOWN",
            "exitcode": parts[2].strip(),
            "elapsed": parts[3].strip(), "nodes": parts[4].strip(),
            "start": parts[5].strip(), "end": parts[6].strip(),
        })
    return tasks

def get_dependency_tree(jobid: str, depth: int = 0, visited: set = None) -> list:
    if visited is None:
        visited = set()
    if jobid in visited or depth > 5 or not is_valid_jobid(jobid):
        return []
    visited.add(jobid)
    try:
        out = run_out(["scontrol", "show", "job", jobid])
        m_state = re.search(r"JobState=(\S+)", out)
        m_dep   = re.search(r"Dependency=(\S+)", out)
        m_name  = re.search(r"JobName=(\S+)", out)
        state   = m_state.group(1) if m_state else "UNKNOWN"
        dep_str = m_dep.group(1)   if m_dep   else "(none)"
        name    = m_name.group(1)  if m_name  else ""
    except Exception:
        return [(depth, jobid, "UNKNOWN", "(none)", "")]
    result = [(depth, jobid, state, dep_str, name)]
    if dep_str and dep_str not in ("(null)", "(none)"):
        for dep in dep_str.split(","):
            # Slurm writes "afterok:12:13" for several ids on one clause, and
            # also uses aftercorr/afterburstbuffer — the old pattern matched
            # only the first id of the four "after*" types it knew about.
            m = re.match(
                r"(?:afterok|afterany|afternotok|aftercorr|afterburstbuffer|after)"
                r":([\d:_+]+)", dep.strip())
            if m:
                for dep_id in m.group(1).split(":"):
                    dep_id = dep_id.split("+")[0]
                    if dep_id:
                        result += get_dependency_tree(dep_id, depth + 1, visited)
    return result

# ──────────────────────────────────────────────
#  SLURM VALUE PARSERS
# ──────────────────────────────────────────────
def parse_slurm_duration(t: str) -> float:
    """Seconds from a Slurm duration.

    Accepts every shape sacct emits: "1-02:03:04", "02:03:04", "12:34.567"
    (TotalCPU uses MM:SS.mmm) and bare seconds.
    """
    t = (t or "").strip()
    if not t or t.upper() in ("UNLIMITED", "N/A", "INVALID", "UNKNOWN", "NONE"):
        return 0.0
    days = 0
    if "-" in t:
        d, _, t = t.partition("-")
        try:
            days = int(d)
        except ValueError:
            return 0.0
    parts = t.split(":")
    try:
        nums = [float(x) for x in parts]
    except ValueError:
        return 0.0
    if len(parts) == 3:
        h, m, s = nums
    elif len(parts) == 2:
        h, m, s = 0.0, nums[0], nums[1]
    elif len(parts) == 1:
        h, m, s = 0.0, 0.0, nums[0]
    else:
        return 0.0
    return days * 86400 + h * 3600 + m * 60 + s


_MEM_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([KMGTP]?)([nc]?)$", re.IGNORECASE)
_MEM_FACTOR = {"K": 1.0 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024, "P": 1024.0 ** 3}


def parse_mem(value: str) -> tuple[float, str]:
    """(megabytes, scope) from a Slurm memory string.

    scope is "c" for per-CPU, "n" for per-node (Slurm appends these to
    ReqMem) or "" when the figure is already a total.  An absent unit means
    megabytes, which is what Slurm assumes for ReqMem.
    """
    value = (value or "").strip()
    if not value or value.upper() in ("N/A", "UNKNOWN", "0"):
        return 0.0, ""
    m = _MEM_RE.match(value)
    if not m:
        return 0.0, ""
    number = float(m.group(1))
    unit = (m.group(2) or "M").upper()
    scope = (m.group(3) or "").lower()
    return number * _MEM_FACTOR.get(unit, 1.0), scope


def parse_tres(tres: str) -> dict[str, str]:
    """Parse "cpu=4,mem=16G,node=1,gres/gpu=2" into a dict."""
    out: dict[str, str] = {}
    for item in (tres or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, val = item.partition("=")
        out[key.strip().lower()] = val.strip()
    return out


def tres_gpu_count(tres: str) -> int:
    """GPU count from an AllocTRES string, across the spellings in use."""
    d = parse_tres(tres)
    for key in ("gres/gpu", "gpu"):
        if key in d:
            m = re.search(r"\d+", d[key])
            if m:
                return int(m.group())
    for key, val in d.items():
        if key.startswith("gres/gpu:"):
            m = re.search(r"\d+", val)
            if m:
                return int(m.group())
    return 0


# ──────────────────────────────────────────────
#  JOB EFFICIENCY  (seff-style, from sacct)
# ──────────────────────────────────────────────
def get_job_efficiency(jobids: list[str]) -> dict[str, dict]:
    """CPU and memory efficiency per job, measured rather than estimated.

    Queries sacct WITHOUT -X on purpose: MaxRSS is only recorded on the step
    rows (.batch/.0), while the allocation row carries Elapsed, NCPUS, NNodes
    and ReqMem.  The rows are merged back together per base job id.
    """
    jobids = [j for j in jobids if is_valid_jobid(j)]
    if not jobids:
        return {}

    rows: list[list[str]] = []
    for i in range(0, len(jobids), 50):
        out = run_out([
            "sacct", "-j", ",".join(jobids[i:i + 50]), "-n", "-P",
            "--format=JobID,State,Elapsed,TotalCPU,NCPUS,NNodes,ReqMem,MaxRSS,AllocTRES,ExitCode",
        ])
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 10:
                rows.append([p.strip() for p in parts[:10]])

    jobs: dict[str, dict] = {}
    for jid, state, elapsed, totalcpu, ncpus, nnodes, reqmem, maxrss, tres, exitcode in rows:
        base = jid.split(".")[0]
        is_step = "." in jid
        rec = jobs.setdefault(base, {
            "jobid": base, "state": "", "elapsed_s": 0.0, "totalcpu_s": 0.0,
            "ncpus": 0, "nnodes": 0, "req_mem_mb": 0.0, "max_rss_mb": 0.0,
            "gpus": 0, "exitcode": "", "step_cpu_s": 0.0,
        })
        cpu_s = parse_slurm_duration(totalcpu)
        rss_mb, _ = parse_mem(maxrss)
        if is_step:
            # Steps contribute MaxRSS (peak across them) and, as a fallback,
            # the CPU time when the allocation row does not carry it.
            rec["max_rss_mb"] = max(rec["max_rss_mb"], rss_mb)
            rec["step_cpu_s"] += cpu_s
        else:
            rec["state"] = state.split()[0].upper() if state else rec["state"]
            rec["exitcode"] = exitcode or rec["exitcode"]
            rec["elapsed_s"] = parse_slurm_duration(elapsed)
            rec["totalcpu_s"] = cpu_s
            try:
                rec["ncpus"] = int(ncpus or 0)
            except ValueError:
                rec["ncpus"] = 0
            try:
                rec["nnodes"] = int(nnodes or 0)
            except ValueError:
                rec["nnodes"] = 0
            mem_mb, scope = parse_mem(reqmem)
            rec["req_mem_mb"] = mem_mb
            rec["req_mem_scope"] = scope
            rec["gpus"] = tres_gpu_count(tres)
            rec["max_rss_mb"] = max(rec["max_rss_mb"], rss_mb)

    for rec in jobs.values():
        if not rec["totalcpu_s"]:
            rec["totalcpu_s"] = rec["step_cpu_s"]
        rec.pop("step_cpu_s", None)

        # ReqMem may be expressed per CPU or per node; scale to a total.
        scope = rec.pop("req_mem_scope", "")
        if scope == "c":
            rec["req_mem_mb"] *= max(rec["ncpus"], 1)
        elif scope == "n":
            rec["req_mem_mb"] *= max(rec["nnodes"], 1)

        core_seconds = rec["elapsed_s"] * rec["ncpus"]
        rec["cpu_eff"] = round(rec["totalcpu_s"] / core_seconds * 100, 1) if core_seconds > 0 else None
        rec["mem_eff"] = round(rec["max_rss_mb"] / rec["req_mem_mb"] * 100, 1) if rec["req_mem_mb"] > 0 else None
        rec["cpu_hours"] = round(rec["elapsed_s"] * rec["ncpus"] / 3600.0, 2)
        rec["gpu_hours"] = round(rec["elapsed_s"] * rec["gpus"] / 3600.0, 2)
        # Memory that was reserved and never touched — the number that
        # matters for cluster citizenship.
        rec["wasted_mem_mb"] = round(max(0.0, rec["req_mem_mb"] - rec["max_rss_mb"]), 1)
    return jobs


def efficiency_verdict(rec: dict) -> tuple[str, str]:
    """(label, style) summarising how well a job used what it reserved."""
    cpu, mem = rec.get("cpu_eff"), rec.get("mem_eff")
    if cpu is None and mem is None:
        return "no data", "dim"
    if (cpu is not None and cpu < 25) or (mem is not None and mem < 15):
        return "over-allocated", "bold red"
    if (cpu is not None and cpu < 60) or (mem is not None and mem < 40):
        return "loose fit", "yellow"
    if mem is not None and mem > 95:
        return "memory-tight", "bold magenta"
    return "good fit", "bold green"


def efficiency_suggestions(rec: dict) -> list[str]:
    """Concrete, copy-pasteable advice derived from an efficiency record."""
    tips: list[str] = []
    cpu, mem = rec.get("cpu_eff"), rec.get("mem_eff")
    ncpus = max(int(rec.get("ncpus") or 0), 1)

    if mem is not None and rec.get("req_mem_mb"):
        peak_mb = rec.get("max_rss_mb", 0.0)
        if mem < 50 and peak_mb > 0:
            # Round a 20% headroom up to a friendly GB figure.
            suggest_gb = max(1, int((peak_mb * 1.2) / 1024 + 0.999))
            tips.append(f"Request about --mem={suggest_gb}G instead of "
                        f"{rec['req_mem_mb']/1024:.1f}G (peak was "
                        f"{peak_mb/1024:.2f}G plus 20% headroom).")
        elif mem > 95:
            tips.append("Peak memory nearly hit the request — raise --mem a little "
                        "to avoid an OUT_OF_MEMORY kill.")

    if cpu is not None:
        if cpu < 40 and ncpus > 1:
            useful = max(1, round(ncpus * cpu / 100))
            tips.append(f"Only ~{useful} of {ncpus} cores were busy; try "
                        f"--cpus-per-task={useful} unless the job is I/O bound.")
        elif cpu > 95 and ncpus > 1:
            tips.append("CPU use was near perfect — more cores may cut wall time.")

    if rec.get("state") == "TIMEOUT":
        tips.append("The job hit its wall clock limit; raise --time or checkpoint.")
    if rec.get("state") == "OUT_OF_MEMORY":
        tips.append("The job was killed for exceeding its memory request; raise --mem.")

    if not tips:
        tips.append("Resource request looks well matched to actual usage.")
    return tips


# ──────────────────────────────────────────────
#  WHY IS MY JOB PENDING?  (sprio / sshare)
# ──────────────────────────────────────────────
def get_job_priority(jobid: str) -> dict:
    """Priority factor breakdown for a pending job, via sprio."""
    if not is_valid_jobid(jobid):
        return {}
    out = run_out(["sprio", "-j", jobid, "-h", "-o", "%i|%Y|%A|%F|%J|%P|%Q|%T|%N"])
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 9 or not parts[0]:
            continue
        def num(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0
        return {
            "jobid": parts[0], "total": num(parts[1]), "age": num(parts[2]),
            "fairshare": num(parts[3]), "jobsize": num(parts[4]),
            "partition": num(parts[5]), "qos": num(parts[6]),
            "tres": num(parts[7]), "nice": num(parts[8]),
        }
    return {}


def get_priority_queue_position(jobid: str, partition: str = "") -> tuple[int, int]:
    """(rank, total) of a pending job among pending jobs in its partition."""
    if not is_valid_jobid(jobid):
        return (0, 0)
    cmd = ["squeue", "-h", "-t", "PENDING", "-o", "%i|%Q", "--sort=-p"]
    if partition and _NODENAME_RE.match(partition):
        cmd += ["-p", partition]
    out = run_out(cmd)
    ids = [ln.split("|")[0].strip() for ln in out.strip().splitlines() if ln.strip()]
    try:
        return (ids.index(jobid) + 1, len(ids))
    except ValueError:
        return (0, len(ids))


def get_fairshare() -> dict:
    """The current user's fairshare figures, via sshare."""
    out = run_out(["sshare", "-U", "-n", "-P", "-o",
                   "Account,User,RawUsage,EffectvUsage,FairShare"])
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[1]:
            continue
        def f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
        return {"account": parts[0], "user": parts[1], "raw_usage": f(parts[2]),
                "effective_usage": f(parts[3]), "fairshare": f(parts[4])}
    return {}


PENDING_REASON_HELP = {
    "Resources":        "The cluster is busy; your job is waiting for nodes to free up.",
    "Priority":         "Jobs with a higher priority are queued ahead of yours.",
    "Dependency":       "A job you depend on has not finished yet (press E for the tree).",
    "DependencyNeverSatisfied": "A dependency failed — this job will never start. Cancel it.",
    "QOSMaxJobsPerUserLimit":   "You already run the maximum jobs allowed by this QOS.",
    "QOSMaxCpuPerUserLimit":    "You already hold the maximum CPUs allowed by this QOS.",
    "AssocMaxJobsLimit":        "Your account has reached its concurrent job limit.",
    "AssocGrpCpuLimit":         "Your account has reached its CPU limit.",
    "AssocGrpGRES":             "Your account has reached its GPU limit.",
    "PartitionTimeLimit":       "The requested wall time exceeds the partition limit.",
    "PartitionNodeLimit":       "The requested node count exceeds the partition limit.",
    "ReqNodeNotAvail":          "A requested node is down, drained or reserved.",
    "JobHeldUser":              "You put this job on hold — press U to release it.",
    "JobHeldAdmin":             "An administrator put this job on hold.",
    "BeginTime":                "The job has a --begin time that has not arrived yet.",
    "Licenses":                 "Waiting for a software license to be released.",
    "ReqNodeNotAvail,":         "A requested node is unavailable.",
    "None":                     "Slurm has not recorded a blocking reason yet.",
}


def explain_pending_reason(reason: str) -> str:
    reason = (reason or "").strip().strip("()")
    if not reason:
        return ""
    if reason in PENDING_REASON_HELP:
        return PENDING_REASON_HELP[reason]
    for key, text in PENDING_REASON_HELP.items():
        if reason.startswith(key):
            return text
    return ""


# ──────────────────────────────────────────────
#  PARTITION LIMITS
# ──────────────────────────────────────────────
def get_partition_info() -> dict[str, dict]:
    """Limits per partition from `scontrol show partition -o`."""
    out = run_out(["scontrol", "show", "partition", "-o"])
    parts: dict[str, dict] = {}
    for line in out.strip().splitlines():
        fields = dict(
            kv.split("=", 1) for kv in line.split() if "=" in kv
        )
        name = fields.get("PartitionName", "").strip()
        if not name:
            continue
        parts[name] = {
            "name":       name,
            "max_time":   fields.get("MaxTime", ""),
            "def_time":   fields.get("DefaultTime", ""),
            "max_nodes":  fields.get("MaxNodes", ""),
            "total_nodes": fields.get("TotalNodes", ""),
            "total_cpus": fields.get("TotalCPUs", ""),
            "def_mem_per_cpu": fields.get("DefMemPerCPU", ""),
            "max_mem_per_node": fields.get("MaxMemPerNode", ""),
            "state":      fields.get("State", ""),
            "default":    fields.get("Default", "NO") == "YES",
        }
    return parts


# ──────────────────────────────────────────────
#  NODE METRICS WITHOUT SSH
# ──────────────────────────────────────────────
def get_node_info_scontrol(node: str) -> dict:
    """Node usage from `scontrol show node`, which needs no SSH access.

    Many sites forbid users logging into compute nodes, which made the
    SSH-only monitor permanently blank there.  This is the default source;
    SSH only adds per-process detail on top.
    """
    if not is_valid_nodename(node):
        return {}
    out = run_out(["scontrol", "show", "node", node, "-o"])
    if not out.strip():
        return {}
    fields = dict(kv.split("=", 1) for kv in out.split() if "=" in kv)

    def num(key, default=0.0):
        try:
            return float(fields.get(key, default))
        except (TypeError, ValueError):
            return default

    cpu_tot = num("CPUTot")
    cpu_alloc = num("CPUAlloc")
    real_mem = num("RealMemory")
    alloc_mem = num("AllocMem")
    free_mem = num("FreeMem")
    load = num("CPULoad")
    gres = fields.get("Gres", "") or ""
    gres_used = fields.get("GresUsed", "") or ""

    def gpu_num(text):
        # Gres spellings: "gpu:4", "gpu:a100:4", "gpu:a100:4(IDX:0-3)".
        # The optional middle group is the GPU model, whose own digits
        # ("a100") must not be mistaken for the count.
        m = re.search(r"gpu:(?:[^,()\s:]+:)?(\d+)", text or "")
        return int(m.group(1)) if m else 0

    return {
        "node":        node,
        "state":       fields.get("State", ""),
        "cpu_alloc":   int(cpu_alloc),
        "cpu_total":   int(cpu_tot),
        "cpu_pct":     int(cpu_alloc / cpu_tot * 100) if cpu_tot else 0,
        "load":        load,
        "load_pct":    int(min(load / cpu_tot * 100, 100)) if cpu_tot else 0,
        "mem_total_mb": real_mem,
        "mem_alloc_mb": alloc_mem,
        "mem_free_mb":  free_mem,
        "mem_pct":     int(alloc_mem / real_mem * 100) if real_mem else 0,
        "gpu_total":   gpu_num(gres),
        "gpu_alloc":   gpu_num(gres_used),
        "reason":      fields.get("Reason", ""),
    }


# ──────────────────────────────────────────────
#  TIME HELPERS
# ──────────────────────────────────────────────
def parse_slurm_datetime(text: str):
    """datetime from a Slurm timestamp, or None for Unknown/N/A/(null)."""
    text = (text or "").strip()
    if not text or text.upper() in ("UNKNOWN", "N/A", "(NULL)", "NONE", "INVALID"):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def humanize_delta(seconds: float) -> str:
    """Compact duration: '3d 4h', '5h 20m', '45m', '30s'."""
    seconds = int(abs(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


# ──────────────────────────────────────────────
#  RESERVATIONS
# ──────────────────────────────────────────────
def _split_list_field(value: str) -> list:
    """Slurm writes an empty list as '(null)' or 'none'."""
    value = (value or "").strip()
    if not value or value.lower() in ("(null)", "none", "n/a"):
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_reservations() -> list:
    """Cluster reservations from `scontrol show reservation -o`."""
    out = run_out(["scontrol", "show", "reservation", "-o"])
    reservations = []
    for line in out.strip().splitlines():
        line = line.strip()
        # scontrol says "No reservations in the system" when there are none.
        if not line or line.lower().startswith("no reservations"):
            continue
        fields = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
        name = fields.get("ReservationName", "").strip()
        if not name:
            continue
        reservations.append({
            "name":       name,
            "state":      fields.get("State", "").strip(),
            "start_time": fields.get("StartTime", "").strip(),
            "end_time":   fields.get("EndTime", "").strip(),
            "duration":   fields.get("Duration", "").strip(),
            "nodes":      fields.get("Nodes", "").strip(),
            "node_cnt":   fields.get("NodeCnt", "").strip(),
            "core_cnt":   fields.get("CoreCnt", "").strip(),
            "partition":  fields.get("PartitionName", "").strip(),
            "features":   fields.get("Features", "").strip(),
            "flags":      _split_list_field(fields.get("Flags", "")),
            "users":      _split_list_field(fields.get("Users", "")),
            "groups":     _split_list_field(fields.get("Groups", "")),
            "accounts":   _split_list_field(fields.get("Accounts", "")),
            "tres":       fields.get("TRES", "").strip(),
        })
    return reservations


def get_my_accounts() -> list:
    """Accounts this user belongs to, for matching against reservations."""
    accounts = []
    out = run_out(["sacctmgr", "-n", "-P", "show", "assoc",
                   f"user={MY_USER}", "format=Account"])
    for line in out.strip().splitlines():
        acct = line.strip()
        if acct and acct not in accounts:
            accounts.append(acct)
    if not accounts:
        # sacctmgr is not always readable by ordinary users; sshare is.
        share = get_fairshare()
        if share.get("account"):
            accounts.append(share["account"])
    return accounts


def reservation_is_mine(res: dict, user: str, accounts=None) -> bool:
    """True when this user may submit into the reservation.

    A maintenance window that merely blocks the cluster is not "mine", which
    is the distinction that matters when a job will not start.
    """
    accounts = accounts or []
    if user and user in res.get("users", []):
        return True
    if any(a in res.get("accounts", []) for a in accounts if a):
        return True
    return False


def reservation_status(res: dict, now=None):
    """(label, style) describing where a reservation sits in time."""
    now = now or datetime.now()
    start = parse_slurm_datetime(res.get("start_time", ""))
    end = parse_slurm_datetime(res.get("end_time", ""))
    if start and now < start:
        return (f"starts in {humanize_delta((start - now).total_seconds())}", C.INFO)
    if start and end and start <= now <= end:
        return (f"active, {humanize_delta((end - now).total_seconds())} left",
                "bold green")
    if end and now > end:
        return ("ended", C.FG_FAINT)
    state = (res.get("state") or "").upper()
    if state == "ACTIVE":
        return ("active", f"bold {C.OK}")
    return (state.lower() or "unknown", C.FG)


def reservation_blocks_jobs(res: dict) -> bool:
    """MAINT reservations without IGNORE_JOBS hold the nodes hostage."""
    flags = [f.upper() for f in res.get("flags", [])]
    return "MAINT" in flags and "IGNORE_JOBS" not in flags


# ──────────────────────────────────────────────
#  WATCHLIST
# ──────────────────────────────────────────────
def load_watchlist(path=None) -> list:
    path = path or WATCHLIST_FILE
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    seen = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        jobid = str(entry.get("jobid", "")).strip()
        if not jobid or jobid in seen or not is_valid_jobid(jobid):
            continue
        seen.add(jobid)
        out.append({
            "jobid": jobid,
            "name":  str(entry.get("name", "")),
            "added": str(entry.get("added", "")),
        })
    return out


def save_watchlist(items: list, path=None) -> bool:
    path = path or WATCHLIST_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(items, indent=2))
        tmp.replace(path)
        return True
    except Exception:
        return False


def toggle_watch(items: list, jobid: str, name: str = ""):
    """Pin or unpin a job. Returns (items, is_now_pinned)."""
    jobid = str(jobid or "").strip()
    if not is_valid_jobid(jobid):
        return items, False
    for i, entry in enumerate(items):
        if entry.get("jobid") == jobid:
            del items[i]
            return items, False
    items.append({
        "jobid": jobid,
        "name":  name or "",
        "added": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return items, True


def watchlist_ids(items: list) -> set:
    return {e.get("jobid", "") for e in items if e.get("jobid")}


# ──────────────────────────────────────────────
#  SUBMIT TEMPLATES
# ──────────────────────────────────────────────
TEMPLATE_FIELDS = ["script", "job_name", "partition", "account", "nodes",
                   "ntasks", "gpus", "cpus", "mem", "time", "output", "error", "extra"]


def load_templates(path: Path | None = None) -> list[dict]:
    path = path or TEMPLATE_FILE
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("name"):
            vals = entry.get("values", {})
            if isinstance(vals, dict):
                out.append({"name": str(entry["name"]),
                            "values": {k: str(v) for k, v in vals.items()
                                       if k in TEMPLATE_FIELDS}})
    return out


def save_templates(templates: list[dict], path: Path | None = None) -> bool:
    path = path or TEMPLATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(templates, indent=2))
        tmp.replace(path)
        return True
    except Exception:
        return False


def upsert_template(templates: list[dict], name: str, values: dict) -> list[dict]:
    name = (name or "").strip()
    if not name:
        return templates
    clean = {k: v for k, v in values.items() if k in TEMPLATE_FIELDS}
    for entry in templates:
        if entry["name"] == name:
            entry["values"] = clean
            return templates
    templates.append({"name": name, "values": clean})
    return templates


# ──────────────────────────────────────────────
#  ARRAY RE-RUN
# ──────────────────────────────────────────────
TERMINAL_STATES = {"COMPLETED", "CD", "FAILED", "F", "TIMEOUT", "TO",
                   "CANCELLED", "CA", "OUT_OF_MEMORY", "OOM", "NODE_FAIL",
                   "NF", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}

_ARRAY_FAIL_STATES = {"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
                      "CANCELLED", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}


def aggregate_efficiency(records: dict[str, dict]) -> dict:
    """Roll per-job efficiency records up into cluster-citizenship figures."""
    usable = [r for r in records.values()
              if r.get("cpu_eff") is not None or r.get("mem_eff") is not None]
    if not usable:
        return {}
    cpu_effs = [r["cpu_eff"] for r in usable if r.get("cpu_eff") is not None]
    mem_effs = [r["mem_eff"] for r in usable if r.get("mem_eff") is not None]
    core_hours = sum(r.get("cpu_hours", 0.0) for r in usable)
    # Core-hours reserved but never used, the figure an HPC admin cares about.
    wasted_core_hours = sum(
        r.get("cpu_hours", 0.0) * max(0.0, 1 - (r["cpu_eff"] or 0) / 100)
        for r in usable if r.get("cpu_eff") is not None)
    gb_hours_reserved = sum(
        r.get("req_mem_mb", 0.0) / 1024 * r.get("elapsed_s", 0.0) / 3600 for r in usable)
    gb_hours_used = sum(
        r.get("max_rss_mb", 0.0) / 1024 * r.get("elapsed_s", 0.0) / 3600 for r in usable)
    buckets = {"0-25%": 0, "25-50%": 0, "50-75%": 0, "75-100%": 0}
    for value in cpu_effs:
        if value < 25:   buckets["0-25%"] += 1
        elif value < 50: buckets["25-50%"] += 1
        elif value < 75: buckets["50-75%"] += 1
        else:            buckets["75-100%"] += 1
    worst = sorted(
        usable,
        key=lambda r: r.get("cpu_hours", 0.0) * max(0.0, 1 - (r.get("cpu_eff") or 0) / 100),
        reverse=True)[:10]
    return {
        "jobs": len(usable),
        "mean_cpu_eff": round(sum(cpu_effs) / len(cpu_effs), 1) if cpu_effs else None,
        "mean_mem_eff": round(sum(mem_effs) / len(mem_effs), 1) if mem_effs else None,
        "core_hours": round(core_hours, 1),
        "wasted_core_hours": round(wasted_core_hours, 1),
        "gpu_hours": round(sum(r.get("gpu_hours", 0.0) for r in usable), 1),
        "gb_hours_reserved": round(gb_hours_reserved, 1),
        "gb_hours_used": round(gb_hours_used, 1),
        "buckets": buckets,
        "worst": worst,
    }


def failed_task_indices(tasks: list[dict]) -> list[int]:
    """Array indices of the tasks that did not complete successfully."""
    out = []
    for t in tasks:
        state = (t.get("state") or "").split()[0].upper() if t.get("state") else ""
        if state not in _ARRAY_FAIL_STATES:
            continue
        jid = str(t.get("jobid", ""))
        if "_" not in jid:
            continue
        idx = jid.split("_", 1)[1].split(".")[0]
        if idx.isdigit():
            out.append(int(idx))
    return sorted(set(out))


def compact_indices(indices: list[int]) -> str:
    """[1,2,3,7,9,10] -> "1-3,7,9-10" (Slurm --array syntax)."""
    if not indices:
        return ""
    idx = sorted(set(indices))
    runs, start, prev = [], idx[0], idx[0]
    for value in idx[1:]:
        if value == prev + 1:
            prev = value
            continue
        runs.append((start, prev))
        start = prev = value
    runs.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def build_array_rerun_args(submit_line: str, indices: list[int]) -> list[str] | None:
    """sbatch argv that re-runs only `indices` of an array job.

    Any --array already present is dropped, so the new selection wins rather
    than being appended twice.
    """
    args = build_sbatch_args(submit_line)
    if not args or not indices:
        return None
    cleaned, skip_next = [], False
    for tok in args[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("--array=") or tok.startswith("-a="):
            continue
        if tok in ("--array", "-a"):
            skip_next = True
            continue
        cleaned.append(tok)
    return ["sbatch", f"--array={compact_indices(indices)}"] + cleaned


# ──────────────────────────────────────────────
#  COMPLETION HOOK
# ──────────────────────────────────────────────
def run_completion_hook(hook: str, jobid: str, state: str, name: str) -> None:
    """Fire the user's completion hook. Never raises, never blocks the UI."""
    hook = (hook or "").strip()
    if not hook:
        return
    hook_path = os.path.expanduser(hook)
    if not (os.path.isfile(hook_path) and os.access(hook_path, os.X_OK)):
        return
    try:
        with open(os.devnull, "r") as devnull:
            subprocess.run(
                [hook_path, str(jobid), str(state), str(name)],
                stdin=devnull, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, close_fds=True,
            )
    except Exception:
        pass


# ──────────────────────────────────────────────
#  MODAL: CONFIRM
# ──────────────────────────────────────────────
class SubmitJobModal(ModalScreen):
    """TUI form to build and submit an sbatch job. Supports saving/loading templates."""
    DEFAULT_CSS = """
    SubmitJobModal { align: center middle; }
    #submit-dialog {
        width: 90%; max-width: 88; height: 90%; max-height: 40;
        background: $sq-panel; border: solid $sq-line;
        padding: 1 2; layout: vertical;
    }
    /* The form outgrew a short terminal once ntasks was added, so the
       fields scroll while the buttons stay pinned to the bottom. */
    #submit-fields { height: 1fr; }
    #submit-title { color: $sq-primary; text-style: bold; margin-bottom: 1; }
    #partition-hint { color: $sq-fg-faint; height: auto; }
    .field-row { height: 3; layout: horizontal; margin-bottom: 0; }
    .field-lbl { width: 18; color: $sq-fg-muted; content-align: right middle; padding-right: 1; }
    .field-inp { width: 1fr; height: 3; border: solid $sq-line; background: $sq-bg; color: $sq-fg; }
    .field-inp:focus { border: solid $sq-primary; }
    #submit-btn-row { height: 3; layout: horizontal; margin-top: 1; }
    #btn-submit-run    { background: $sq-ok; color: white; border: none; min-width: 16; margin-right: 1; }
    #btn-submit-save   { background: $sq-primary; color: white; border: none; min-width: 16; margin-right: 1; }
    #btn-submit-load   { background: $sq-violet; color: white; border: none; min-width: 16; margin-right: 1; }
    #btn-submit-cancel { background: $sq-elevated; color: $sq-fg; border: none; min-width: 10; }
    #btn-submit-run:hover    { background: $sq-ok-hover; }
    #btn-submit-save:hover   { background: $sq-primary-hover; }
    #btn-submit-load:hover   { background: $sq-violet; }
    #btn-submit-cancel:hover { background: $sq-line; }
    #submit-status { color: $sq-fg-muted; margin-top: 1; }
    """

    def __init__(self, initial: dict | None = None):
        super().__init__()
        self._initial = initial or {}
        self._partitions: dict[str, dict] = {}

    def _field(self, label: str, fid: str, placeholder: str, val: str = ""):
        with Horizontal(classes="field-row"):
            yield Label(label, classes="field-lbl")
            yield Input(value=val, placeholder=placeholder, id=f"si-{fid}",
                        classes="field-inp")

    def compose(self) -> ComposeResult:
        v = self._initial
        with Vertical(id="submit-dialog"):
            yield Label("Submit new job — sbatch", id="submit-title")
            with VerticalScroll(id="submit-fields"):
                yield from self._field("Script (.sh):",  "script",    "/path/to/job.sh", v.get("script", ""))
                yield from self._field("Job name:",      "job_name",  "my_job",          v.get("job_name", ""))
                yield from self._field("Partition:",     "partition", "gpu_part",        v.get("partition", ""))
                yield from self._field("Account:",       "account",   "my_account",      v.get("account", ""))
                yield from self._field("Nodes:",         "nodes",     "1",               v.get("nodes", ""))
                yield from self._field("Tasks (ntasks):", "ntasks",   "1",               v.get("ntasks", ""))
                yield from self._field("GPUs per node:", "gpus",      "4",               v.get("gpus", ""))
                yield from self._field("CPUs per task:", "cpus",      "40",              v.get("cpus", ""))
                yield from self._field("Memory:",        "mem",       "64G",             v.get("mem", ""))
                yield from self._field("Max time:",      "time",      "2:00:00",         v.get("time", ""))
                yield from self._field("Output log:",    "output",    "logs/%j.out",     v.get("output", ""))
                yield from self._field("Error log:",     "error",     "logs/%j.err",     v.get("error", ""))
                yield from self._field("Args extra:",    "extra",     "--exclusive",     v.get("extra", ""))
            yield Label("", id="partition-hint")
            with Horizontal(id="submit-btn-row"):
                yield Button("Submit",           id="btn-submit-run")
                yield Button("Save template",    id="btn-submit-save")
                yield Button("Load template",    id="btn-submit-load")
                yield Button("Close  Esc",       id="btn-submit-cancel")
            yield Label("", id="submit-status")

    def on_mount(self) -> None:
        self._load_partitions()

    @work(thread=True)
    def _load_partitions(self) -> None:
        info = get_partition_info()
        self.app.call_from_thread(self._apply_partitions, info)

    def _apply_partitions(self, info: dict) -> None:
        if not self.is_attached:
            return
        self._partitions = info
        names = ", ".join(sorted(info)) or "(none reported)"
        self.query_one("#partition-hint", Label).update(
            f"  Partitions: {names[:110]}")
        self._update_partition_hint()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "si-partition":
            self._update_partition_hint()

    def _update_partition_hint(self) -> None:
        """Show the selected partition's limits, so a request that the
        partition can never satisfy is visible before sbatch rejects it."""
        if not self.is_attached or not self._partitions:
            return
        hint = self.query_one("#partition-hint", Label)
        try:
            chosen = self.query_one("#si-partition", Input).value.strip()
        except Exception:
            return
        if not chosen:
            names = ", ".join(sorted(self._partitions))
            hint.update(f"  Partitions: {names[:110]}")
            return
        p = self._partitions.get(chosen)
        if not p:
            close = [n for n in self._partitions if n.startswith(chosen)]
            hint.update(f"  [{C.WARN}]Unknown partition '{chosen}'[/]"
                        + (f" — did you mean {', '.join(close[:4])}?" if close else ""))
            return
        bits = [f"MaxTime {p['max_time']}"]
        if p["max_nodes"]:        bits.append(f"MaxNodes {p['max_nodes']}")
        if p["def_mem_per_cpu"]:  bits.append(f"DefMem/CPU {p['def_mem_per_cpu']}M")
        if p["max_mem_per_node"]: bits.append(f"MaxMem/Node {p['max_mem_per_node']}M")
        bits.append(f"State {p['state']}")
        hint.update(f"  [#4d8dfb]{chosen}[/]: " + "  ·  ".join(bits))

    def _get_values(self) -> dict:
        return {f: self.query_one(f"#si-{f}", Input).value.strip()
                for f in TEMPLATE_FIELDS}

    def _set_values(self, values: dict) -> None:
        for field in TEMPLATE_FIELDS:
            try:
                self.query_one(f"#si-{field}", Input).value = values.get(field, "")
            except Exception:
                pass
        self._update_partition_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        status = self.query_one("#submit-status", Label)
        if bid == "btn-submit-cancel":
            self.dismiss(None)
        elif bid == "btn-submit-save":
            vals = self._get_values()
            if not vals.get("script"):
                status.update(f"[bold {C.ERR}]✗ Nothing to save — fill the form first[/]")
                return
            self.app.push_screen(
                TextPromptModal("Save template as", "e.g. gpu-training",
                                vals.get("job_name", "")),
                callback=lambda name: self._save_template(name, vals))
        elif bid == "btn-submit-load":
            self.app.push_screen(TemplatePickerModal(load_templates()),
                                 callback=self._template_chosen)
        elif bid == "btn-submit-run":
            vals = self._get_values()
            if not vals.get("script"):
                status.update(f"[bold {C.ERR}]✗ A script path is required[/]")
                return
            script = os.path.expanduser(vals["script"])
            if not os.path.isfile(script):
                status.update(f"[bold {C.ERR}]✗ Script not found: {script}[/]")
                return
            # sbatch has a 30 s timeout — running it inline froze the whole
            # TUI until the controller answered.
            status.update(f"[{C.FG_FAINT}]Submitting…[/]")
            event.button.disabled = True
            self._worker_submit(vals)

    # ── templates ──────────────────────────────────────────────────────
    def _save_template(self, name: str | None, values: dict) -> None:
        if not name:
            return
        templates = upsert_template(load_templates(), name, values)
        status = self.query_one("#submit-status", Label)
        if save_templates(templates):
            status.update(f"[bold {C.OK}]✓ Template '{name}' saved[/]")
        else:
            status.update(f"[bold {C.ERR}]✗ Could not write {TEMPLATE_FILE}[/]")

    def _template_chosen(self, result) -> None:
        if not result:
            return
        action, entry = result
        status = self.query_one("#submit-status", Label)
        if action == "load":
            self._set_values(entry["values"])
            status.update(f"[bold {C.OK}]✓ Loaded template '{entry['name']}'[/]")
        elif action == "delete":
            remaining = [t for t in load_templates() if t["name"] != entry["name"]]
            if save_templates(remaining):
                status.update(f"[bold {C.WARN}]Template '{entry['name']}' deleted[/]")
            else:
                status.update(f"[bold {C.ERR}]✗ Could not update the template file[/]")

    @work(thread=True)
    def _worker_submit(self, vals: dict) -> None:
        ok, msg = submit_job(vals)
        self.app.call_from_thread(self._submit_done, vals, ok, msg)

    def _submit_done(self, vals: dict, ok: bool, msg: str) -> None:
        if not self.is_attached:
            return
        status = self.query_one("#submit-status", Label)
        self.query_one("#btn-submit-run", Button).disabled = False
        if ok:
            status.update(f"[bold {C.OK}]✓ Submitted: {msg}[/]")
            self.app.notify(f"Job submitted: {msg}", timeout=5)
            self.set_timer(2.0, lambda: self.dismiss(vals))
        else:
            status.update(f"[bold {C.ERR}]✗ Error: {msg}[/]")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)



class ArrayJobModal(ModalScreen):
    """Shows all tasks of an array job with individual state."""
    DEFAULT_CSS = """
    ArrayJobModal { align: center middle; }
    #array-dialog { width: 92%; max-width: 96; height: 80%; max-height: 32; background: $sq-panel;
                    border: solid $sq-line; padding: 1 2; layout: vertical; }
    #array-title  { color: $sq-amber; text-style: bold; margin-bottom: 1; }
    #array-table  { height: 1fr; }
    #array-summary { color: $sq-fg-muted; margin-top: 1; }
    #array-btn-row { height: 3; margin-top: 1; align: left middle; }
    #btn-array-rerun { background: $sq-primary; color: white; border: none;
                       min-width: 26; margin-right: 1; }
    #btn-array-rerun:hover { background: $sq-primary-hover; }
    #btn-array-rerun.disabled { background: $sq-elevated; color: $sq-fg-faint; }
    #btn-array-close { background: $sq-elevated; color: $sq-fg; border: none;
                       min-width: 14; }
    """

    def __init__(self, jobid: str):
        super().__init__()
        self._jobid = jobid
        self._tasks: list[dict] = []
        self._failed: list[int] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="array-dialog"):
            yield Label(f"Array job {self._jobid} — loading tasks...", id="array-title")
            tbl = DataTable(id="array-table")
            tbl.add_columns("TASK ID", "NAME",   "STATE",  "EXIT", "ELAPSED", "NODES", "START")
            yield tbl
            yield Label("", id="array-summary")
            with Horizontal(id="array-btn-row"):
                yield Button("Rerun failed tasks", id="btn-array-rerun")
                yield Button("Close  Esc", id="btn-array-close")

    def on_mount(self) -> None:
        self._worker_load()

    @work(thread=True)
    def _worker_load(self) -> None:
        tasks = expand_array_job(self._jobid)
        self.app.call_from_thread(self._populate, tasks)

    def _populate(self, tasks: list[dict]) -> None:
        # The worker may finish after the user pressed Esc; querying a
        # detached screen raises NoMatches inside call_from_thread.
        if not self.is_attached:
            return
        self._tasks = tasks
        self._failed = failed_task_indices(tasks)
        btn = self.query_one("#btn-array-rerun", Button)
        if self._failed:
            btn.label = f"Rerun {len(self._failed)} failed task(s)"
            btn.set_class(False, "disabled")
        else:
            btn.label = "No failed tasks"
            btn.set_class(True, "disabled")
        tbl = self.query_one("#array-table", DataTable)
        if not tasks:
            self.query_one("#array-title", Label).update(
                f"[{C.WARN}]Array job {self._jobid} — no tasks found in sacct[/]")
            return
        state_count = {}
        for t in tasks:
            st = t["state"]
            state_count[st] = state_count.get(st, 0) + 1
            style = state_style(st)
            tbl.add_row(
                Text(t["jobid"],   style=C.INFO),
                Text(t["name"][:22]),
                Text(st,           style=style),
                Text(t["exitcode"],style=C.ERR if t["exitcode"] not in ("0:0","") else "dim"),
                Text(t["elapsed"], style=C.FG),
                Text(t["nodes"][:20]),
                Text(t["start"][:16], style=C.FG_FAINT),
            )
        total = len(tasks)
        ok    = state_count.get("COMPLETED", 0)
        fail  = sum(v for k, v in state_count.items() if k in ("FAILED","TIMEOUT","OUT_OF_MEMORY"))
        run   = state_count.get("RUNNING", 0)
        pend  = state_count.get("PENDING", 0)
        self.query_one("#array-title", Label).update(
            f"Array job [bold {C.INFO}]{self._jobid}[/] — {total} tasks")
        self.query_one("#array-summary", Label).update(
            f"  [{C.OK}]✓ {ok} COMPLETED[/]  "
            f"[{C.ERR}]✗ {fail} FAILED/TIMEOUT[/]  "
            f"[{C.INFO}]▶ {run} RUNNING[/]  "
            f"[{C.FG}]{pend} PENDING[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-array-close":
            self.dismiss(None)
        elif event.button.id == "btn-array-rerun":
            self._start_rerun()

    def _start_rerun(self) -> None:
        if not self._failed:
            self.app.notify("No failed tasks to rerun", severity="warning")
            return
        self.app.notify("Recovering the original submit line…", timeout=3)
        self._worker_prepare_rerun()

    @work(thread=True)
    def _worker_prepare_rerun(self) -> None:
        base = self._jobid.split("_")[0]
        submit_line = get_submit_line(base)
        args = build_array_rerun_args(submit_line, self._failed) if submit_line else None
        self.app.call_from_thread(self._confirm_rerun, args, submit_line)

    def _confirm_rerun(self, args: list[str] | None, submit_line: str) -> None:
        if not self.is_attached:
            return
        if not args:
            detail = (f"Recovered line is not an sbatch command:\n{submit_line}"
                      if submit_line else
                      "sacct/scontrol did not return the original submit line.")
            self.app.notify(f"Cannot rebuild the submission. {detail}",
                            severity="error", timeout=8)
            return
        preview = " ".join(shlex.quote(a) for a in args)
        self.app.push_screen(
            ConfirmModal("Rerun failed array tasks",
                         f"{len(self._failed)} task(s): "
                         f"{compact_indices(self._failed)}\n\n{preview[:200]}"),
            callback=lambda ok: self._do_rerun(ok, args))

    def _do_rerun(self, confirmed: bool, args: list[str]) -> None:
        if confirmed:
            self._worker_submit_rerun(args)

    @work(thread=True)
    def _worker_submit_rerun(self, args: list[str]) -> None:
        ok, msg = run_sbatch(args)
        self.app.call_from_thread(self._rerun_done, ok, msg)

    def _rerun_done(self, ok: bool, msg: str) -> None:
        if ok:
            self.app.notify(f"Failed tasks resubmitted: {msg}",
                            severity="information", timeout=7)
            self.dismiss(None)
        else:
            self.app.notify(f"Resubmission failed: {msg}", severity="error", timeout=8)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class DependencyTreeModal(ModalScreen):
    """Shows the dependency tree of a job in ASCII."""
    DEFAULT_CSS = """
    DependencyTreeModal { align: center middle; }
    #dep-dialog { width: 90%; max-width: 86; height: 75%; max-height: 30; background: $sq-panel;
                  border: solid $sq-line; padding: 1 2; layout: vertical; }
    #dep-title  { color: $sq-violet; text-style: bold; margin-bottom: 1; }
    #dep-log    { height: 1fr; border: solid $sq-elevated; background: $sq-bg; }
    #btn-dep-close { background: $sq-elevated; color: $sq-fg; border: none;
                     min-width: 14; margin-top: 1; }
    """

    def __init__(self, jobid: str):
        super().__init__()
        self._jobid = jobid

    def compose(self) -> ComposeResult:
        with Vertical(id="dep-dialog"):
            yield Label(f"Dependencies for job {self._jobid} — loading...",
                        id="dep-title")
            yield RichLog(id="dep-log", highlight=False, markup=False, wrap=False)
            yield Button("Close  Esc", id="btn-dep-close")

    def on_mount(self) -> None:
        self._worker_load()

    @work(thread=True)
    def _worker_load(self) -> None:
        tree = get_dependency_tree(self._jobid)
        self.app.call_from_thread(self._render_dep_tree, tree)

    def _render_dep_tree(self, tree: list) -> None:
        if not self.is_attached:
            return
        log = self.query_one("#dep-log", RichLog)
        STATE_ICONS = {
            "RUNNING":   ("▶", f"bold {C.INFO}"),
            "COMPLETED": ("✓", f"bold {C.OK}"),
            "PENDING":   ("·", C.FG_MUTED),
            "FAILED":    ("✗", f"bold {C.ERR}"),
            "TIMEOUT":   ("⧗", f"bold {C.WARN}"),
            "CANCELLED": ("⊘", C.WARN),
            "UNKNOWN":   ("?", C.FG_FAINT),
        }
        if not tree:
            log.write(Text("  No dependencies found or job not in scontrol.", style=C.FG_FAINT))
            self.query_one("#dep-title", Label).update(
                f"Dependencies for [bold {C.INFO}]{self._jobid}[/] — none")
            return
        self.query_one("#dep-title", Label).update(
            f"Dependency tree — job [bold {C.INFO}]{self._jobid}[/]")
        for row in tree:
            depth, jid, state, dep_str, name = row
            icon, col = STATE_ICONS.get(state, ("?", C.FG_FAINT))
            if depth == 0:
                prefix = ""
            else:
                prefix = "  " * (depth - 1) + "  └─ depends on: "
            dep_info = f"  [dep: {dep_str}]" if dep_str not in ("(none)", "(null)") else ""
            line = Text()
            line.append(prefix, style=C.FG_FAINT)
            line.append(f"{icon} ", style=col)
            line.append(f"Job {jid}", style=f"bold {C.FG}")
            if name:
                line.append(f" ({name})", style=C.FG_MUTED)
            line.append(f"  [{state}]", style=col)
            if dep_info:
                line.append(dep_info, style=C.FG_FAINT)
            log.write(line)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-dep-close":
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class EfficiencyModal(ModalScreen):
    """seff-style report: what a job reserved versus what it actually used."""
    DEFAULT_CSS = """
    EfficiencyModal { align: center middle; }
    #eff-dialog { width: 92%; max-width: 92; height: 80%; max-height: 34;
                  background: $sq-panel; border: solid $sq-primary; padding: 1 2;
                  layout: vertical; }
    #eff-title { color: $sq-primary; text-style: bold; margin-bottom: 1; }
    #eff-log   { height: 1fr; border: solid $sq-elevated; background: $sq-bg; }
    #btn-eff-close { background: $sq-elevated; color: $sq-fg; border: none;
                     min-width: 14; margin-top: 1; }
    """

    def __init__(self, jobid: str, job_name: str = "") -> None:
        super().__init__()
        self._jobid = jobid
        self._job_name = job_name

    def compose(self) -> ComposeResult:
        with Vertical(id="eff-dialog"):
            yield Label(f"Efficiency for job {self._jobid} — loading…", id="eff-title")
            yield RichLog(id="eff-log", highlight=False, markup=False, wrap=False)
            yield Button("Close  Esc", id="btn-eff-close")

    def on_mount(self) -> None:
        self._worker_load()

    @work(thread=True)
    def _worker_load(self) -> None:
        data = get_job_efficiency([self._jobid])
        self.app.call_from_thread(self._render_report, data.get(self._jobid))

    def _render_report(self, rec: dict | None) -> None:
        if not self.is_attached:
            return
        log = self.query_one("#eff-log", RichLog)
        title = self.query_one("#eff-title", Label)
        if not rec:
            title.update(f"[{C.WARN}]Efficiency — job {self._jobid}: no accounting data[/]")
            log.write(Text("  sacct returned nothing for this job.", style=C.FG_FAINT))
            log.write(Text("  Accounting may be disabled, or the job is too old.", style=C.FG_FAINT))
            return
        verdict, vstyle = efficiency_verdict(rec)
        title.update(f"Efficiency — job [bold {C.INFO}]{self._jobid}[/] {self._job_name}")

        def line(t="", s="white"):
            log.write(Text(t, style=s))

        def meter(label, pct, detail, invert_ok=False):
            if pct is None:
                line(f"  {label:<10} n/a   {detail}", C.FG_FAINT)
                return
            shown = min(int(pct), 100)
            col = bar_color(100 - shown) if not invert_ok else bar_color(shown)
            line(f"  {label:<10} [{make_bar(shown, 28)}] {pct:>6.1f}%   {detail}", col)

        line(f"  State     : {rec['state']}   Exit: {rec['exitcode']}", C.FG)
        line(f"  Wall time : {rec['elapsed_s']/3600:.2f} h over "
             f"{rec['ncpus']} CPU(s) on {rec['nnodes']} node(s)", C.FG)
        line("")
        line("── EFFICIENCY " + "─" * 50, "bold #29313c")
        meter("CPU", rec["cpu_eff"],
              f"used {rec['totalcpu_s']/3600:.2f} h of {rec['cpu_hours']:.2f} core-hours reserved")
        meter("Memory", rec["mem_eff"],
              f"peak {rec['max_rss_mb']/1024:.2f} GB of {rec['req_mem_mb']/1024:.2f} GB requested")
        line("")
        line(f"  Verdict   : {verdict}", vstyle)
        if rec["wasted_mem_mb"] > 0 and rec["req_mem_mb"] > 0:
            line(f"  Unused RAM: {rec['wasted_mem_mb']/1024:.2f} GB reserved and never touched",
                 "yellow" if rec["wasted_mem_mb"] > 1024 else "dim")
        line("")
        line("── RESOURCES BILLED " + "─" * 44, "bold #29313c")
        line(f"  CPU-hours : {rec['cpu_hours']:.2f}", C.PRIMARY_SOFT)
        if rec["gpus"]:
            line(f"  GPU-hours : {rec['gpu_hours']:.2f}  ({rec['gpus']} GPU(s))", "bold #dd8a4c")
        line("")
        line("── SUGGESTION " + "─" * 50, "bold #29313c")
        for tip in efficiency_suggestions(rec):
            line(f"  • {tip}", C.FG_MUTED)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-eff-close":
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class PriorityModal(ModalScreen):
    """Explains why a pending job has not started yet."""
    DEFAULT_CSS = """
    PriorityModal { align: center middle; }
    #prio-dialog { width: 92%; max-width: 92; height: 80%; max-height: 32;
                   background: $sq-panel; border: solid $sq-violet; padding: 1 2;
                   layout: vertical; }
    #prio-title { color: $sq-violet-hover; text-style: bold; margin-bottom: 1; }
    #prio-log   { height: 1fr; border: solid $sq-elevated; background: $sq-bg; }
    #btn-prio-close { background: $sq-elevated; color: $sq-fg; border: none;
                      min-width: 14; margin-top: 1; }
    """

    def __init__(self, jobid: str, reason: str = "", partition: str = "",
                 state: str = "") -> None:
        super().__init__()
        self._jobid = jobid
        self._reason = reason
        self._partition = partition
        self._state = state

    def compose(self) -> ComposeResult:
        with Vertical(id="prio-dialog"):
            yield Label(f"Why is job {self._jobid} waiting? — loading…", id="prio-title")
            yield RichLog(id="prio-log", highlight=False, markup=False, wrap=True)
            yield Button("Close  Esc", id="btn-prio-close")

    def on_mount(self) -> None:
        self._worker_load()

    @work(thread=True)
    def _worker_load(self) -> None:
        prio = get_job_priority(self._jobid)
        rank, total = get_priority_queue_position(self._jobid, self._partition)
        share = get_fairshare()
        est = get_start_estimates().get(self._jobid, "")
        self.app.call_from_thread(self._render_priority, prio, rank, total, share, est)

    def _render_priority(self, prio: dict, rank: int, total: int, share: dict, est: str) -> None:
        if not self.is_attached:
            return
        log = self.query_one("#prio-log", RichLog)
        self.query_one("#prio-title", Label).update(
            f"Why is job [bold {C.INFO}]{self._jobid}[/] waiting?")

        def line(t="", s="white"):
            log.write(Text(t, style=s))

        reason = (self._reason or "").strip()
        line("── BLOCKING REASON " + "─" * 45, "bold #29313c")
        line(f"  Slurm reports: {reason or '(none recorded)'}", f"bold {C.WARN}")
        explanation = explain_pending_reason(reason)
        if explanation:
            line(f"  {explanation}", C.FG)
        if est:
            line(f"  Estimated start: {est}", f"bold {C.INFO}")
        line("")

        if rank and total:
            line("── QUEUE POSITION " + "─" * 46, "bold #29313c")
            scope = f"partition {self._partition}" if self._partition else "the cluster"
            line(f"  #{rank} of {total} pending jobs in {scope}", C.FG)
            pct = int((1 - (rank - 1) / total) * 100) if total else 0
            line(f"  [{make_bar(pct, 30)}] ahead of {pct}% of the queue", bar_color(pct))
            line("")

        if prio:
            line("── PRIORITY BREAKDOWN " + "─" * 42, "bold #29313c")
            line(f"  Total priority: {prio['total']}", f"bold {C.FG}")
            factors = [("Age", prio["age"]), ("Fairshare", prio["fairshare"]),
                       ("Job size", prio["jobsize"]), ("Partition", prio["partition"]),
                       ("QOS", prio["qos"]), ("TRES", prio["tres"])]
            biggest = max((v for _, v in factors), default=0) or 1
            for label, value in factors:
                width = int(24 * value / biggest)
                line(f"  {label:<12} {value:>8}  {'█' * width}", C.PRIMARY_SOFT)
            if prio.get("nice"):
                line(f"  Nice penalty {prio['nice']:>8}", C.FG_FAINT)
            dominant = max(factors, key=lambda kv: kv[1])[0] if any(v for _, v in factors) else ""
            if dominant:
                line(f"  Largest contribution: {dominant}", C.FG_FAINT)
            line("")
        else:
            line("  (sprio is unavailable on this cluster — no priority breakdown)", C.FG_FAINT)
            line("")

        if share:
            line("── YOUR FAIRSHARE " + "─" * 46, "bold #29313c")
            fs = share["fairshare"]
            pct = int(max(0.0, min(1.0, fs)) * 100)
            line(f"  Fairshare factor: {fs:.4f}  [{make_bar(pct, 24)}]", bar_color(pct))
            line(f"  Effective usage : {share['effective_usage']:.4f}"
                 f"   Account: {share['account']}", C.FG_FAINT)
            if fs < 0.2:
                line("  Your recent usage is high, which lowers the priority of new jobs.",
                     "yellow")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-prio-close":
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class TextPromptModal(ModalScreen[Optional[str]]):
    """One-line text prompt. Returns the entered text, or None if cancelled."""
    DEFAULT_CSS = """
    TextPromptModal { align: center middle; }
    #prompt-box { width: 80%; max-width: 64; height: auto; background: $sq-panel;
                  border: solid $sq-primary; padding: 1 2; }
    #prompt-title { color: $sq-primary; text-style: bold; margin-bottom: 1; }
    #prompt-input { border: solid $sq-line; background: $sq-bg; color: $sq-fg; }
    #prompt-input:focus { border: solid $sq-primary; }
    #prompt-buttons { height: 3; margin-top: 1; align: right middle; }
    #btn-prompt-ok { background: $sq-ok; color: white; border: none; min-width: 12; margin-right: 1; }
    #btn-prompt-cancel { background: $sq-elevated; color: $sq-fg; border: none; min-width: 12; }
    """

    def __init__(self, title: str, placeholder: str = "", value: str = "") -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._title, id="prompt-title")
            yield Input(value=self._value, placeholder=self._placeholder, id="prompt-input")
            with Horizontal(id="prompt-buttons"):
                yield Button("OK", id="btn-prompt-ok")
                yield Button("Cancel", id="btn-prompt-cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def _submit(self) -> None:
        self.dismiss(self.query_one("#prompt-input", Input).value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-prompt-ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class TemplatePickerModal(ModalScreen):
    """Pick a saved submit template, or delete one."""
    DEFAULT_CSS = """
    TemplatePickerModal { align: center middle; }
    #tpl-box { width: 80%; max-width: 70; height: auto; max-height: 28;
               background: $sq-panel; border: solid $sq-primary; padding: 1 2; }
    #tpl-title { color: $sq-primary; text-style: bold; margin-bottom: 1; }
    #tpl-table { height: auto; max-height: 16; }
    #tpl-buttons { height: 3; margin-top: 1; align: left middle; }
    #btn-tpl-load   { background: $sq-ok; color: white; border: none; min-width: 14; margin-right: 1; }
    #btn-tpl-delete { background: $sq-err; color: white; border: none; min-width: 14; margin-right: 1; }
    #btn-tpl-cancel { background: $sq-elevated; color: $sq-fg; border: none; min-width: 12; }
    """

    def __init__(self, templates: list[dict]) -> None:
        super().__init__()
        self._templates = templates

    def compose(self) -> ComposeResult:
        with Vertical(id="tpl-box"):
            yield Label("Saved templates", id="tpl-title")
            tbl = DataTable(id="tpl-table")
            tbl.cursor_type = "row"
            tbl.add_columns("NAME", "SCRIPT", "PARTITION", "CPUs", "MEM", "TIME")
            yield tbl
            with Horizontal(id="tpl-buttons"):
                yield Button("Load", id="btn-tpl-load")
                yield Button("Delete", id="btn-tpl-delete")
                yield Button("Cancel  Esc", id="btn-tpl-cancel")

    def on_mount(self) -> None:
        tbl = self.query_one("#tpl-table", DataTable)
        if not self._templates:
            tbl.add_row(Text("(no templates saved yet)", style=f"{C.FG_FAINT} italic"),
                        *[Text("") for _ in range(5)])
            return
        for t in self._templates:
            v = t["values"]
            tbl.add_row(
                Text(t["name"], style=C.FG),
                Text(os.path.basename(v.get("script", ""))[:24]),
                Text(v.get("partition", "")), Text(v.get("cpus", "")),
                Text(v.get("mem", "")), Text(v.get("time", "")),
            )

    def _selected(self) -> dict | None:
        tbl = self.query_one("#tpl-table", DataTable)
        if not self._templates or tbl.row_count == 0:
            return None
        idx = tbl.cursor_row
        return self._templates[idx] if 0 <= idx < len(self._templates) else None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-tpl-cancel":
            self.dismiss(None)
        elif bid == "btn-tpl-load":
            entry = self._selected()
            self.dismiss(("load", entry) if entry else None)
        elif bid == "btn-tpl-delete":
            entry = self._selected()
            self.dismiss(("delete", entry) if entry else None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class BulkCancelModal(ModalScreen[Optional[list]]):
    """Cancel several jobs at once behind a typed confirmation."""
    DEFAULT_CSS = """
    BulkCancelModal { align: center middle; }
    #bulk-box { width: 88%; max-width: 78; height: auto; max-height: 30;
                background: $sq-panel; border: double $sq-err-hover; padding: 1 2; }
    #bulk-title { color: $sq-err-hover; text-style: bold; margin-bottom: 1; }
    #bulk-list { height: auto; max-height: 14; border: solid $sq-elevated;
                 background: $sq-bg; margin-bottom: 1; }
    #bulk-warn { color: $sq-warn-hover; margin-bottom: 1; }
    #bulk-input { border: solid $sq-line; background: $sq-bg; color: $sq-fg; }
    #bulk-input:focus { border: solid $sq-err-hover; }
    #bulk-buttons { height: 3; margin-top: 1; align: right middle; }
    #btn-bulk-go { background: $sq-err; color: white; border: none; min-width: 18; margin-right: 1; }
    #btn-bulk-cancel { background: $sq-elevated; color: $sq-fg; border: none; min-width: 12; }
    """
    CONFIRM_WORD = "CANCEL"

    def __init__(self, jobs: list[dict], description: str) -> None:
        super().__init__()
        self._jobs = jobs
        self._description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="bulk-box"):
            yield Label(f"Cancel {len(self._jobs)} job(s) — {self._description}",
                        id="bulk-title")
            yield RichLog(id="bulk-list", highlight=False, markup=False, wrap=False)
            yield Label(f"This cannot be undone. Type {self.CONFIRM_WORD} to confirm:",
                        id="bulk-warn")
            yield Input(placeholder=self.CONFIRM_WORD, id="bulk-input")
            with Horizontal(id="bulk-buttons"):
                yield Button(f"Cancel {len(self._jobs)} jobs", id="btn-bulk-go")
                yield Button("Go back  Esc", id="btn-bulk-cancel")

    def on_mount(self) -> None:
        log = self.query_one("#bulk-list", RichLog)
        for j in self._jobs[:60]:
            log.write(Text(
                f"  {j.get('jobid',''):<12} {j.get('state',''):<10} "
                f"{j.get('partition',''):<12} {j.get('name','')[:28]}",
                style=C.FG))
        if len(self._jobs) > 60:
            log.write(Text(f"  … and {len(self._jobs) - 60} more", style=C.FG_FAINT))
        self.query_one("#bulk-input", Input).focus()

    def _try_confirm(self) -> None:
        typed = self.query_one("#bulk-input", Input).value.strip()
        if typed != self.CONFIRM_WORD:
            self.query_one("#bulk-warn", Label).update(
                f"[bold {C.ERR}]Type {self.CONFIRM_WORD} exactly to confirm.[/]")
            return
        self.dismiss([j["jobid"] for j in self._jobs])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-bulk-go":
            self._try_confirm()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_confirm()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    #confirm-box {
        width: 85%; max-width: 66; height: auto;
        background: $sq-panel; border: double $sq-err-hover; padding: 1 2;
    }
    #confirm-title { text-style: bold; color: $sq-err-hover; margin-bottom: 1; }
    #confirm-msg   { color: $sq-fg; margin-bottom: 1; }
    #confirm-buttons { margin-top: 1; align: center middle; height: 3; }
    Button { margin: 0 1; }
    #btn-yes { background: $sq-err; color: white; border: none; }
    #btn-no  { background: $sq-elevated; color: $sq-fg; border: none; }
    #btn-yes:hover { background: $sq-err-hover; }
    #btn-no:hover  { background: $sq-line; }
    """
    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title, self._message = title, message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._title,   id="confirm-title")
            yield Label(self._message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("Go back",  id="btn-no",  variant="default")
                yield Button("Confirm",  id="btn-yes", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)

# ──────────────────────────────────────────────
#  MODAL: JOB DETAIL
# ──────────────────────────────────────────────
class JobDetailModal(ModalScreen):
    DEFAULT_CSS = """
    JobDetailModal { align: center middle; }
    #detail-box { width: 96%; height: 85%; background: $sq-bg; border: solid $sq-line; }
    #detail-title { background: $sq-panel; color: $sq-primary; text-style: bold; padding: 0 2; height: 1; }
    #detail-text  { height: 1fr; padding: 1 2; }
    #detail-close { background: $sq-elevated; color: $sq-fg; border: none; margin: 0 2 1 2; width: 100%; }
    #detail-close:hover { background: $sq-line; }
    """
    def __init__(self, jobid: str) -> None:
        super().__init__()
        self._jobid = jobid

    def compose(self) -> ComposeResult:
        # The scontrol call used to run here, blocking the UI thread for up
        # to the 10 s command timeout before the modal could even be drawn.
        with Vertical(id="detail-box"):
            yield Label(f"  scontrol show job {self._jobid}", id="detail-title")
            yield TextArea("Loading…", id="detail-text", read_only=True)
            yield Button("Close  Esc", id="detail-close")

    def on_mount(self) -> None:
        self._worker_load()

    @work(thread=True)
    def _worker_load(self) -> None:
        if is_valid_jobid(self._jobid):
            out = run_out(["scontrol", "show", "job", self._jobid])
        else:
            out = f"Invalid job id: {self._jobid!r}"
        self.app.call_from_thread(self._apply_detail, out)

    def _apply_detail(self, out: str) -> None:
        if not self.is_attached:
            return
        if not out.strip():
            out = f"Could not retrieve info for job {self._jobid}.\n(Already finished?)"
        self.query_one("#detail-text", TextArea).text = out

    def on_button_pressed(self, _) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()

# ──────────────────────────────────────────────
#  MODAL: LIVE LOG VIEWER
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
#  HELPER: detect available editors
# ──────────────────────────────────────────────
def detect_editors() -> list[tuple[str, str]]:
    candidates = [
        ("nvim",  "nvim"),
        ("vim",   "vim"),
        ("nano",  "nano"),
        ("vi",    "vi"),
        ("emacs", "emacs"),
        ("micro", "micro"),
        ("hx",    "hx"),
    ]
    found = []
    for label, binary in candidates:
        # shutil.which: no subprocess, so this stays cheap at import time and
        # cannot inherit the terminal's stdin the way `which` did.
        path = shutil.which(binary)
        if path:
            found.append((label, path))
    return found

AVAILABLE_EDITORS: list[tuple[str, str]] = detect_editors()


# ──────────────────────────────────────────────
#  MODAL: EDITOR PICKER
# ──────────────────────────────────────────────
class EditorPickerModal(ModalScreen):
    """Pick an editor — then the LogViewerModal opens it via _launch_editor."""
    DEFAULT_CSS = """
    EditorPickerModal { align: center middle; }
    #picker-box {
        width: 88%; max-width: 62; height: auto; background: $sq-panel;
        border: double $sq-primary; padding: 1 2;
    }
    #picker-title { color: $sq-primary; text-style: bold; margin-bottom: 1; }
    #picker-path  { color: $sq-fg-faint; margin-bottom: 1; }
    .editor-btn {
        width: 100%; background: $sq-elevated; color: $sq-fg;
        border: none; margin-bottom: 1;
    }
    .editor-btn:hover { background: $sq-primary; color: white; }
    #btn-picker-cancel {
        width: 100%; background: $sq-bg; color: $sq-fg-faint;
        border: solid $sq-line; margin-top: 1;
    }
    #btn-picker-cancel:hover { background: $sq-elevated; color: $sq-fg; }
    """

    def __init__(self, file_path: str) -> None:
        super().__init__()
        self._file_path = file_path

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("Open with editor", id="picker-title")
            yield Label(f"  {self._file_path}", id="picker-path")
            if AVAILABLE_EDITORS:
                for label, binary in AVAILABLE_EDITORS:
                    yield Button(f"  {label}   ({binary})",
                                 id=f"editor--{label}", classes="editor-btn")
            else:
                yield Label("  No editors found in PATH.", id="no-editors")
            yield Button("Cancel  Esc", id="btn-picker-cancel")

    def on_mount(self) -> None:
        btns = list(self.query(Button))
        if btns: btns[0].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-picker-cancel":
            self.dismiss(None); return
        if bid.startswith("editor--"):
            label  = bid[len("editor--"):]
            binary = next((b for lbl, b in AVAILABLE_EDITORS if lbl == label), None)
            self.dismiss(binary)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
        elif event.key in ("up", "down"):
            btns = list(self.query(Button))
            if self.focused in btns:
                idx = btns.index(self.focused)
                nxt = (idx - 1) % len(btns) if event.key == "up" else (idx + 1) % len(btns)
                btns[nxt].focus()
            elif btns:
                btns[0].focus()


# Keep alias so nothing else breaks
OpenFileModal = EditorPickerModal


class ResourceMonitorModal(ModalScreen):
    DEFAULT_CSS = """
    ResourceMonitorModal { align: center middle; }
    #mon-box {
        width: 98%; height: 95%; background: $sq-bg;
        border: solid $sq-primary; layout: vertical;
    }
    #mon-title {
        background: $sq-panel; color: $sq-primary; text-style: bold;
        padding: 0 2; height: 1;
    }
    #mon-keys-row {
        height: 1; background: $sq-bg; border-bottom: solid $sq-line;
        align: left middle; padding: 0 2;
    }
    #mon-keys-label { color: $sq-fg-dim; }
    #mon-scroll { height: 1fr; border: none; }
    #mon-content { height: auto; padding: 1 2; }
    #mon-footer {
        height: 3; background: $sq-panel; border-top: solid $sq-line;
        align: left middle; padding: 0 2;
    }
    #mon-refresh-lbl  { color: $sq-primary; margin-right: 2; }
    #btn-mon-refresh  { background: $sq-elevated; color: $sq-fg; border: none; margin-right: 1; min-width: 18; }
    #btn-mon-close    { background: $sq-elevated; color: $sq-fg; border: none; min-width: 16; }
    #btn-mon-refresh:hover { background: $sq-primary; color: white; }
    #btn-mon-close:hover   { background: $sq-line; }
    """

    _timer: Timer | None = None
    REFRESH_SECS = CONFIG["monitor"]["refresh_interval"]

    def __init__(self, jobid: str, job_name: str = "", state: str = "") -> None:
        super().__init__()
        self._jobid    = jobid
        self._job_name = job_name
        self._state    = state

    def compose(self) -> ComposeResult:
        with Vertical(id="mon-box"):
            yield Label(
                f"  Monitor — job {self._jobid}  ·  {self._job_name}  ·  {self._state}",
                id="mon-title"
            )
            with Horizontal(id="mon-keys-row"):
                yield Label(
                    f"  ↑↓/PgUp/PgDn: scroll  │  r: refresh  │  Esc: close  │  "
                    f"auto-refresh every {self.REFRESH_SECS}s",
                    id="mon-keys-label"
                )
            yield RichLog(id="mon-content", highlight=False, markup=False,
                          wrap=False, max_lines=2000)
            with Horizontal(id="mon-footer"):
                yield Label("", id="mon-refresh-lbl")
                yield Button("Refresh  r", id="btn-mon-refresh")
                yield Button("Close  Esc", id="btn-mon-close")

    def on_mount(self) -> None:
        self._do_refresh()
        self._timer = self.set_interval(self.REFRESH_SECS, self._do_refresh)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if   event.button.id == "btn-mon-refresh": self._do_refresh()
        elif event.button.id == "btn-mon-close":   self._close()

    def on_key(self, event) -> None:
        log = self.query_one("#mon-content", RichLog)
        if   event.key == "escape":   self._close()
        elif event.key == "r":        self._do_refresh()
        elif event.key == "up":       log.scroll_relative(y=-3)
        elif event.key == "down":     log.scroll_relative(y=3)
        elif event.key == "pageup":   log.scroll_relative(y=-20)
        elif event.key == "pagedown": log.scroll_relative(y=20)
        elif event.key == "home":     log.scroll_home()
        elif event.key == "end":      log.scroll_end(animate=False)

    def _close(self) -> None:
        if self._timer: self._timer.stop()
        self.dismiss()

    def _do_refresh(self) -> None:
        self._fetch_resources(self._jobid)

    @work(thread=True, exclusive=True, group="monitor")
    def _fetch_resources(self, jobid: str) -> None:
        # exclusive: each refresh SSHes to up to 8 nodes twice with an 8 s
        # timeout, which routinely exceeds the 8 s refresh interval.
        lines: list[tuple[str, str]] = []  # (text, style)

        def sep(title: str = "") -> None:
            if title:
                lines.append((f"── {title} {'─'*(60-len(title))}", "bold #29313c"))
            else:
                lines.append(("─" * 64, C.FG_DIM))

        ts = datetime.now().strftime("%H:%M:%S")

        # ── JOB INFO ──
        sep("JOB INFO")
        squeue_out = run_out(["squeue", "-j", jobid, "-h",
                               "--format=%P|%T|%M|%L|%C|%m|%b|%N"])
        if squeue_out.strip():
            p = squeue_out.strip().split("|")
            if len(p) >= 8:
                elapsed_s  = parse_time_to_secs(p[2])
                timeleft_s = parse_time_to_secs(p[3])
                total_s    = elapsed_s + timeleft_s
                pct_time   = int(elapsed_s / total_s * 100) if total_s > 0 else 0
                bar        = make_bar(pct_time, 30)
                lines.append((f"  Partition : {p[0]}   State: {p[1]}   CPUs: {p[4]}   Mem: {p[5]}   GPUs: {p[6]}", C.FG))
                lines.append((f"  Nodes     : {p[7]}", C.FG))
                lines.append((f"  Elapsed   : {p[2]}  /  Left: {p[3]}", C.FG))
                col = bar_color(pct_time)
                lines.append((f"  Timeline  : [{bar}] {pct_time}%", col))
        else:
            lines.append(("  Job not found in queue (may have already finished)", C.FG_FAINT))

        # ── SSTAT (actual CPU/MEM for the job) ──
        sep("USAGE VIA SSTAT (job accounting)")
        sstat = get_job_sstat(jobid)
        lines.append((f"  AvgCPU: {sstat['avg_cpu']}   MaxRSS: {sstat['max_rss']}   Tasks: {sstat['tasks']}", C.INFO))

        # ── NODES ──
        nodes = get_job_nodes(jobid)
        use_ssh   = CONFIG["monitor"]["use_ssh"]
        max_nodes = CONFIG["monitor"]["max_nodes"]
        if not nodes:
            sep("NODES")
            lines.append(("  No nodes assigned (job still PENDING?)", C.FG_FAINT))
        else:
            for node in nodes[:max_nodes]:
                sep(f"NODE: {node}")

                # ── Allocation, straight from the controller (no SSH) ──
                info = get_node_info_scontrol(node)
                if info:
                    cpu_pct = info["cpu_pct"]
                    lines.append((
                        f"  CPU  [{make_bar(cpu_pct, 20)}] {cpu_pct:>3}%   "
                        f"{info['cpu_alloc']}/{info['cpu_total']} cores allocated"
                        f"   Load: {info['load']:.2f}",
                        bar_color(cpu_pct)))
                    mem_pct = info["mem_pct"]
                    lines.append((
                        f"  MEM  [{make_bar(mem_pct, 20)}] {mem_pct:>3}%   "
                        f"{info['mem_alloc_mb']/1024:.1f} / {info['mem_total_mb']/1024:.1f} GB allocated"
                        f"   (free {info['mem_free_mb']/1024:.1f} GB)",
                        bar_color(mem_pct)))
                    if info["gpu_total"]:
                        gpu_pct = int(info["gpu_alloc"] / info["gpu_total"] * 100)
                        lines.append((
                            f"  GPU  [{make_bar(gpu_pct, 20)}] {gpu_pct:>3}%   "
                            f"{info['gpu_alloc']}/{info['gpu_total']} GPUs allocated",
                            bar_color(gpu_pct)))
                    lines.append((f"  State: {info['state']}", C.FG_FAINT))
                else:
                    lines.append(("  (scontrol returned no data for this node)", C.FG_FAINT))

                if not use_ssh:
                    continue

                # ── Live utilisation, only if SSH to compute nodes is allowed ──
                gpus = get_node_gpu_info(node)
                if gpus:
                    lines.append(("  ── live via ssh ───────────────────────────────────", C.FG_DIM))
                    lines.append(("  GPU  IDX  NAME                      UTIL       MEM USED / TOTAL       TEMP    POWER", "bold #4d8dfb"))
                    for g in gpus:
                        util_bar = make_bar(g["util"], 16)
                        util_col = bar_color(g["util"])
                        mem_pct  = int(g["mem_used"] / g["mem_total"] * 100) if g["mem_total"] > 0 else 0
                        mem_bar  = make_bar(mem_pct, 16)
                        lines.append((
                            f"  GPU  [{g['index']:>2}]  {g['name']:<24}  "
                            f"[{util_bar}] {g['util']:>3}%  "
                            f"[{mem_bar}] {g['mem_used']:>6}/{g['mem_total']:<6} MB  "
                            f"{g['temp']:>4}°C  {g['power']:>6}W",
                            util_col
                        ))
                live = get_node_cpu_mem(node)
                if live.get("mem_total_kb"):
                    if not gpus:
                        lines.append(("  ── live via ssh ───────────────────────────────────", C.FG_DIM))
                    used_gb  = live["mem_used_kb"] / 1024 / 1024
                    total_gb = live["mem_total_kb"] / 1024 / 1024
                    lines.append((
                        f"  USED [{make_bar(live['mem_pct'], 20)}] {live['mem_pct']:>3}%   "
                        f"{used_gb:.1f} / {total_gb:.1f} GB in use   "
                        f"CPU {live['cpu_pct']}%",
                        bar_color(live["mem_pct"])))

        sep()
        source = "scontrol + ssh" if CONFIG["monitor"]["use_ssh"] else "scontrol only (ssh disabled)"
        lines.append((f"  Last update: {ts}  │  Job {jobid}  │  source: {source}", C.FG_FAINT))

        self.app.call_from_thread(self._apply_monitor, lines, ts)

    def _apply_monitor(self, lines: list[tuple[str, str]], ts: str) -> None:
        if not self.is_attached:
            return
        self.query_one("#mon-refresh-lbl", Label).update(f"  ↻ {ts}")
        log = self.query_one("#mon-content", RichLog)
        log.clear()
        for text, style in lines:
            log.write(Text(text, style=style))


class LogViewerModal(ModalScreen):
    DEFAULT_CSS = """
    LogViewerModal { align: center middle; }
    #log-box { width: 98%; height: 95%; background: $sq-bg; border: solid $sq-ok; }
    #log-title { background: $sq-panel; color: $sq-ok; text-style: bold; padding: 0 2; height: 1; }
    #log-tab-row {
        height: 3; background: $sq-panel; border-bottom: solid $sq-line;
        align: left middle; padding: 0 2;
    }
    #btn-show-stdout { background: $sq-primary; color: white; border: none; margin-right: 1; min-width: 18; }
    #btn-show-stderr { background: $sq-warn; color: white; border: none; margin-right: 1; min-width: 18; }
    #btn-show-stdout:hover { background: $sq-primary-hover; }
    #btn-show-stderr:hover { background: $sq-warn-hover; }
    #path-label { color: $sq-fg-faint; margin-left: 2; }
    #log-content { height: 1fr; background: $sq-bg; color: $sq-fg; border: none; padding: 0 1; }
    #log-footer-row {
        height: 3; background: $sq-panel; border-top: solid $sq-line;
        align: left middle; padding: 0 2;
    }
    #refresh-label   { color: $sq-ok; margin-right: 2; }
    #btn-log-refresh { background: $sq-elevated; color: $sq-fg; border: none; margin-right: 1; min-width: 18; }
    #btn-log-refresh  { background: $sq-elevated; color: $sq-fg; border: none; margin-right: 1; min-width: 16; }
    #btn-open-editor  { background: $sq-violet; color: white;   border: none; margin-right: 1; min-width: 22; }
    #btn-log-close    { background: $sq-elevated; color: $sq-fg; border: none; min-width: 16; }
    #btn-log-refresh:hover  { background: $sq-line; }
    #btn-open-editor:hover  { background: $sq-violet; }
    #btn-log-close:hover    { background: $sq-line; }
    #log-keys-row {
        height: 1; background: $sq-bg; border-top: solid $sq-line;
        align: left middle; padding: 0 2;
    }
    #log-keys-label { color: $sq-fg-dim; }
    #log-search-row {
        height: 3; background: $sq-panel; border-top: solid $sq-line;
        align: left middle; padding: 0 2;
    }
    #log-search-lbl { color: $sq-fg-muted; margin-right: 1; width: 10; }
    #log-search { width: 1fr; max-width: 48; border: solid $sq-line;
                  background: $sq-bg; color: $sq-fg; margin-right: 2; }
    #log-search:focus { border: solid $sq-primary; }
    #log-match-lbl { color: $sq-ok; }
    #btn-errors-only { background: $sq-elevated; color: $sq-fg; border: none;
                       min-width: 18; margin-left: 2; }
    #btn-errors-only.on { background: $sq-warn; color: white; }
    """

    _showing: str = "stdout"
    _live: bool = True
    _current_path: str = ""
    _timer: Timer | None = None
    LIVE_REFRESH_SECS = CONFIG["logs"]["live_refresh"]
    MAX_BUFFER_LINES = 5000     # kept in memory so search can look back

    def __init__(self, jobid: str, stdout_path: str, stderr_path: str,
                 job_name: str = "", state: str = "", live: bool = True) -> None:
        super().__init__()
        self._jobid        = jobid
        self._stdout_path  = stdout_path
        self._stderr_path  = stderr_path
        self._job_name     = job_name
        self._state        = state
        self._live         = live
        # Full text kept per stream so search filters instantly and the
        # auto-tail only has to read the bytes that were appended.
        self._buffers: dict[str, list[str]] = {"stdout": [], "stderr": []}
        self._offsets: dict[str, int] = {"stdout": 0, "stderr": 0}
        self._filter = ""
        self._errors_only = False

    def compose(self) -> ComposeResult:
        live_indicator = "  ·  live" if self._live else "  ·  static"
        title = f"  Log — job {self._jobid}  ·  {self._job_name}  ·  {self._state}{live_indicator}"
        with Vertical(id="log-box"):
            yield Label(title, id="log-title")
            with Horizontal(id="log-tab-row"):
                yield Button("stdout", id="btn-show-stdout")
                yield Button("stderr", id="btn-show-stderr")
                yield Label("", id="path-label")
            yield RichLog(id="log-content", highlight=True, markup=False, wrap=True)
            with Horizontal(id="log-search-row"):
                yield Label("Filter", id="log-search-lbl")
                yield Input(placeholder="text or /regex/ …  (press / to focus)",
                            id="log-search")
                yield Label("", id="log-match-lbl")
                yield Button("Errors only", id="btn-errors-only")
            with Horizontal(id="log-keys-row"):
                yield Label(
                    "  /: search  │  ↑↓: scroll  │  PgUp/PgDn  │  r: refresh  │  "
                    "e: editor  │  Esc: close"
                    + (f"  │  auto-tail {self.LIVE_REFRESH_SECS}s" if self._live else ""),
                    id="log-keys-label"
                )
            with Horizontal(id="log-footer-row"):
                live_txt = "live" if self._live else "static"
                yield Label(live_txt, id="refresh-label")
                yield Button("Refresh  r",   id="btn-log-refresh")
                yield Button("Editor  e",     id="btn-open-editor")
                yield Button("Close  Esc",    id="btn-log-close")

    def on_mount(self) -> None:
        self._show_stream("stdout")
        # The header advertised a live tail but nothing ever re-read the file;
        # only a manual "r" refreshed it.
        if self._live:
            self._timer = self.set_interval(
                self.LIVE_REFRESH_SECS, lambda: self._show_stream(self._showing))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if   bid == "btn-show-stdout":  self._show_stream("stdout")
        elif bid == "btn-show-stderr":  self._show_stream("stderr")
        elif bid == "btn-log-refresh":  self._show_stream(self._showing)
        elif bid == "btn-open-editor":  self._pick_editor()
        elif bid == "btn-log-close":    self._close()
        elif bid == "btn-errors-only":
            self._errors_only = not self._errors_only
            event.button.set_class(self._errors_only, "on")
            self._refresh_view()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-search":
            self._filter = event.value
            self._refresh_view()

    def on_key(self, event) -> None:
        search = self.query_one("#log-search", Input)
        if self.focused is search:
            # Let the user type freely; only Esc leaves the search box.
            if event.key == "escape":
                search.value = ""
                self._filter = ""
                self._refresh_view()
                self.query_one("#log-content", RichLog).focus()
                event.stop()
            return
        if   event.key == "slash":     search.focus(); event.stop()
        elif event.key == "escape":    self._close()
        elif event.key == "r":         self._show_stream(self._showing)
        elif event.key == "e":         self._pick_editor()
        elif event.key == "up":        self.query_one("#log-content", RichLog).scroll_relative(y=-3)
        elif event.key == "down":      self.query_one("#log-content", RichLog).scroll_relative(y=3)
        elif event.key == "pageup":    self.query_one("#log-content", RichLog).scroll_relative(y=-20)
        elif event.key == "pagedown":  self.query_one("#log-content", RichLog).scroll_relative(y=20)
        elif event.key == "home":      self.query_one("#log-content", RichLog).scroll_home()
        elif event.key == "end":       self.query_one("#log-content", RichLog).scroll_end(animate=False)

    def _close(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None
        self.dismiss()

    def _pick_editor(self) -> None:
        path = self._current_path
        if not path:
            self.app.notify("No file path available yet", severity="warning"); return
        if not os.path.exists(path):
            self.app.notify(f"File not found: {path}", severity="warning"); return
        if not AVAILABLE_EDITORS:
            self.app.notify("No editors found in PATH (nvim, vim, nano…)", severity="warning"); return
        self.app.push_screen(
            EditorPickerModal(path),
            callback=lambda binary: self._on_editor_picked(binary, path),
        )

    def _on_editor_picked(self, binary: str | None, path: str) -> None:
        if not binary:
            return
        # Close the TUI entirely and replace the process with the editor.
        # This avoids all terminal ownership conflicts with nvim/vim.
        self.app.request_open_editor_and_exit(binary, path)

    # ── Log helpers ─────────────────────────────────────────────────────────

    ERROR_KEYS   = ("error", "exception", "traceback", "fatal", "oom",
                    "segfault", "core dumped", "cuda error")
    WARN_KEYS    = ("warning", "warn", "deprecat")
    SUCCESS_KEYS = ("success", "done", "finished", "completed")

    #  Severity is decided once and named, so the errors-only filter can ask
    #  what a line *is* instead of comparing rendered style strings.
    @classmethod
    def classify(cls, line: str) -> str:
        """'error' | 'warn' | 'ok' | 'plain' for one log line."""
        lower = line.lower()
        if any(k in lower for k in cls.ERROR_KEYS):
            return "error"
        if any(k in lower for k in cls.WARN_KEYS):
            return "warn"
        if any(k in lower for k in cls.SUCCESS_KEYS):
            return "ok"
        return "plain"

    @classmethod
    def line_style(cls, line: str) -> str:
        return {
            "error": f"bold {C.ERR}",
            "warn":  C.WARN,
            "ok":    C.OK,
            "plain": C.FG,
        }[cls.classify(line)]

    @work(thread=True, exclusive=True, group="logview")
    def _show_stream(self, which: str) -> None:
        """Read whatever is new on this stream (runs in a background thread)."""
        self._showing = which
        path = self._stdout_path if which == "stdout" else self._stderr_path

        if not path or not os.path.exists(path):
            msg = f"(File not found: {path})" if path else "(No path available)"
            self.app.call_from_thread(self._apply_stream_error, which, path, msg)
            return

        lines, offset, reset = follow_file(path, self._offsets.get(which, 0),
                                           LOG_TAIL_LINES)
        self.app.call_from_thread(self._apply_stream_lines,
                                  which, path, lines, offset, reset)

    def _apply_stream_error(self, which: str, path: str, msg: str) -> None:
        if not self.is_attached:
            return
        self._current_path = path
        self._buffers[which] = []
        self._offsets[which] = 0
        self._sync_header(which, path)
        log = self.query_one("#log-content", RichLog)
        log.clear()
        log.write(Text(msg, style=f"{C.FG_FAINT} italic"))
        self.query_one("#log-match-lbl", Label).update("")

    def _apply_stream_lines(self, which: str, path: str, lines: list[str],
                            offset: int, reset: bool) -> None:
        if not self.is_attached:
            return
        self._current_path = path
        self._offsets[which] = offset
        if reset:
            self._buffers[which] = list(lines)
        elif lines:
            self._buffers[which].extend(lines)
        # Bound memory on a job that logs forever.
        if len(self._buffers[which]) > self.MAX_BUFFER_LINES:
            del self._buffers[which][:-self.MAX_BUFFER_LINES]
        self._sync_header(which, path)
        if reset or lines:
            self._refresh_view()

    def _sync_header(self, which: str, path: str) -> None:
        self.query_one("#btn-show-stdout", Button).set_class(which == "stdout", "active-log")
        self.query_one("#btn-show-stderr", Button).set_class(which == "stderr", "active-log")
        self.query_one("#path-label", Label).update(f"  {path}")

    def _matcher(self):
        """Predicate for the current filter.

        A term wrapped in slashes is treated as a regular expression; anything
        else is a case-insensitive substring. An invalid regex falls back to a
        substring match rather than erroring while the user is mid-keystroke.
        """
        term = (self._filter or "").strip()
        if not term:
            return None
        if len(term) > 2 and term.startswith("/") and term.endswith("/"):
            inner = term[1:-1]
            try:
                rx = re.compile(inner, re.IGNORECASE)
                return lambda line: bool(rx.search(line))
            except re.error:
                # Half-typed pattern: match the text the user meant, not the
                # delimiters they typed around it.
                term = inner
        low = term.lower()
        return lambda line: low in line.lower()

    def _refresh_view(self) -> None:
        """Redraw the visible stream, honouring the filter and errors toggle."""
        if not self.is_attached:
            return
        log = self.query_one("#log-content", RichLog)
        log.clear()
        buf = self._buffers.get(self._showing, [])
        if not buf:
            log.write(Text("(File is empty)", style=f"{C.FG_FAINT} italic"))
            self.query_one("#log-match-lbl", Label).update("")
            return

        match = self._matcher()
        shown = 0
        for line in buf:
            if not line:
                continue
            level = self.classify(line)
            if self._errors_only and level not in ("error", "warn"):
                continue
            style = self.line_style(line)
            if match and not match(line):
                continue
            log.write(Text(line, style=style))
            shown += 1

        label = self.query_one("#log-match-lbl", Label)
        if match or self._errors_only:
            label.update(f"  {shown} / {len(buf)} lines")
            if shown == 0:
                log.write(Text("(no matching lines)", style=f"{C.FG_FAINT} italic"))
        else:
            label.update(f"  {len(buf)} lines")
        log.scroll_end(animate=False)

# ──────────────────────────────────────────────
#  WIDGETS
# ──────────────────────────────────────────────
class StatsBar(Static):
    def update_stats(self, stats: dict, last_update: str) -> None:
        s  = stats["by_state"]
        r  = s.get("RUNNING",    s.get("R",  0))
        pd = s.get("PENDING",    s.get("PD", 0))
        cg = s.get("COMPLETING", s.get("CG", 0))
        text = Text()
        text.append(f"  {last_update}", style=C.FG_FAINT)
        text.append("    │    ", style=C.FG_FAINT)
        text.append(f"TOTAL {stats['total']}", style=f"bold {C.FG}")
        text.append("  •  ", style=C.FG_FAINT)
        text.append(f"MINE {stats['mine']}", style=f"bold {C.FG}")
        text.append("    │    ", style=C.FG_FAINT)
        text.append(f"RUNNING {r}", style=f"bold {C.OK}")
        text.append("   ")
        text.append(f"PENDING {pd}", style=f"bold {C.INFO}")
        text.append("   ")
        text.append(f"COMPLETING {cg}", style=C.OK)
        text.append("    │    ", style=C.FG_FAINT)
        text.append(f"GPUs in use {stats['running_gpus']}", style=f"bold {C.VIOLET}")
        self.update(text)


class ActionBar(Static):
    DEFAULT_CSS = """
    ActionBar {
        height: 7; background: $sq-panel;
        border-top: solid $sq-line; padding: 0 1; layout: vertical;
    }
    #action-row-1 { height: 3; align: left middle; }
    #action-row-2 { height: 3; align: left middle; }
    ActionBar Button { min-width: 20; }
    #selected-label  {
        color: $sq-fg-muted; margin-right: 2; width: 20; height: 3;
        content-align: left middle;
    }
    #selected-spacer { width: 20; margin-right: 2; height: 3; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="action-row-1"):
            yield Label("Job: -", id="selected-label")
            yield Button("Details  d",  id="btn-detail")
            yield Button("Logs  l",      id="btn-logs")
            yield Button("Monitor  m",  id="btn-monitor")
        with Horizontal(id="action-row-2"):
            yield Label("", id="selected-spacer")
            yield Button("Hold  h",     id="btn-hold")
            yield Button("Release  u",  id="btn-release")
            yield Button("Cancel  x",  id="btn-scancel")

    def set_selected(self, jobid: str | None, pinned: bool = False) -> None:
        lbl = self.query_one("#selected-label", Label)
        if jobid:
            lbl.update(("★ " if pinned else "") + "Job: " + jobid)
        else:
            lbl.update("Job: -")

def cell_by_col(table, name: str) -> str | None:
    """Read the selected row's cell for column `name`.

    The tables swap between FULL/COMPACT/MINIMAL column sets depending on the
    terminal width, so a fixed column index points at a different field on a
    narrow terminal.  Returns None when the column is not currently shown.
    """
    if table is None or table.row_count == 0:
        return None
    cols = [c for c, _ in getattr(table, "COLS", [])]
    if name not in cols:
        return None
    try:
        val = str(table.get_cell_at((table.cursor_row, cols.index(name)))).strip()
    except Exception:
        return None
    return val or None


class HistoryTable(DataTable):
    """Panel 4: searchable history of all past jobs."""
    COLS_FULL = [
        ("JOBID", 10), ("NAME", 22), ("USER", 12), ("PARTITION", 11),
        ("STATE", 11), ("CPUs", 5), ("MEM", 7), ("GPUs", 5),
        ("FIRST SEEN", 18), ("LAST SEEN", 18),
    ]
    COLS_COMPACT = [
        ("JOBID", 10), ("NAME", 20), ("STATE", 11),
        ("GPUs", 5), ("FIRST SEEN", 18), ("LAST SEEN", 18),
    ]
    COLS_MINIMAL = [
        ("JOBID", 10), ("NAME", 18), ("STATE", 11), ("LAST SEEN", 18),
    ]

    COLS = COLS_FULL

    def _pick_cols(self) -> list:
        w = self.app.size.width if self.app else 200
        if w >= 140: return self.COLS_FULL
        if w >= 90:  return self.COLS_COMPACT
        return self.COLS_MINIMAL

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._build_columns()

    def on_resize(self, event) -> None:
        new_cols = self._pick_cols()
        if new_cols != self.COLS:
            self.COLS = new_cols
            self._rebuild_columns()

    def _build_columns(self) -> None:
        self.COLS = self._pick_cols()
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    def _rebuild_columns(self) -> None:
        self.clear(columns=True)
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)
        # clear(columns=True) also drops every row: without this the table
        # stayed empty after a resize until the tab was re-entered.
        self.populate(*self._last_render)

    _last_render: tuple = ([], "")

    def populate(self, history: list[dict], filter_text: str = "") -> None:
        self._last_render = (history, filter_text)
        # Preserve cursor position across refreshes
        selected_jobid = None
        if self.row_count > 0:
            try:
                selected_jobid = str(self.get_cell_at((self.cursor_row, 0))).strip() or None
            except Exception:
                pass
        self.clear()
        ft = filter_text.lower()
        # Show most recent first
        new_cursor = None
        idx = 0
        for e in reversed(history):
            if ft and not any(ft in str(v).lower() for v in e.values()):
                continue
            st = e.get("state", "")
            col_keys = [col for col, _ in self.COLS]
            def hv(key, _e=e, _st=st):
                vals = {
                    "JOBID":      Text(_e.get("jobid", ""),      style=f"bold {C.FG}"),
                    "NAME":       Text(_e.get("name", ""),       style=C.FG),
                    "USER":       Text(_e.get("user", ""),       style=C.FG),
                    "PARTITION":  Text(_e.get("partition", ""),  style=C.FG),
                    "STATE":      Text(_st,                      style=state_style(_st)),
                    "CPUs":       Text(_e.get("cpus", ""),       style=C.FG),
                    "MEM":        Text(_e.get("mem",  ""),       style=C.FG),
                    "GPUs":       Text(_e.get("gpus", ""),       style=C.INFO),
                    "FIRST SEEN": Text(_e.get("first_seen", ""), style=C.FG_FAINT),
                    "LAST SEEN":  Text(_e.get("last_seen",  ""), style=C.FG_FAINT),
                }
                return vals.get(key, Text(""))
            self.add_row(*[hv(k) for k in col_keys])
            if e.get("jobid") == selected_jobid:
                new_cursor = idx
            idx += 1
        if new_cursor is not None:
            self.move_cursor(row=new_cursor)

    def get_selected_jobid(self) -> str | None:
        return cell_by_col(self, "JOBID")


class SqueueTable(DataTable):
    # Full set shown on wide terminals (≥ 140 cols)
    COLS_FULL = [
        ("JOBID", 10), ("PARTITION", 11), ("NAME", 20), ("USER", 12),
        ("STATE", 11), ("TIME", 10), ("TIME LEFT", 11), ("EST. START", 13),
        ("CPUs", 5), ("MEM", 7), ("GPUs", 5), ("NODES", 14), ("REASON", 22),
    ]
    # Compact set for narrow terminals (< 140 cols)
    COLS_COMPACT = [
        ("JOBID", 10), ("NAME", 18), ("USER", 10),
        ("STATE", 11), ("TIME LEFT", 11), ("EST. START", 13),
        ("GPUs", 5), ("REASON", 20),
    ]
    # Minimal set for very narrow terminals (< 90 cols)
    COLS_MINIMAL = [
        ("JOBID", 10), ("NAME", 16), ("STATE", 11), ("TIME LEFT", 11),
    ]

    COLS = COLS_FULL   # active set, updated on resize

    def _pick_cols(self) -> list:
        w = self.app.size.width if self.app else 200
        if w >= 140: return self.COLS_FULL
        if w >= 90:  return self.COLS_COMPACT
        return self.COLS_MINIMAL

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._build_columns()

    def on_resize(self, event) -> None:
        new_cols = self._pick_cols()
        if new_cols != self.COLS:
            self.COLS = new_cols
            self._rebuild_columns()

    def _build_columns(self) -> None:
        self.COLS = self._pick_cols()
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    _last_jobs: list[dict] = []
    pinned: set = set()          # job ids on the watchlist, set by the app

    def _rebuild_columns(self) -> None:
        """Rebuild columns after a resize, then redraw the cached rows."""
        self.clear(columns=True)
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)
        # The old version only called .refresh() (a repaint, not a reload),
        # so the table sat empty until the next 3 s poll.
        self.refresh_jobs(self._last_jobs)

    def refresh_jobs(self, jobs: list[dict]) -> None:
        self._last_jobs = jobs
        selected_jobid = cell_by_col(self, "JOBID")
        self.clear()
        new_cursor = None
        pins = self.pinned
        for idx, j in enumerate(jobs):
            rs = f"bold {C.FG}" if j["user"] == MY_USER else C.FG_MUTED
            st = j["state"]
            def c(val, extra=""): return Text(val, style=f"{rs} {extra}".strip())
            est = j.get("est_start", "")
            is_pinned = j["jobid"] in pins
            # The marker goes on NAME, never on JOBID: the job id cell is
            # parsed back out for every action, so it must stay verbatim.
            name_cell = ("★ " + j["name"]) if is_pinned else j["name"]
            col_keys = [col for col, _ in self.COLS]
            def cv(key):
                vals = {
                    "JOBID": Text(j["jobid"], style=f"bold {C.VIOLET}") if is_pinned
                             else c(j["jobid"]),
                    "PARTITION": c(j["partition"]),
                    "NAME": c(name_cell),   "USER": c(j["user"]),
                    "STATE": Text(st, style=state_style(st)),
                    "TIME": c(j["time"]),   "TIME LEFT": c(j["time_left"]),
                    "EST. START": Text(est or "—", style=C.INFO if est else "dim"),
                    "CPUs": c(j["cpus"]),   "MEM": c(j["mem"]),
                    "GPUs": c(j["gpus"]),   "NODES": c(j["nodes"]),
                    "REASON": c(j["reason"]),
                }
                return vals.get(key, Text(""))
            self.add_row(*[cv(k) for k in col_keys])
            if j["jobid"] == selected_jobid: new_cursor = idx
        if new_cursor is not None: self.move_cursor(row=new_cursor)

    def get_selected_jobid(self) -> str | None:
        return cell_by_col(self, "JOBID")

    def get_selected_user(self) -> str | None:
        # index 3 used to be hardcoded here: on a terminal narrower than 140
        # columns that cell is STATE (or TIME LEFT), so the owner check in
        # scancel/hold/release compared a job state against $USER and refused
        # every action on the user's own jobs.
        return cell_by_col(self, "USER")


class MyJobsTable(DataTable):
    COLS_FULL = [
        ("JOBID", 10), ("NAME", 24), ("STATE", 11), ("TIME", 10),
        ("TIME LEFT", 10), ("EST. START", 13), ("CPUs", 5), ("MEM", 8),
        ("GPUs", 8), ("NODES", 16), ("REASON", 28),
    ]
    COLS_COMPACT = [
        ("JOBID", 10), ("NAME", 20), ("STATE", 11),
        ("TIME LEFT", 10), ("EST. START", 13), ("GPUs", 5), ("REASON", 20),
    ]
    COLS_MINIMAL = [
        ("JOBID", 10), ("NAME", 18), ("STATE", 11), ("TIME LEFT", 10),
    ]

    COLS = COLS_FULL

    def _pick_cols(self) -> list:
        w = self.app.size.width if self.app else 200
        if w >= 140: return self.COLS_FULL
        if w >= 90:  return self.COLS_COMPACT
        return self.COLS_MINIMAL

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._build_columns()

    def on_resize(self, event) -> None:
        new_cols = self._pick_cols()
        if new_cols != self.COLS:
            self.COLS = new_cols
            self._rebuild_columns()

    def _build_columns(self) -> None:
        self.COLS = self._pick_cols()
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    _last_jobs: list[dict] = []
    pinned: set = set()          # job ids on the watchlist, set by the app

    def _rebuild_columns(self) -> None:
        self.clear(columns=True)
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)
        self.refresh_jobs(self._last_jobs)

    def refresh_jobs(self, jobs: list[dict]) -> None:
        self._last_jobs = jobs
        selected_jobid = cell_by_col(self, "JOBID")
        self.clear()
        mine = [j for j in jobs if j.get("user") == MY_USER]
        if not mine:
            self.add_row(
                Text("—", style=C.FG_FAINT),
                Text(f"No jobs found for user {MY_USER}", style=f"{C.FG_FAINT} italic"),
                *[Text("", style=C.FG_FAINT)] * (len(self.COLS) - 2),
            )
            return
        new_cursor = None
        for idx, j in enumerate(mine):
            st = j["state"]
            est = j.get("est_start", "")
            is_pinned = j["jobid"] in self.pinned
            name_cell = ("★ " + j["name"]) if is_pinned else j["name"]
            col_keys = [col for col, _ in self.COLS]
            def mv(key):
                vals = {
                    "JOBID": Text(j["jobid"],
                                  style=f"bold {C.VIOLET}" if is_pinned else "bold yellow"),
                    "NAME":  Text(name_cell,      style=f"bold {C.FG}"),
                    "STATE": Text(st,             style=state_style(st)),
                    "TIME":  Text(j["time"],      style=C.FG),
                    "TIME LEFT": Text(j["time_left"], style=C.FG),
                    "EST. START": Text(est or "—", style=C.INFO if est else "dim"),
                    "CPUs":  Text(j["cpus"],      style=C.FG),
                    "MEM":   Text(j["mem"],       style=C.FG),
                    "GPUs":  Text(j["gpus"],      style=C.FG),
                    "NODES": Text(j["nodes"],     style=C.FG),
                    "REASON":Text(j["reason"],    style=C.FG_FAINT),
                }
                return vals.get(key, Text(""))
            self.add_row(*[mv(k) for k in col_keys])
            if j["jobid"] == selected_jobid: new_cursor = idx
        if new_cursor is not None: self.move_cursor(row=new_cursor)

    def get_selected_jobid(self) -> str | None:
        val = cell_by_col(self, "JOBID")
        return val if val != "—" else None

    def get_selected_user(self) -> str | None:
        return MY_USER


class SinfoTable(DataTable):
    COLS_FULL = [
        ("NODE", 16), ("PARTITION", 12), ("STATE", 10),
        ("CPU A/I/O/T", 12), ("MEM (MB)", 10), ("GRES", 20), ("FEATURES", 30),
    ]
    COLS_COMPACT = [
        ("NODE", 14), ("PARTITION", 11), ("STATE", 10),
        ("CPU A/I/O/T", 12), ("GRES", 18),
    ]
    COLS_MINIMAL = [
        ("NODE", 14), ("STATE", 10), ("GRES", 16),
    ]

    COLS = COLS_FULL
    def _pick_cols(self) -> list:
        w = self.app.size.width if self.app else 200
        if w >= 140: return self.COLS_FULL
        if w >= 90:  return self.COLS_COMPACT
        return self.COLS_MINIMAL

    _last_nodes: list[dict] = []

    def on_resize(self, event) -> None:
        new_cols = self._pick_cols()
        if new_cols != self.COLS:
            self.COLS = new_cols
            self.clear(columns=True)
            for col, width in self.COLS:
                self.add_column(col, width=width, key=col)
            self.refresh_nodes(self._last_nodes)

    STATE_COLORS = {
        "idle": "green", "alloc": "bold green", "mix": "yellow",
        "down": "bold red", "drain": "red", "drng": "red",
    }
    def on_mount(self) -> None:
        self.COLS = self._pick_cols()
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    def refresh_nodes(self, nodes: list[dict]) -> None:
        self._last_nodes = nodes
        self.clear()
        if not nodes:
            self.add_row(*[Text("n/a", style=C.FG_FAINT)] * len(self.COLS)); return
        for n in nodes:
            st = n["state"]
            color = self.STATE_COLORS.get(st.lower().rstrip("*"), C.FG)
            col_keys = [col for col, _ in self.COLS]
            def sv(key, _n=n, _st=st, _c=color):
                vals = {
                    "NODE":        Text(_n["node"],      style=f"bold {_c}"),
                    "PARTITION":   Text(_n["partition"], style=C.FG),
                    "STATE":       Text(_st,             style=_c),
                    "CPU A/I/O/T": Text(_n["cpu_aiotd"], style=C.FG),
                    "MEM (MB)":    Text(_n["mem"],       style=C.FG),
                    "GRES":        Text(_n["gres"],      style=C.INFO),
                    "FEATURES":    Text(_n["features"],  style=C.FG_FAINT),
                }
                return vals.get(key, Text(""))
            self.add_row(*[sv(k) for k in col_keys])


class ReservationTable(DataTable):
    """Panel: cluster reservations, and whether you can use them."""
    COLS_FULL = [
        ("", 3), ("NAME", 18), ("STATE", 22), ("NODES", 18), ("COUNT", 7),
        ("PARTITION", 12), ("START", 17), ("END", 17), ("USERS/ACCOUNTS", 22),
        ("FLAGS", 20),
    ]
    COLS_COMPACT = [
        ("", 3), ("NAME", 16), ("STATE", 20), ("NODES", 16),
        ("PARTITION", 11), ("USERS/ACCOUNTS", 20),
    ]
    COLS_MINIMAL = [("", 3), ("NAME", 16), ("STATE", 20), ("NODES", 14)]

    COLS = COLS_FULL
    _last_rows: list = []

    def _pick_cols(self) -> list:
        w = self.app.size.width if self.app else 200
        if w >= 140: return self.COLS_FULL
        if w >= 90:  return self.COLS_COMPACT
        return self.COLS_MINIMAL

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.COLS = self._pick_cols()
        self._add_columns()

    def _add_columns(self) -> None:
        for idx, (col, width) in enumerate(self.COLS):
            self.add_column(col or " ", width=width, key=f"{col}-{idx}")

    def on_resize(self, event) -> None:
        new_cols = self._pick_cols()
        if new_cols != self.COLS:
            self.COLS = new_cols
            self.clear(columns=True)
            self._add_columns()
            self.populate(self._last_rows)

    def populate(self, reservations: list) -> None:
        """`reservations` carries a precomputed 'mine' flag and status label."""
        self._last_rows = reservations
        self.clear()
        if not reservations:
            self.add_row(*([Text("", style=C.FG_FAINT)] * max(0, len(self.COLS) - 1)),
                         Text("No reservations on this cluster", style=f"{C.FG_FAINT} italic"))
            return
        for res in reservations:
            label, style = res["_status"]
            mine = res["_mine"]
            marker = Text("✓" if mine else ("▲" if res["_blocks"] else "·"),
                          style=f"bold {C.OK}" if mine
                          else ("bold yellow" if res["_blocks"] else "dim"))
            who = ",".join(res["users"] + res["accounts"])[:20] or "—"
            col_keys = [col for col, _ in self.COLS]

            def value(key, _r=res, _l=label, _s=style, _m=marker, _w=who):
                vals = {
                    "":          _m,
                    "NAME":      Text(_r["name"][:18],
                                      style=f"bold {C.FG}" if _r["_mine"] else C.FG_MUTED),
                    "STATE":     Text(_l, style=_s),
                    "NODES":     Text(_r["nodes"][:18] or "—", style=C.INFO),
                    "COUNT":     Text(_r["node_cnt"] or "—", style=C.FG),
                    "PARTITION": Text((_r["partition"] or "—").replace("(null)", "—"),
                                      style=C.FG),
                    "START":     Text(_r["start_time"][:16].replace("T", " "), style=C.FG_FAINT),
                    "END":       Text(_r["end_time"][:16].replace("T", " "), style=C.FG_FAINT),
                    "USERS/ACCOUNTS": Text(_w, style=C.FG),
                    "FLAGS":     Text(",".join(_r["flags"])[:20] or "—", style=C.FG_FAINT),
                }
                return vals.get(key, Text(""))
            self.add_row(*[value(k) for k in col_keys])


class WatchlistTable(DataTable):
    """Panel: jobs the user pinned, surviving restarts and tab changes."""
    COLS_FULL = [
        ("JOBID", 12), ("NAME", 22), ("STATE", 12), ("TIME", 10),
        ("TIME LEFT", 10), ("NODES", 14), ("REASON", 22), ("PINNED", 18),
    ]
    COLS_COMPACT = [
        ("JOBID", 12), ("NAME", 20), ("STATE", 12), ("TIME LEFT", 10), ("REASON", 18),
    ]
    COLS_MINIMAL = [("JOBID", 12), ("NAME", 16), ("STATE", 12)]

    COLS = COLS_FULL
    _last_rows: list = []

    def _pick_cols(self) -> list:
        w = self.app.size.width if self.app else 200
        if w >= 140: return self.COLS_FULL
        if w >= 90:  return self.COLS_COMPACT
        return self.COLS_MINIMAL

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.COLS = self._pick_cols()
        self._add_columns()

    def _add_columns(self) -> None:
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    def on_resize(self, event) -> None:
        new_cols = self._pick_cols()
        if new_cols != self.COLS:
            self.COLS = new_cols
            self.clear(columns=True)
            self._add_columns()
            self.populate(self._last_rows)

    def populate(self, rows: list) -> None:
        selected = cell_by_col(self, "JOBID")
        self._last_rows = rows
        self.clear()
        if not rows:
            self.add_row(
                Text("—", style=C.FG_FAINT),
                Text("Nothing pinned — press P on a job to watch it",
                     style=f"{C.FG_FAINT} italic"),
                *[Text("", style=C.FG_FAINT)] * (len(self.COLS) - 2))
            return
        new_cursor = None
        for idx, row in enumerate(rows):
            state = row.get("state", "")
            col_keys = [col for col, _ in self.COLS]

            def value(key, _r=row, _st=state):
                vals = {
                    "JOBID":     Text(_r["jobid"], style=f"bold {C.FG}"),
                    "NAME":      Text((_r.get("name") or "")[:22], style=C.FG),
                    "STATE":     Text(_st or "…", style=state_style(_st) if _st else "dim"),
                    "TIME":      Text(_r.get("time", ""), style=C.FG),
                    "TIME LEFT": Text(_r.get("time_left", ""), style=C.FG),
                    "NODES":     Text((_r.get("nodes") or "")[:14], style=C.FG),
                    "REASON":    Text((_r.get("reason") or "")[:22], style=C.FG_FAINT),
                    "PINNED":    Text(_r.get("added", "")[:16], style=C.FG_FAINT),
                }
                return vals.get(key, Text(""))
            self.add_row(*[value(k) for k in col_keys])
            if row["jobid"] == selected:
                new_cursor = idx
        if new_cursor is not None:
            self.move_cursor(row=new_cursor)

    def get_selected_jobid(self):
        val = cell_by_col(self, "JOBID")
        return val if val != "—" else None


class EventLog(RichLog):
    """Persistent event log — survives dashboard restarts."""

    def on_mount(self) -> None:
        past = load_event_log(n=200)
        if past:
            self.write(Text.assemble(
                ("─" * 22 + " previous session " + "─" * 21, "dim #6a7583")))
            for line in past:
                if line.startswith("[") and "] " in line:
                    end = line.index("] ")
                    self.write(Text.assemble(
                        (line[:end + 1] + " ", "dim #6a7583"),
                        (line[end + 2:],        C.FG_FAINT),
                    ))
                else:
                    self.write(Text(line, style=C.FG_FAINT))
            self.write(Text.assemble(
                ("─" * 22 + " current session " + "─" * 23,  "dim #29313c")))
        self.scroll_end(animate=False)

    def log_event(self, msg: str, style: str = "white") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.write(Text.assemble((f"[{ts}] ", C.FG_FAINT), (msg, style)))
        append_event_log(ts, msg)


# ──────────────────────────────────────────────
#  APP
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
#  HISTORY STATS PANEL
# ──────────────────────────────────────────────
class HistoryStatsPanel(Static):
    DEFAULT_CSS = """
    HistoryStatsPanel {
        height: 1fr; layout: vertical; background: $sq-bg;
    }
    #stats-toolbar {
        height: 3; background: $sq-panel; border-bottom: solid $sq-line;
        align: left middle; padding: 0 2;
    }
    #stats-toolbar-label { color: $sq-primary; text-style: bold; margin-right: 2; }
    #btn-stats-refresh { background: $sq-elevated; color: $sq-fg; border: none; min-width: 18; margin-right: 1; }
    #btn-stats-refresh:hover { background: $sq-primary; color: white; }
    #btn-stats-efficiency { background: $sq-elevated; color: $sq-fg; border: none; min-width: 22; }
    #btn-stats-efficiency:hover { background: $sq-warn; color: white; }
    #stats-content {
        height: 1fr; border: none;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="stats-toolbar"):
            yield Label("History analysis",           id="stats-toolbar-label")
            yield Button("Recalculate",                id="btn-stats-refresh")
            yield Button("Efficiency report",          id="btn-stats-efficiency")
        yield RichLog(id="stats-content", highlight=False, markup=False,
                      wrap=True, max_lines=5000)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-stats-refresh":
            self.app.refresh_stats()
        elif event.button.id == "btn-stats-efficiency":
            self.app.refresh_efficiency()

    def on_key(self, event) -> None:
        log = self.query_one("#stats-content", RichLog)
        if   event.key == "up":       log.scroll_relative(y=-3)
        elif event.key == "down":     log.scroll_relative(y=3)
        elif event.key == "pageup":   log.scroll_relative(y=-20)
        elif event.key == "pagedown": log.scroll_relative(y=20)
        elif event.key == "home":     log.scroll_home()
        elif event.key == "end":      log.scroll_end(animate=False)

    def render_efficiency(self, records: dict, names: dict) -> None:
        log = self.query_one("#stats-content", RichLog)
        log.clear()

        def line(text: str = "", style: str = "white") -> None:
            log.write(Text(text, style=style))

        def sep(title: str = "") -> None:
            bar = "─" * max(0, 62 - len(title))
            line(f"── {title} {bar}" if title else "─" * 66,
                 f"bold {C.FG_MUTED}" if title else C.FG_DIM)

        agg = aggregate_efficiency(records)
        if not agg:
            line("  No accounting data available for your finished jobs.", C.FG_FAINT)
            line("  sacct must be configured on the cluster for this report.", C.FG_FAINT)
            return

        sep("EFFICIENCY — LAST %d FINISHED JOBS" % agg["jobs"])
        for label, value in (("CPU efficiency", agg["mean_cpu_eff"]),
                             ("Memory efficiency", agg["mean_mem_eff"])):
            if value is None:
                line(f"  Mean {label:<18}: n/a", C.FG_FAINT)
            else:
                line(f"  Mean {label:<18}: [{make_bar(int(value), 28)}] {value:>5.1f}%",
                     bar_color(100 - int(value)))
        line("")
        line(f"  Core-hours consumed   : {agg['core_hours']:.1f} h", C.PRIMARY_SOFT)
        line(f"  Core-hours wasted     : {agg['wasted_core_hours']:.1f} h"
             f"  ({agg['wasted_core_hours']/agg['core_hours']*100:.0f}% of the total)"
             if agg["core_hours"] else
             f"  Core-hours wasted     : {agg['wasted_core_hours']:.1f} h",
             "bold red" if agg["wasted_core_hours"] > agg["core_hours"] * 0.4 else "yellow")
        if agg["gpu_hours"]:
            line(f"  GPU-hours consumed    : {agg['gpu_hours']:.1f} h", "bold #dd8a4c")
        line(f"  RAM reserved          : {agg['gb_hours_reserved']:.0f} GB·h", C.PRIMARY_SOFT)
        line(f"  RAM actually used     : {agg['gb_hours_used']:.0f} GB·h", C.PRIMARY_SOFT)

        sep("CPU EFFICIENCY DISTRIBUTION")
        total = sum(agg["buckets"].values()) or 1
        colours = {"0-25%": f"bold {C.ERR}", "25-50%": C.WARN,
                   "50-75%": C.INFO, "75-100%": f"bold {C.OK}"}
        for bucket, count in agg["buckets"].items():
            width = int(30 * count / total)
            line(f"  {bucket:<9} [{'█' * width}{'░' * (30 - width)}] {count:>4} jobs",
                 colours[bucket])

        sep("BIGGEST WASTE — REVIEW THESE REQUESTS")
        line(f"  {'JOBID':<12}{'NAME':<22}{'CPU%':>6}{'MEM%':>7}{'WASTED':>10}", "bold #4d8dfb")
        for rec in agg["worst"]:
            wasted = rec.get("cpu_hours", 0.0) * max(0.0, 1 - (rec.get("cpu_eff") or 0) / 100)
            if wasted <= 0:
                continue
            cpu = f"{rec['cpu_eff']:.0f}" if rec.get("cpu_eff") is not None else "-"
            mem = f"{rec['mem_eff']:.0f}" if rec.get("mem_eff") is not None else "-"
            name = (names.get(rec["jobid"], "") or "")[:20]
            line(f"  {rec['jobid']:<12}{name:<22}{cpu:>6}{mem:>7}{wasted:>9.1f}h",
                 efficiency_verdict(rec)[1])
        sep()
        line("  Press ↻ Recalculate for the history summary.", C.FG_FAINT)

    def render_stats(self, stats: dict, error_patterns: list | None = None) -> None:
        log = self.query_one("#stats-content", RichLog)
        log.clear()

        def line(text: str = "", style: str = "white") -> None:
            log.write(Text(text, style=style))

        def sep(title: str = "") -> None:
            if title:
                bar = "─" * max(0, 62 - len(title))
                line(f"── {title} {bar}", "bold #29313c")
            else:
                line("─" * 66, C.FG_DIM)

        if not stats:
            line("  No history data yet.", C.FG_FAINT)
            return

        ts = datetime.now().strftime("%H:%M:%S  %d/%m/%Y")

        # ── GENERAL SUMMARY ──
        sep("GENERAL SUMMARY")
        line(f"  Total jobs in history : {stats['total']}", C.FG)
        line(f"  Completed successfully    : {stats['succeeded']}  ({stats['success_rt']}%)", f"bold {C.OK}")
        line(f"  Failed / Timeout        : {stats['failed']}    ({stats['fail_rt']}%)", f"bold {C.ERR}")
        line(f"  Others (running/pending) : {stats['other']}", C.FG_FAINT)

        # ── SUCCESS/FAILURE BAR ──
        sep()
        total  = stats["total"]
        ok_w   = int(40 * stats["succeeded"] / total) if total else 0
        fail_w = int(40 * stats["failed"]    / total) if total else 0
        rest_w = 40 - ok_w - fail_w
        ok_bar   = Text("█" * ok_w,   style=f"bold {C.OK}")
        fail_bar = Text("█" * fail_w, style=f"bold {C.ERR}")
        rest_bar = Text("░" * rest_w, style=C.FG_DIM)
        full_bar = Text("  [") + ok_bar + fail_bar + rest_bar + Text("]")
        full_bar += Text(f"  ✓ {stats['success_rt']}%  ✗ {stats['fail_rt']}%", style=C.FG)
        log.write(full_bar)

        # ── RESOURCES USED ──
        sep("RESOURCES USED (jobs COMPLETED)")
        line(f"  GPU-hours total       : {stats['gpu_hours']:.1f} h", "bold #dd8a4c")
        line(f"  CPU-hours total       : {stats['cpu_hours']:.1f} h", C.PRIMARY_SOFT)
        avg_h = int(stats["avg_wall_hrs"])
        avg_m = int((stats["avg_wall_hrs"] - avg_h) * 60)
        line(f"  Average time per job    : {avg_h}h {avg_m:02d}m", C.INFO)

        # ── BY PARTITION ──
        sep("JOBS BY PARTITION")
        by_part = sorted(stats["by_partition"].items(), key=lambda x: -x[1])
        max_count = max((v for _, v in by_part), default=1)
        for part, count in by_part:
            bar_w = int(20 * count / max_count)
            bar = "█" * bar_w + "░" * (20 - bar_w)
            pct  = round(count / total * 100, 1) if total else 0
            line(f"  {part:<18}  [{bar}]  {count:>4} jobs  ({pct}%)", C.PRIMARY_SOFT)

        # ── TOP JOB NAMES ──
        sep("TOP 10 JOB NAMES")
        by_name = sorted(stats["by_name"].items(), key=lambda x: -x[1])
        max_name = max((v for _, v in by_name), default=1) or 1
        for name, count in by_name[:10]:
            bar_w = int(20 * count / max_name)
            bar = "█" * bar_w + "░" * (20 - bar_w)
            line(f"  {name[:24]:<24}  [{bar}]  {count:>4}", C.FG)

        # ── TIMELINE LAST 30 DAYS ──
        sep("ACTIVITY — LAST 30 DAYS")
        jobs_by_day = stats.get("jobs_by_day", {})
        if jobs_by_day:
            max_day = max(jobs_by_day.values()) or 1
            # Show in groups of 6 days per line
            days = list(jobs_by_day.items())
            for i in range(0, len(days), 6):
                chunk = days[i:i+6]
                bars_txt = Text("  ")
                for day, cnt in chunk:
                    h = int(8 * cnt / max_day)
                    col = f"bold {C.OK}" if cnt > 0 else C.FG_DIM
                    short_day = day[5:]  # MM-DD
                    bars_txt += Text(f"{short_day} ", style=C.FG_FAINT)
                    bars_txt += Text("█" * h + "░" * (8 - h) + " ", style=col)
                log.write(bars_txt)
            line(f"  Max in one day: {max_day} jobs", C.FG_FAINT)

        # ── STATE BREAKDOWN ──
        sep("STATES")
        state_cols = {
            "COMPLETED": "bold green", "CD": "bold green",
            "FAILED": "bold red",      "F":  "bold red",
            "CANCELLED": "yellow",     "CA": "yellow",
            "TIMEOUT": "bold yellow",  "TO": "bold yellow",
            "OUT_OF_MEMORY": "bold magenta", "OOM": "bold magenta",
            "NODE_FAIL": "bold red",   "NF": "bold red",
            "RUNNING": "bold cyan",    "R":  "bold cyan",
            "PENDING": "white",        "PD": "white",
        }
        for state, count in sorted(stats["states"].items(), key=lambda x: -x[1]):
            col = state_cols.get(state, C.FG_FAINT)
            pct = round(count / total * 100, 1) if total else 0
            line(f"  {state:<22}  {count:>4} jobs  ({pct}%)", col)

        sep()
        line(f"  Updated: {ts}  │  {len(stats.get('jobs_by_day', {}))} days analysed", C.FG_FAINT)


class SlurmDashboard(App):

    CSS = """
    /* ─────────────────────────────────────────────────────────────
       Surfaces: canvas → content → chrome. Colour carries meaning
       (state, thresholds); chrome stays neutral so the data reads.
       ───────────────────────────────────────────────────────────── */
    Screen { background: $sq-bg; }
    * {
        scrollbar-background: $sq-surface;
        scrollbar-background-hover: $sq-surface;
        scrollbar-background-active: $sq-surface;
        scrollbar-color: $sq-line-strong;
        scrollbar-color-hover: $sq-fg-dim;
        scrollbar-color-active: $sq-primary;
        scrollbar-corner-color: $sq-surface;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    Header { background: $sq-panel; color: $sq-fg; text-style: bold; }
    Footer { background: $sq-panel; color: $sq-fg-muted; }
    Footer > .footer--key { color: $sq-primary; text-style: bold; }

    #main-layout { height: 1fr; }
    /* Content tables fill their panel instead of sizing to the row count. */
    #squeue-table, #mine-table, #sinfo-table, #history-table { height: 1fr; }

    /* ── buttons ───────────────────────────────────────────────────
       Quiet by default. Exactly one accented action per context, and
       destructive actions stay muted until hovered.
       ───────────────────────────────────────────────────────────── */
    Button {
        background: $sq-elevated; color: $sq-fg; border: none;
        min-width: 14; height: 3; margin-right: 1;
        content-align: center middle;
    }
    Button:hover { background: $sq-line-strong; color: $sq-fg; }
    Button:focus { background: $sq-line-strong; text-style: bold; }

    #btn-submit-run, #btn-jobs-new, #btn-prompt-ok, #btn-tpl-load {
        background: $sq-primary; color: $sq-bg; text-style: bold;
    }
    #btn-submit-run:hover, #btn-jobs-new:hover,
    #btn-prompt-ok:hover, #btn-tpl-load:hover {
        background: $sq-primary-hover; color: $sq-bg;
    }

    #btn-jobs-bulk, #btn-scancel, #btn-tpl-delete { color: $sq-err; }
    #btn-jobs-bulk:hover, #btn-scancel:hover, #btn-tpl-delete:hover {
        background: $sq-err; color: $sq-bg; text-style: bold;
    }
    #btn-yes, #btn-bulk-go {
        background: $sq-err; color: $sq-bg; text-style: bold;
    }
    #btn-yes:hover, #btn-bulk-go:hover { background: $sq-err-hover; }

    Button.active-log { background: $sq-primary; color: $sq-bg; text-style: bold; }

    /* ── tabs ───────────────────────────────────────────────────── */
    Tabs { background: $sq-panel; border-bottom: solid $sq-line; overflow-x: auto; }
    Tab { color: $sq-fg-faint; }
    Tab:hover { color: $sq-fg; }
    Tab.-active { color: $sq-primary; text-style: bold; }

    /* ── tables ─────────────────────────────────────────────────── */
    DataTable {
        background: $sq-surface; color: $sq-fg;
        border: solid $sq-line; scrollbar-size-vertical: 1;
    }
    DataTable:focus { border: solid $sq-primary; }
    DataTable > .datatable--header {
        background: $sq-panel; color: $sq-fg-muted; text-style: bold;
    }
    DataTable > .datatable--cursor { background: $sq-primary 30%; color: $sq-fg; }
    DataTable > .datatable--even-row { background: $sq-surface; }
    DataTable > .datatable--odd-row  { background: $sq-surface-alt; }

    /* ── inputs ─────────────────────────────────────────────────── */
    Input {
        background: $sq-surface; color: $sq-fg;
        border: solid $sq-line;
    }
    Input:focus { border: solid $sq-primary; }

    /* ── shared chrome: every panel toolbar looks the same ──────── */
    #jobs-toolbar, #history-toolbar, #resv-toolbar,
    #watch-toolbar, #stats-toolbar {
        height: 3; background: $sq-panel; border-bottom: solid $sq-line;
        align: left middle; padding: 0 2;
    }
    #jobs-toolbar-lbl, #history-filter-lbl, #resv-toolbar-lbl,
    #watch-toolbar-lbl, #stats-toolbar-label {
        color: $sq-fg; text-style: bold; margin-right: 2;
    }
    #history-action-row {
        height: 3; background: $sq-panel; border-top: solid $sq-line;
        align: left middle; padding: 0 2;
    }

    /* height is the outer box: the hairline needs its own row, otherwise
       the single line of content is squeezed to nothing. */
    StatsBar {
        height: 2; background: $sq-panel; color: $sq-fg;
        padding: 0 1; border-bottom: solid $sq-line; overflow: hidden;
    }

    /* ── panels ─────────────────────────────────────────────────── */
    #jobs-panel, #history-panel, #resv-panel, #watch-panel {
        height: 1fr; layout: vertical;
    }
    #jobs-info-panel { height: 1fr; padding: 0; }
    #jobs-info-log   { height: 1fr; border: none; background: $sq-surface; }
    #resv-table, #watch-table { height: 1fr; }

    EventLog {
        background: $sq-surface; border: solid $sq-line; height: 1fr;
    }
    #log-label {
        background: $sq-panel; color: $sq-fg; text-style: bold;
        padding: 0 1; border-bottom: solid $sq-line; height: 1;
    }

    /* ── secondary text ─────────────────────────────────────────── */
    #history-hint, #resv-legend { color: $sq-fg-faint; }
    #resv-legend { height: 1; padding: 0 2; }
    #history-selected, #resv-summary, #watch-summary {
        color: $sq-fg-muted; margin-left: 2;
    }
    #history-selected { width: 30; margin-right: 2; margin-left: 0; }
    #history-search {
        width: 1fr; max-width: 42; margin-right: 2; height: 1;
    }
    """

    BINDINGS = [
        ("q", "quit",           "Quit"),
        ("r", "manual_refresh", "Refresh"),
        ("1", "tab_all",        "All Jobs"),
        ("2", "tab_mine",       "My Jobs"),
        ("3", "tab_nodes",      "Nodes"),
        ("4", "tab_history",    "History"),
        ("d", "job_detail",     "Details"),
        ("l", "job_logs",       "Logs"),
        ("m", "job_monitor",    "Monitor"),
        ("x", "job_scancel",    "scancel"),
        ("h", "job_hold",       "Hold"),
        ("u", "job_release",    "Release"),
        ("b", "history_rerun",  "Rerun"),
        ("5", "tab_stats",      "Stats"),
        ("6", "tab_jobs",       "Jobs"),
        ("n", "new_job",        "New job"),
        ("a", "array_expand",   "Array"),
        ("e", "dep_tree",       "Deps"),
        ("f", "job_efficiency", "Efficiency"),
        ("w", "why_pending",    "Why pending"),
        ("k", "bulk_cancel",    "Bulk cancel"),
        ("7", "tab_resv",       "Reservations"),
        ("8", "tab_watch",      "Watchlist"),
        ("9", "tab_log",        "Event log"),
        ("p", "toggle_watch",   "Pin/Unpin"),
    ]

    TITLE = "SLURM Dashboard"
    SUB_TITLE = f"user: {MY_USER}  │  refresh: {REFRESH_INTERVAL}s"

    _active_tab:  str = "tab-all"

    def __init__(self) -> None:
        super().__init__()
        # Register before the stylesheets are parsed so every $sq-* variable
        # resolves, including those in modal DEFAULT_CSS blocks.
        self.register_theme(build_theme())
        self.theme = "sqdash"
        # Instance state — these were mutable class attributes, i.e. shared by
        # every instance of the app.
        self._prev_states: dict[str, str] = {}
        self._history: list[dict] = []
        self._jobs_by_id: dict[str, dict] = {}
        self._watchlist: list = []
        self._watch_states: dict = {}      # sacct-resolved states for pinned jobs
        self._reservations: list = []
        self._my_accounts: list = []
        self._reservations_loaded = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatsBar(id="stats-bar")
        yield Tabs(
            Tab("1  All Jobs",     id="tab-all"),
            Tab("2  My Jobs",      id="tab-mine"),
            Tab("3  Nodes",        id="tab-nodes"),
            Tab("4  History",      id="tab-history"),
            Tab("5  Stats",        id="tab-stats"),
            Tab("6  Jobs",         id="tab-jobs"),
            Tab("7  Reservations", id="tab-resv"),
            Tab("8  Watchlist",    id="tab-watch"),
            Tab("9  Event Log",    id="tab-log"),
        )
        with Container(id="main-layout"):
            yield SqueueTable(id="squeue-table")
            yield MyJobsTable(id="mine-table")
            yield SinfoTable(id="sinfo-table")
            with Vertical(id="log-panel"):
                yield Label(" Event log — job state changes", id="log-label")
                yield EventLog(id="event-log", max_lines=200)
            # ── History panel ──
            with Vertical(id="history-panel"):
                with Horizontal(id="history-toolbar"):
                    yield Label("Filter", id="history-filter-lbl")
                    yield Input(placeholder="job name / id / state / partition…",
                                id="history-search")
                    yield Label(
                        f"↑↓ navigate  ·  l: logs  ·  m: monitor  ·  b: resubmit  ·  {HISTORY_FILE}",
                        id="history-hint"
                    )
                yield HistoryTable(id="history-table")
                with Horizontal(id="history-action-row"):
                    yield Label("Selected: —", id="history-selected")
                    yield Button("Logs  l",       id="btn-hist-logs")
                    yield Button("Monitor  m",    id="btn-hist-monitor")
                    yield Button("Rerun  b",      id="btn-hist-rerun")
            # ── Stats panel ──
            with Vertical(id="stats-panel"):
                yield HistoryStatsPanel(id="stats-widget")
            # ── Jobs management panel ──
            with Vertical(id="jobs-panel"):
                with Horizontal(id="jobs-toolbar"):
                    yield Label("Job management", id="jobs-toolbar-lbl")
                    yield Button("New job  n",        id="btn-jobs-new")
                    yield Button("Array  a",          id="btn-jobs-array")
                    yield Button("Deps  e",           id="btn-jobs-deps")
                    yield Button("Efficiency  f",     id="btn-jobs-eff")
                    yield Button("Why pending  w",    id="btn-jobs-why")
                    yield Button("Bulk cancel  k",    id="btn-jobs-bulk")
                with Vertical(id="jobs-info-panel"):
                    yield RichLog(id="jobs-info-log", highlight=False,
                                  markup=False, wrap=True, max_lines=2000)
            # ── Reservations panel ──
            with Vertical(id="resv-panel"):
                with Horizontal(id="resv-toolbar"):
                    yield Label("Reservations", id="resv-toolbar-lbl")
                    yield Button("Refresh", id="btn-resv-refresh")
                    yield Label("", id="resv-summary")
                yield ReservationTable(id="resv-table")
                yield Label(
                    "  ✓ available to you    ▲ maintenance that blocks jobs"
                    "    · other users",
                    id="resv-legend")
            # ── Watchlist panel ──
            with Vertical(id="watch-panel"):
                with Horizontal(id="watch-toolbar"):
                    yield Label("Watchlist", id="watch-toolbar-lbl")
                    yield Button("Unpin  p", id="btn-watch-unpin")
                    yield Button("Clear finished", id="btn-watch-clear")
                    yield Label("", id="watch-summary")
                yield WatchlistTable(id="watch-table")
        yield ActionBar(id="action-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#mine-table").display    = False
        self.query_one("#sinfo-table").display   = False
        self.query_one("#log-panel").display     = False
        self.query_one("#history-panel").display = False
        self.query_one("#stats-panel").display   = False
        self.query_one("#jobs-panel").display    = False
        self.query_one("#resv-panel").display    = False
        self.query_one("#watch-panel").display   = False
        self.query_one(ActionBar).display        = True
        self._history = load_history()
        self._watchlist = load_watchlist()
        self._worker_fix_stale_history()   # audit stale RUNNING/PENDING on startup
        self.refresh_data()
        self.set_interval(REFRESH_INTERVAL, self.refresh_data)

    # ── tab switching ──
    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        tid = event.tab.id
        self._active_tab = tid
        self.query_one("#squeue-table").display  = (tid == "tab-all")
        self.query_one("#mine-table").display    = (tid == "tab-mine")
        self.query_one("#sinfo-table").display   = (tid == "tab-nodes")
        self.query_one("#log-panel").display     = (tid == "tab-log")
        self.query_one("#history-panel").display = (tid == "tab-history")
        self.query_one("#stats-panel").display   = (tid == "tab-stats")
        self.query_one("#jobs-panel").display    = (tid == "tab-jobs")
        self.query_one("#resv-panel").display    = (tid == "tab-resv")
        self.query_one("#watch-panel").display   = (tid == "tab-watch")
        self.query_one(ActionBar).display        = tid in ("tab-all", "tab-mine")
        self._sync_action_bar()
        if tid == "tab-history":
            self._refresh_history_table()
        if tid == "tab-stats":
            self.refresh_stats()
        if tid == "tab-jobs":
            self._render_jobs_panel()
        if tid == "tab-resv":
            self.refresh_reservations()
        if tid == "tab-watch":
            self._refresh_watchlist_table()
            self._resolve_watch_states()

    # ── history search ──
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "history-search":
            self._refresh_history_table(event.value)

    def on_data_table_row_highlighted(self, _) -> None:
        # Was on_data_table_cursor_moved(): DataTable has no CursorMoved
        # message, so the handler never ran and the selected-job labels only
        # caught up on the next 3 s poll.  RowHighlighted is the real event.
        self._sync_action_bar()
        if self._active_tab == "tab-history":
            jobid = self.query_one(HistoryTable).get_selected_jobid()
            lbl   = self.query_one("#history-selected", Label)
            lbl.update(f"Selected: [bold {C.WARN}]{jobid}[/]" if jobid else "Selected: —")

    def _refresh_history_table(self, filter_text: str = "") -> None:
        ft = self.query_one("#history-search", Input).value if not filter_text else filter_text
        self.query_one(HistoryTable).populate(self._history, ft)

    # ── action bar sync ──
    def _sync_action_bar(self) -> None:
        jobid = self._get_selected_jobid()
        self.query_one(ActionBar).set_selected(
            jobid, pinned=bool(jobid) and self._is_pinned(jobid))

    def _get_active_table(self):
        if self._active_tab == "tab-all":  return self.query_one(SqueueTable)
        if self._active_tab == "tab-mine": return self.query_one(MyJobsTable)
        return None

    def _get_selected_jobid(self) -> str | None:
        if self._active_tab == "tab-history":
            return self.query_one(HistoryTable).get_selected_jobid()
        if self._active_tab == "tab-watch":
            return self.query_one(WatchlistTable).get_selected_jobid()
        t = self._get_active_table()
        return t.get_selected_jobid() if t else None

    def _get_selected_user(self) -> str | None:
        """Owner of the selected job.

        Resolved from the last squeue snapshot first: the table only shows a
        USER column on wide terminals, and the cancel/hold/release guards must
        not silently fail (nor pass) because a column is hidden.
        """
        jobid = self._get_selected_jobid()
        if jobid:
            job = self._jobs_by_id.get(jobid)
            if job:
                return job.get("user")
        t = self._get_active_table()
        return t.get_selected_user() if t else None

    # ── button routing (unified) ──
    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        actions = {
            # ActionBar
            "btn-scancel":        self.action_job_scancel,
            "btn-hold":           self.action_job_hold,
            "btn-release":        self.action_job_release,
            "btn-detail":         self.action_job_detail,
            "btn-logs":           self.action_job_logs,
            "btn-monitor":        self.action_job_monitor,
            # History panel
            "btn-hist-logs":      self._open_history_logs,
            "btn-hist-monitor":   self._open_history_monitor,
            "btn-hist-rerun":     self.action_history_rerun,
            # Jobs panel
            "btn-jobs-new":       self.action_new_job,
            "btn-jobs-array":     self.action_array_expand,
            "btn-jobs-deps":      self.action_dep_tree,
            "btn-jobs-eff":       self.action_job_efficiency,
            "btn-jobs-why":       self.action_why_pending,
            "btn-jobs-bulk":      self.action_bulk_cancel,
            "btn-resv-refresh":   self.refresh_reservations,
            "btn-watch-unpin":    self.action_toggle_watch,
            "btn-watch-clear":    self.action_clear_finished_watch,
        }
        handler = actions.get(bid)
        if handler is not None:
            event.stop()
            handler()

    # ── history log opener ──
    def _open_history_logs(self) -> None:
        jobid = self.query_one(HistoryTable).get_selected_jobid()
        if not jobid:
            self.notify("Select a job from history first", severity="warning"); return
        entry = next((e for e in self._history if e.get("jobid") == jobid), None)
        if not entry:
            self.notify("Entry not found in history", severity="warning"); return
        stdout = entry.get("stdout", "")
        stderr = entry.get("stderr", "")
        if not stdout and not stderr:
            # Try fetching from scontrol (job might still be queryable)
            self.notify(f"No log paths stored — trying scontrol for job {jobid}…", timeout=3)
            self._fetch_and_open_logs(jobid, live=False)
            return
        state    = entry.get("state", "")
        live     = state in ("R", "RUNNING", "PD", "PENDING", "CG", "COMPLETING")
        jname    = entry.get("name", "")
        username = entry.get("user", "") or os.environ.get("USER", "")
        stdout   = resolve_existing_slurm_log(stdout, jobid, jname, username)
        stderr   = resolve_existing_slurm_log(stderr, jobid, jname, username)
        self.push_screen(LogViewerModal(
            jobid, stdout, stderr,
            job_name=jname,
            state=state,
            live=live,
        ))

    # ── live log opener (from queue tabs + history) ──
    def action_job_logs(self) -> None:
        if self._active_tab == "tab-history":
            self._open_history_logs(); return
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        self.notify(f"Loading log paths for job {jobid}…", timeout=2)
        self._fetch_and_open_logs(jobid, live=True)

    def _fetch_and_open_logs(self, jobid: str, live: bool = True) -> None:
        self._do_fetch_logs(jobid, live)

    @work(thread=True)
    def _do_fetch_logs(self, jobid: str, live: bool) -> None:
        paths = get_job_log_paths(jobid)
        # The history list is owned by the UI thread — mutating and saving it
        # from here raced with the 3 s refresh writing the same file.
        self.call_from_thread(self._store_log_paths, jobid, paths)
        self.call_from_thread(self._push_log_viewer_from_paths, jobid, paths, live)

    def _store_log_paths(self, jobid: str, paths: dict) -> None:
        entry = next((e for e in self._history if e.get("jobid") == jobid), None)
        if entry and (paths.get("stdout") or paths.get("stderr")):
            entry["stdout"] = paths.get("stdout", "")
            entry["stderr"] = paths.get("stderr", "")
            save_history(self._history)

    def _persist_history(self) -> None:
        save_history(self._history)

    def _push_log_viewer_from_paths(self, jobid: str, paths: dict, live: bool) -> None:
        if not paths["stdout"] and not paths["stderr"]:
            self.notify(
                f"Could not find log paths for job {jobid}",
                severity="warning", timeout=5
            )
            return

        entry    = next((e for e in self._history if e.get("jobid") == jobid), {})
        jname    = paths.get("name", "") or entry.get("name", "")
        username = entry.get("user", "") or os.environ.get("USER", "")

        stdout = resolve_existing_slurm_log(paths["stdout"], jobid, jname, username)
        stderr = resolve_existing_slurm_log(paths["stderr"], jobid, jname, username)

        self.push_screen(LogViewerModal(
            jobid,
            stdout_path=stdout,
            stderr_path=stderr,
            job_name=jname,
            state=paths.get("state", ""),
            live=live,
        ))

    # ── history rerun ──
    def action_history_rerun(self) -> None:
        if self._active_tab != "tab-history":
            self.notify("Switch to the History tab first", severity="warning")
            return
        jobid = self.query_one(HistoryTable).get_selected_jobid()
        if not jobid:
            self.notify("Select a job from History first", severity="warning")
            return
        entry = next((e for e in self._history if e.get("jobid") == jobid), None)
        name  = entry.get("name", "") if entry else ""
        sl    = entry.get("submit_line", "") if entry else ""
        if sl:
            hint = f"SubmitLine cached: {sl[:70]}{'…' if len(sl)>70 else ''}"
        else:
            hint = "No SubmitLine cached → will be searched in sacct/scontrol/requeue"
        self.push_screen(
            ConfirmModal("Confirm resubmission", f"Job {jobid}  [{name}]\n{hint}"),
            callback=lambda ok: self._do_history_rerun(ok, jobid),
        )

    def _do_history_rerun(self, confirmed: bool, jobid: str) -> None:
        if not confirmed:
            return
        self.notify(f"Resubmitting job {jobid}…", timeout=3)
        self._worker_rerun(jobid)

    @work(thread=True)
    def _worker_rerun(self, jobid: str) -> None:
        entry     = next((e for e in self._history if e.get("jobid") == jobid), None)
        cached_sl = entry.get("submit_line", "") if entry else ""
        ok, msg   = False, ""

        if not is_valid_jobid(jobid):
            self.app.call_from_thread(self._rerun_fail, jobid,
                                      f"Invalid job id: {jobid!r}")
            return

        # Strategy 1+2: try cached submit_line first.  build_sbatch_args()
        # rejects a line that is not an sbatch call or a script path instead
        # of prepending "sbatch" to whatever the history file happened to say.
        if cached_sl:
            args = build_sbatch_args(cached_sl)
            if args:
                ok, msg = run_sbatch(args)
            else:
                ok, msg = False, f"Cached submit line is not an sbatch command: {cached_sl!r}"

        # If no cache or it failed, query sacct + scontrol
        if not ok:
            fresh_sl = get_submit_line(jobid)
            if fresh_sl:
                args = build_sbatch_args(fresh_sl)
                if args:
                    ok, msg = run_sbatch(args)
                    # Cache the submit line for future reruns
                    if ok and entry:
                        entry["submit_line"] = fresh_sl
                        self.app.call_from_thread(self._persist_history)
                else:
                    ok, msg = False, f"Recovered submit line is not an sbatch command: {fresh_sl!r}"

        # Last fallback: scontrol requeue (job must still exist in Slurm)
        if not ok:
            # run() cannot raise: the bare subprocess.run() used here before
            # propagated TimeoutExpired/FileNotFoundError out of the worker
            # thread, leaving the rerun silently dead with no notification.
            _, requeue_err = run(["scontrol", "requeue", jobid], timeout=20)
            if not requeue_err.strip():
                ok  = True
                msg = f"Job {jobid} requeued → back to PENDING"
            else:
                # Build informative error message
                requeue_err = requeue_err.strip()
                script_info = get_job_script_info(jobid)
                hint = ""
                if script_info["command"] and os.path.isfile(script_info["command"]):
                    hint = f"\nScript found: sbatch {script_info['command']}"
                elif script_info["command"]:
                    hint = f"\nScript (not accessible): {script_info['command']}"
                msg = (
                    f"Could not resubmit automatically.\n"
                    f"requeue: {requeue_err or 'failed'}\n"
                    f"Tip: add the SubmitLine to the submit_line field in the history JSON,\n"
                    f"or submit manually: sbatch <your_script.sh>{hint}"
                )

        if ok:
            self.app.call_from_thread(self._rerun_ok, jobid, msg)
        else:
            self.app.call_from_thread(self._rerun_fail, jobid, msg)

    def _rerun_ok(self, jobid: str, msg: str) -> None:
        self.notify(msg or f"Job {jobid} resubmitted", severity="information", timeout=7)
        self.query_one(EventLog).log_event(f"rerun {jobid} → {msg}", f"bold {C.OK}")
        self.refresh_data()

    def _rerun_fail(self, jobid: str, err: str) -> None:
        self.notify(f"Error resubmitting {jobid}: {err}", severity="error", timeout=10)
        self.query_one(EventLog).log_event(f"rerun {jobid} FAIL: {err}", f"bold {C.ERR}")

    # ── job actions ──
    def action_job_detail(self) -> None:
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        self.push_screen(JobDetailModal(jobid))

    def _is_live_state(self, state: str) -> bool:
        return state in {"PD", "PENDING", "R", "RUNNING", "CG", "COMPLETING",
                         "S", "SUSPENDED", "PR", "PREEMPTED"}

    def _open_history_monitor(self) -> None:
        jobid = self.query_one(HistoryTable).get_selected_jobid()
        if not jobid:
            self.notify("Select a job from History first",
                        severity="warning", timeout=3); return
        entry = next((e for e in self._history if e.get("jobid") == jobid), None)
        state = entry.get("state", "") if entry else ""
        # Use the snapshot refreshed by the 3 s poller instead of running
        # squeue synchronously here, which blocked the UI thread.
        live = self._jobs_by_id.get(jobid)
        if live:
            state = live["state"]
        if not self._is_live_state(state):
            self.notify(
                f"Job {jobid} is not active ({state or 'UNKNOWN'}). "
                "Only RUNNING/PENDING jobs can be monitored.",
                severity="warning", timeout=5)
            return
        name = live["name"] if live else (entry.get("name", "") if entry else "")
        self.push_screen(ResourceMonitorModal(jobid, job_name=name, state=state))

    def action_job_monitor(self) -> None:
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        # If on the History tab, delegate to its specific helper
        if self._active_tab == "tab-history":
            self._open_history_monitor(); return
        # Indices 2 and 4 were hardcoded here and pointed at other columns
        # once the table switched to its compact/minimal layout.
        job = self._jobs_by_id.get(jobid, {})
        t = self._get_active_table()
        job_name = job.get("name") or cell_by_col(t, "NAME") or ""
        state    = job.get("state") or cell_by_col(t, "STATE") or ""
        self.push_screen(ResourceMonitorModal(jobid, job_name=job_name, state=state))

    def action_job_scancel(self) -> None:
        jobid = self._get_selected_jobid()
        user  = self._get_selected_user()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        if user is None:
            self.notify("Cannot determine the owner of this job — refusing to cancel",
                        severity="error"); return
        if user != MY_USER:
            self.notify(f"Cannot cancel jobs belonging to another user ({user})",
                        severity="error"); return
        self.push_screen(
            ConfirmModal("Confirm cancel",
                         f"Cancel job  {jobid}?  This cannot be undone."),
            callback=lambda ok: self._do_scancel(ok, jobid),
        )

    def _do_scancel(self, confirmed: bool, jobid: str) -> None:
        if not confirmed: return
        if not is_valid_jobid(jobid):
            self.notify(f"Invalid job id: {jobid!r}", severity="error"); return
        _, stderr = run(["scancel", jobid])
        if stderr.strip():
            self.notify(f"scancel error: {stderr.strip()}", severity="error", timeout=6)
            self.query_one(EventLog).log_event(f"scancel {jobid} ERROR: {stderr.strip()}", f"bold {C.ERR}")
        else:
            self.notify(f"Job {jobid} cancelled", severity="information", timeout=4)
            self.query_one(EventLog).log_event(f"scancel {jobid} → OK  (by {MY_USER})", f"bold {C.WARN}")
        self.refresh_data()

    def action_job_hold(self) -> None:
        jobid = self._get_selected_jobid()
        user  = self._get_selected_user()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        if user is None:
            self.notify("Cannot determine the owner of this job — refusing to hold",
                        severity="error"); return
        if user != MY_USER:
            self.notify("Cannot hold jobs belonging to another user", severity="error"); return
        if not is_valid_jobid(jobid):
            self.notify(f"Invalid job id: {jobid!r}", severity="error"); return
        _, stderr = run(["scontrol", "hold", jobid])
        if stderr.strip():
            self.notify(f"hold error: {stderr.strip()}", severity="error", timeout=6)
        else:
            self.notify(f"Job {jobid} placed on hold", severity="information", timeout=3)
            self.query_one(EventLog).log_event(f"scontrol hold {jobid} → OK", C.WARN)
        self.refresh_data()

    def action_job_release(self) -> None:
        jobid = self._get_selected_jobid()
        user  = self._get_selected_user()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        if user is None:
            self.notify("Cannot determine the owner of this job — refusing to release",
                        severity="error"); return
        if user != MY_USER:
            self.notify("Cannot release jobs belonging to another user", severity="error"); return
        if not is_valid_jobid(jobid):
            self.notify(f"Invalid job id: {jobid!r}", severity="error"); return
        _, stderr = run(["scontrol", "release", jobid])
        if stderr.strip():
            self.notify(f"release error: {stderr.strip()}", severity="error", timeout=6)
        else:
            self.notify(f"Job {jobid} released", severity="information", timeout=3)
            self.query_one(EventLog).log_event(f"scontrol release {jobid} → OK", f"bold {C.OK}")
        self.refresh_data()

    # ── reservations ────────────────────────────────────────────────────
    def refresh_reservations(self) -> None:
        self._worker_reservations()

    @work(thread=True, exclusive=True, group="reservations")
    def _worker_reservations(self) -> None:
        reservations = parse_reservations()
        # Account membership rarely changes; look it up once per session.
        accounts = self._my_accounts or get_my_accounts()
        self.app.call_from_thread(self._apply_reservations, reservations, accounts)

    def _apply_reservations(self, reservations: list, accounts: list) -> None:
        self._my_accounts = accounts
        for res in reservations:
            res["_mine"] = reservation_is_mine(res, MY_USER, accounts)
            res["_blocks"] = reservation_blocks_jobs(res)
        # Ones you can use first, then blocking maintenance, then the rest.
        reservations.sort(key=lambda r: (not r["_mine"], not r["_blocks"], r["name"]))
        self._reservations = reservations
        self._reservations_loaded = True
        self._rerender_reservations()

    def _rerender_reservations(self) -> None:
        """Recompute the time labels from cached data.

        Called on every poll while the tab is open so the "starts in 3h"
        countdowns stay honest, without asking the controller again.
        """
        reservations = self._reservations
        now = datetime.now()
        for res in reservations:
            res["_status"] = reservation_status(res, now)
        try:
            self.query_one(ReservationTable).populate(reservations)
            mine = sum(1 for r in reservations if r["_mine"])
            blocking = sum(1 for r in reservations if r["_blocks"])
            summary = (f"{len(reservations)} total  ·  {mine} available to you"
                       f"  ·  {blocking} blocking maintenance")
            self.query_one("#resv-summary", Label).update(summary)
        except Exception:
            pass

    # ── watchlist ───────────────────────────────────────────────────────
    def _is_pinned(self, jobid: str) -> bool:
        return jobid in watchlist_ids(self._watchlist)

    def action_toggle_watch(self) -> None:
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning", timeout=3)
            return
        if not is_valid_jobid(jobid):
            self.notify(f"Invalid job id: {jobid!r}", severity="error")
            return
        job = self._jobs_by_id.get(jobid, {})
        entry = next((e for e in self._history if e.get("jobid") == jobid), {})
        name = job.get("name") or entry.get("name", "")
        self._watchlist, pinned = toggle_watch(self._watchlist, jobid, name)
        if not save_watchlist(self._watchlist):
            self.notify(f"Could not write {WATCHLIST_FILE}", severity="error")
        self.notify(f"Job {jobid} {'pinned to' if pinned else 'removed from'} "
                    f"the watchlist", timeout=3)
        self.query_one(EventLog).log_event(
            f"{'pin' if pinned else 'unpin'} {jobid}",
            "bold magenta" if pinned else "dim")
        self._sync_action_bar()
        self._refresh_watchlist_table()
        # Repaint the queue tables so the pin marker updates immediately.
        self._apply_pins_to_tables()
        if pinned:
            self._resolve_watch_states()

    def action_clear_finished_watch(self) -> None:
        """Drop pinned jobs that have reached a final state."""
        live = self._jobs_by_id
        keep, dropped = [], 0
        for entry in self._watchlist:
            jid = entry["jobid"]
            if jid in live:
                keep.append(entry)
                continue
            state = self._watch_state_for(jid)
            if state and state.upper() in TERMINAL_STATES:
                dropped += 1
            else:
                keep.append(entry)
        if not dropped:
            self.notify("No finished jobs pinned", severity="warning", timeout=3)
            return
        self._watchlist = keep
        save_watchlist(self._watchlist)
        self.notify(f"Removed {dropped} finished job(s) from the watchlist",
                    timeout=4)
        self._refresh_watchlist_table()
        self._apply_pins_to_tables()

    def _watch_state_for(self, jobid: str) -> str:
        job = self._jobs_by_id.get(jobid)
        if job:
            return job.get("state", "")
        entry = next((e for e in self._history if e.get("jobid") == jobid), None)
        if entry and entry.get("state"):
            return entry["state"]
        return self._watch_states.get(jobid, "")

    def _watchlist_rows(self) -> list:
        rows = []
        for entry in self._watchlist:
            jid = entry["jobid"]
            live = self._jobs_by_id.get(jid)
            hist = next((e for e in self._history if e.get("jobid") == jid), {})
            rows.append({
                "jobid":     jid,
                "name":      (live or {}).get("name") or entry.get("name")
                             or hist.get("name", ""),
                "state":     self._watch_state_for(jid),
                "time":      (live or {}).get("time", ""),
                "time_left": (live or {}).get("time_left", ""),
                "nodes":     (live or {}).get("nodes", ""),
                "reason":    (live or {}).get("reason", ""),
                "added":     entry.get("added", ""),
            })
        return rows

    def _refresh_watchlist_table(self) -> None:
        try:
            rows = self._watchlist_rows()
            self.query_one(WatchlistTable).populate(rows)
            running = sum(1 for r in rows if r["state"] in ("R", "RUNNING"))
            pending = sum(1 for r in rows if r["state"] in ("PD", "PENDING"))
            done = sum(1 for r in rows
                       if (r["state"] or "").upper() in TERMINAL_STATES)
            self.query_one("#watch-summary", Label).update(
                f"{len(rows)} pinned  ·  {running} running  ·  {pending} pending"
                f"  ·  {done} finished")
        except Exception:
            pass

    @work(thread=True, exclusive=True, group="watchstates")
    def _resolve_watch_states(self) -> None:
        """Ask sacct about pinned jobs we have no state for."""
        unknown = [e["jobid"] for e in self._watchlist
                   if not self._watch_state_for(e["jobid"])]
        if not unknown:
            return
        states = sacct_final_state(unknown)
        if states:
            self.app.call_from_thread(self._apply_watch_states, states)

    def _apply_watch_states(self, states: dict) -> None:
        self._watch_states.update(states)
        self._refresh_watchlist_table()

    def _apply_pins_to_tables(self) -> None:
        """Hand the pinned set to the queue tables and repaint them."""
        pinned = watchlist_ids(self._watchlist)
        for table_cls in (SqueueTable, MyJobsTable):
            try:
                table = self.query_one(table_cls)
                table.pinned = pinned
                table.refresh_jobs(table._last_jobs)
            except Exception:
                pass

    # ── data refresh ──
    @work(thread=True)
    def _worker_fix_stale_history(self) -> None:
        """
        On startup: find history entries stuck in a live state (RUNNING, PENDING,
        COMPLETING, etc.) and resolve their real final state via sacct.
        Runs entirely in background — does not block the UI.
        """
        live_states = {"RUNNING", "R", "PENDING", "PD", "COMPLETING", "CG",
                       "CONFIGURING", "CF", "RESIZING", "RS", "SUSPENDED", "S",
                       "PREEMPTED", "PR", "REQUEUED", "RQ", "UNKNOWN", ""}
        stale = [
            e["jobid"] for e in self._history
            if e.get("state", "").upper() in live_states
        ]
        if not stale:
            return
        # Chunk into batches of 50 to avoid overly long sacct command lines
        def chunks(lst, n):
            for i in range(0, len(lst), n):
                yield lst[i:i + n]
        resolved: dict[str, str] = {}
        for batch in chunks(stale, 50):
            resolved.update(sacct_final_state(batch))
        if not resolved:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates = []
        for jid, real_state in resolved.items():
            # Normalise "CANCELLED by 1234" → "CANCELLED" (sacct format)
            real_state = real_state.split()[0].upper()
            # Skip if sacct returned a live state too (job genuinely still running)
            if real_state in live_states:
                continue
            updates.append((jid, real_state, now))
        if updates:
            self.app.call_from_thread(self._apply_stale_fixes, updates)

    def _apply_stale_fixes(self, updates: list[tuple]) -> None:
        log     = self.query_one(EventLog)
        changed = False
        for jid, real_state, now in updates:
            entry = next((e for e in self._history if e.get("jobid") == jid), None)
            if entry and entry.get("state", "") != real_state:
                old = entry.get("state", "?")
                entry["state"]     = real_state
                entry["last_seen"] = now
                changed = True
                log.log_event(
                    f"[startup] Job {jid}: {old} → {real_state} (sacct audit)",
                    state_style(real_state)
                )
        if changed:
            save_history(self._history)
            self._refresh_history_table()

    # ── editor-and-exit ──────────────────────────────────────────
    _open_editor_after_exit: tuple | None = None

    def request_open_editor_and_exit(self, binary: str, path: str) -> None:
        """Close the TUI and open an editor as the replacement process."""
        self._open_editor_after_exit = (binary, path)
        self.exit()

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_data(self) -> None:
        # exclusive: squeue/sinfo can take seconds on a loaded controller,
        # and the 3 s timer would otherwise stack workers indefinitely.
        jobs  = parse_squeue()
        nodes = parse_sinfo()
        stats = compute_stats(jobs)
        ts    = datetime.now().strftime("%H:%M:%S")

        # Fetch estimated start times for PENDING jobs and merge into job dicts
        pending_ids = {j["jobid"] for j in jobs
                       if j["state"] in ("PD", "PENDING")}
        if pending_ids:
            estimates = get_start_estimates()
            for j in jobs:
                if j["jobid"] in estimates:
                    j["est_start"] = estimates[j["jobid"]]
        events = []
        cur_states = {j["jobid"]: j["state"] for j in jobs}
        for jid, st in cur_states.items():
            old = self._prev_states.get(jid)
            if old is not None and old != st:
                events.append((jid, old, st))
        _active = {"R", "RUNNING", "PD", "PENDING", "CG", "COMPLETING",
                   "S", "SUSPENDED", "PR", "PREEMPTED"}
        for jid, old_st in self._prev_states.items():
            if jid not in cur_states and old_st in _active:
                events.append((jid, old_st, "GONE"))
        self._prev_states = cur_states
        self.call_from_thread(self._apply_update, jobs, nodes, stats, ts, events)

    def _apply_update(self, jobs, nodes, stats, ts, events) -> None:
        self._jobs_by_id = {j["jobid"]: j for j in jobs}
        pins = watchlist_ids(self._watchlist)
        self.query_one(SqueueTable).pinned = pins
        self.query_one(MyJobsTable).pinned = pins
        self.query_one(StatsBar).update_stats(stats, ts)
        self.query_one(SqueueTable).refresh_jobs(jobs)
        self.query_one(MyJobsTable).refresh_jobs(jobs)
        self.query_one(SinfoTable).refresh_nodes(nodes)
        self._sync_action_bar()

        log = self.query_one(EventLog)
        gone_jids = set(jid for jid, _, new in events if new == "GONE")

        # Update history only with jobs still visible in squeue.
        # Jobs in gone_jids get their final state from sacct — skip them
        # here to avoid overwriting a final state with stale squeue data.
        #
        # HISTORY_ONLY_MINE: the history file is capped at MAX_HISTORY entries
        # and the History tab is documented as "your past jobs".  Recording
        # every job on the cluster evicted the user's own jobs within minutes
        # on a busy system (and wrote other users' job names to disk).
        before = len(self._history)
        touched = False
        for j in jobs:
            if j["jobid"] in gone_jids:
                continue
            if HISTORY_ONLY_MINE and j.get("user") != MY_USER:
                continue
            prev = next((e for e in self._history
                         if e.get("jobid") == j["jobid"]), None)
            if prev is None or prev.get("state") != j.get("state"):
                touched = True
            self._history = upsert_history(self._history, j)
        # Writing the full JSON every 3 s hammered $HOME (often NFS on a
        # cluster); persist only when something actually changed.
        if touched or len(self._history) != before:
            save_history(self._history)

        # Only refresh history table if there are no pending sacct lookups.
        # If there are gone jobs, _apply_resolve_gone will do the refresh
        # once sacct returns the real final states.
        tracked_gone = [jid for jid in gone_jids
                        if any(e.get("jobid") == jid for e in self._history)]
        if self._active_tab == "tab-watch":
            self._refresh_watchlist_table()
        if self._active_tab == "tab-resv" and self._reservations_loaded:
            self._rerender_reservations()
        if self._active_tab == "tab-history" and not tracked_gone:
            self._refresh_history_table()

        if tracked_gone:
            self._worker_resolve_gone(tracked_gone)
        for jid, old, new in events:
            if new != "GONE":
                log.log_event(f"Job {jid}: {old} → {new}", state_style(new))
        if events:
            self.notify(f"{len(events)} job state change(s) detected", timeout=3)

    @work(thread=True)
    def _worker_resolve_gone(self, gone_jids: list[str]) -> None:
        """
        For each jobid that disappeared from squeue, queries sacct for
        get its final real state (COMPLETED, FAILED, TIMEOUT, etc.)
        and updates the history + event log.
        """
        final_states = sacct_final_state(gone_jids)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates = []
        for jid in gone_jids:
            real_state = final_states.get(jid, "COMPLETED")
            # Normalise sacct aliases
            aliases = {
                "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED",
                "FAILED": "FAILED", "TIMEOUT": "TIMEOUT",
                "OUT_OF_MEMORY": "OUT_OF_MEMORY", "NODE_FAIL": "NODE_FAIL",
                "COMPLETED": "COMPLETED", "PREEMPTED": "PREEMPTED",
            }
            # sacct sometimes returns "CANCELLED by 1234" → take first word
            real_state = real_state.split()[0].upper()
            real_state = aliases.get(real_state, real_state)
            updates.append((jid, real_state, now))
        self.app.call_from_thread(self._apply_resolve_gone, updates)

    def _apply_resolve_gone(self, updates: list[tuple]) -> None:
        log = self.query_one(EventLog)
        changed = False
        finished: list[tuple[str, str, str]] = []
        for jid, real_state, now in updates:
            entry = next((e for e in self._history if e.get("jobid") == jid), None)
            if entry:
                old_state = entry.get("state", "?")
                entry["state"]     = real_state
                entry["last_seen"] = now
                changed = True
                if entry.get("user", MY_USER) == MY_USER:
                    finished.append((jid, real_state, entry.get("name", "")))
            else:
                old_state = "?"
            col = state_style(real_state)
            log.log_event(f"Job {jid}: {old_state} → {real_state} (sacct)", col)
        if finished:
            self._announce_finished(finished)
        if changed:
            save_history(self._history)
            # Always refresh so data is correct whenever user switches to History
            self._refresh_history_table()

    # ── completion notifications ───────────────────────────────────────
    def _announce_finished(self, finished: list[tuple[str, str, str]]) -> None:
        """Toast, bell and user hook when one of your jobs reaches its end."""
        cfg = CONFIG["notifications"]
        if cfg["notify_on_finish"]:
            for jid, state, name in finished[:5]:
                severity = ("information" if state == "COMPLETED"
                            else "warning" if state in ("CANCELLED", "PREEMPTED")
                            else "error")
                label = f" ({name})" if name else ""
                self.notify(f"Job {jid}{label} finished: {state}",
                            severity=severity, timeout=10)
            if len(finished) > 5:
                self.notify(f"…and {len(finished) - 5} more jobs finished", timeout=6)
        if cfg["bell_on_finish"]:
            try:
                self.bell()
            except Exception:
                pass
        if cfg["hook"]:
            self._worker_run_hooks(finished)

    @work(thread=True)
    def _worker_run_hooks(self, finished: list[tuple[str, str, str]]) -> None:
        hook = CONFIG["notifications"]["hook"]
        for jid, state, name in finished:
            run_completion_hook(hook, jid, state, name)

    def refresh_stats(self) -> None:
        self._worker_stats()

    def refresh_efficiency(self) -> None:
        self.notify("Querying sacct for efficiency data…", timeout=4)
        self._worker_efficiency()

    @work(thread=True, exclusive=True, group="efficiency")
    def _worker_efficiency(self) -> None:
        done = [e for e in self._history
                if (e.get("state") or "").upper() in TERMINAL_STATES]
        recent = done[-150:]
        names = {e["jobid"]: e.get("name", "") for e in recent}
        data = get_job_efficiency([e["jobid"] for e in recent])
        self.app.call_from_thread(self._apply_efficiency, data, names)

    def _apply_efficiency(self, data: dict, names: dict) -> None:
        try:
            self.query_one(HistoryStatsPanel).render_efficiency(data, names)
        except Exception:
            pass

    @work(thread=True)
    def _worker_stats(self) -> None:
        stats = compute_history_stats(self._history)
        self.app.call_from_thread(self._apply_stats, stats, None)

    def _apply_stats(self, stats: dict, errors: list | None) -> None:
        try:
            panel = self.query_one(HistoryStatsPanel)
            panel.render_stats(stats, errors)
        except Exception:
            pass

    def action_tab_jobs(self)  -> None: self.query_one(Tabs).active = "tab-jobs"
    def action_tab_stats(self) -> None: self.query_one(Tabs).active = "tab-stats"
    def action_tab_log(self)   -> None: self.query_one(Tabs).active = "tab-log"
    def action_tab_resv(self)  -> None: self.query_one(Tabs).active = "tab-resv"
    def action_tab_watch(self) -> None: self.query_one(Tabs).active = "tab-watch"

    def action_new_job(self) -> None:
        self.push_screen(SubmitJobModal(), self._on_job_submitted)

    def _on_job_submitted(self, result) -> None:
        if result:
            self.refresh_data()

    def action_array_expand(self) -> None:
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning", timeout=3)
            return
        self.push_screen(ArrayJobModal(jobid), lambda _: None)

    def action_dep_tree(self) -> None:
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning", timeout=3)
            return
        self.push_screen(DependencyTreeModal(jobid), lambda _: None)

    # ── efficiency ─────────────────────────────────────────────────────
    def action_job_efficiency(self) -> None:
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning", timeout=3)
            return
        job = self._jobs_by_id.get(jobid, {})
        entry = next((e for e in self._history if e.get("jobid") == jobid), {})
        name = job.get("name") or entry.get("name", "")
        self.push_screen(EfficiencyModal(jobid, job_name=name), lambda _: None)

    # ── why is this job pending ────────────────────────────────────────
    def action_why_pending(self) -> None:
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning", timeout=3)
            return
        job = self._jobs_by_id.get(jobid)
        if not job:
            self.notify(f"Job {jobid} is not in the queue any more — "
                        "press F for its efficiency report instead",
                        severity="warning", timeout=5)
            return
        state = job.get("state", "")
        if state not in ("PD", "PENDING"):
            self.notify(f"Job {jobid} is {state}, not pending", severity="warning",
                        timeout=4)
            return
        self.push_screen(
            PriorityModal(jobid, reason=job.get("reason", ""),
                          partition=job.get("partition", ""), state=state),
            lambda _: None)

    # ── bulk cancel ────────────────────────────────────────────────────
    def action_bulk_cancel(self) -> None:
        self.push_screen(
            TextPromptModal(
                "Bulk cancel — which of your jobs?",
                "pending | running | all | text matching the job name",
                "pending"),
            callback=self._bulk_collect)

    def _bulk_collect(self, selector: str | None) -> None:
        if not selector:
            return
        sel = selector.strip().lower()
        mine = [j for j in self._jobs_by_id.values() if j.get("user") == MY_USER]
        if sel in ("pending", "pd"):
            jobs, desc = [j for j in mine if j["state"] in ("PD", "PENDING")], "your pending jobs"
        elif sel in ("running", "r"):
            jobs, desc = [j for j in mine if j["state"] in ("R", "RUNNING")], "your running jobs"
        elif sel == "all":
            jobs, desc = mine, "all your jobs"
        else:
            jobs = [j for j in mine if sel in j.get("name", "").lower()]
            desc = f"your jobs matching '{selector.strip()}'"
        jobs = [j for j in jobs if is_valid_jobid(j.get("jobid", ""))]
        if not jobs:
            self.notify(f"No jobs matched {desc}", severity="warning", timeout=4)
            return
        self.push_screen(BulkCancelModal(jobs, desc), callback=self._bulk_cancel_confirmed)

    def _bulk_cancel_confirmed(self, jobids: list | None) -> None:
        if not jobids:
            return
        self.notify(f"Cancelling {len(jobids)} job(s)…", timeout=3)
        self._worker_bulk_cancel(list(jobids))

    @work(thread=True)
    def _worker_bulk_cancel(self, jobids: list[str]) -> None:
        ids = [j for j in jobids if is_valid_jobid(j)]
        errors: list[str] = []
        done = 0
        # scancel takes several ids at once; chunked so the argv stays sane.
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            _, err = run(["scancel"] + batch, timeout=30)
            if err.strip():
                errors.append(err.strip())
            else:
                done += len(batch)
        self.app.call_from_thread(self._bulk_cancel_done, done, len(ids), errors)

    def _bulk_cancel_done(self, done: int, total: int, errors: list[str]) -> None:
        log = self.query_one(EventLog)
        if errors:
            self.notify(f"Cancelled {done}/{total}; errors: {errors[0][:120]}",
                        severity="error", timeout=8)
            log.log_event(f"bulk scancel {done}/{total} — {errors[0][:80]}", f"bold {C.ERR}")
        else:
            self.notify(f"Cancelled {done} job(s)", severity="information", timeout=5)
            log.log_event(f"bulk scancel {done} job(s) by {MY_USER}", f"bold {C.WARN}")
        self.refresh_data()

    def _render_jobs_panel(self) -> None:
        log = self.query_one("#jobs-info-log", RichLog)
        log.clear()
        log.write(Text("── Keyboard shortcuts "
                       + "─" * 45, style=f"bold {C.FG_MUTED}"))
        shortcuts = [
            ("N", "New job — sbatch form, with save/load of templates"),
            ("A", "Array expand — per-task states, and rerun only the failed ones"),
            ("E", "Dependencies — dependency tree for the selected job"),
            ("F", "Efficiency — what the job reserved versus what it used"),
            ("W", "Why pending — blocking reason, queue position, priority"),
            ("K", "Bulk cancel — cancel many of your jobs behind a typed confirm"),
            ("P", "Pin/unpin — keep a job on the Watchlist tab across restarts"),
            ("7", "Reservations — who has the cluster booked, and what you may use"),
            ("8", "Watchlist — the jobs you pinned, with live state"),
            ("/", "Inside the log viewer: filter lines (text or /regex/)"),
        ]
        for key, desc in shortcuts:
            line = Text(f"  {key:>2}   ", style=f"bold {C.PRIMARY}")
            line.append(desc, style=C.FG)
            log.write(line)
        log.write(Text(""))
        log.write(Text("── Workflow "
                       + "─" * 55, style=f"bold {C.FG_MUTED}"))
        tips = [
            "1. Press N  →  fill the sbatch form  →  save it as a template for next time",
            "2. Select an array job  →  A  →  rerun just the failed tasks",
            "3. A job stuck in PENDING?  →  W tells you what is blocking it",
            "4. After a job finishes  →  F shows whether the request was oversized",
            "5. Stats tab  →  Efficiency report aggregates that across your history",
            "6. Pin the jobs you care about with P; they stay on tab 8 after a restart",
            "7. Job will not start?  →  tab 7 shows reservations holding the nodes",
        ]
        for tip in tips:
            log.write(Text(f"  {tip}", style=C.FG_MUTED))
        log.write(Text(""))
        log.write(Text("── Configuration " + "─" * 50, style=f"bold {C.FG_MUTED}"))
        log.write(Text(f"  {CONFIG_FILE}", style=C.FG_MUTED))
        log.write(Text(f"  templates: {TEMPLATE_FILE}", style=C.FG_MUTED))
        log.write(Text(f"  watchlist: {WATCHLIST_FILE}", style=C.FG_MUTED))
        log.write(Text(f"  ssh to compute nodes: "
                       f"{'enabled' if CONFIG['monitor']['use_ssh'] else 'disabled'}",
                       style=C.FG_MUTED))


    def action_manual_refresh(self) -> None:

        self.refresh_data()
        self.notify("Manual refresh triggered", timeout=2)

    def action_tab_all(self)     -> None: self.query_one(Tabs).active = "tab-all"
    def action_tab_mine(self)    -> None: self.query_one(Tabs).active = "tab-mine"
    def action_tab_nodes(self)   -> None: self.query_one(Tabs).active = "tab-nodes"
    def action_tab_history(self) -> None: self.query_one(Tabs).active = "tab-history"


def _exec_editor(binary: str, path: str) -> None:
    """
    Replace the current process with a shell one-liner that:
      1. Opens the editor
      2. Re-launches the dashboard when the editor exits
    No terminal ownership conflict — the TUI is already gone.
    """
    dashboard_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"
    shell_cmd = (
        f"stty sane 2>/dev/null; "
        f"{shlex.quote(binary)} {shlex.quote(path)}; "
        f"stty sane 2>/dev/null; "
        f"exec {dashboard_cmd}"
    )
    try:
        os.execvpe("bash", ["bash", "-lc", shell_cmd], os.environ)
    except Exception:
        # Fallback: just open editor, no relaunch
        os.execvp(binary, [binary, path])


def _fix_stdin_blocking() -> None:
    """
    On Python 3.9 + Textual, if the environment (HPC modules, nvcc, pipes)
    leaves stdin in O_NONBLOCK, linux_driver raises BlockingIOError [Errno 11].
    Force stdin back to blocking mode before starting the TUI.
    """
    import sys, fcntl
    try:
        fd    = sys.stdin.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if flags & os.O_NONBLOCK:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    except Exception:
        pass  # stdin is not a real fd (e.g. redirected) — ignore


USAGE = """\
slurm_dashboard — terminal dashboard for Slurm

  sqdash                 launch the dashboard
  sqdash --write-config  create the config file with documented defaults
  sqdash --show-config   print the effective configuration and exit
  sqdash --help          this message

Config: {config}
Templates: {templates}
"""


def _main() -> None:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(USAGE.format(config=CONFIG_FILE, templates=TEMPLATE_FILE))
        return
    if "--write-config" in args:
        path = write_default_config()
        print(f"Config written to {path}")
        return
    if "--show-config" in args:
        print(f"# effective configuration (from {CONFIG_FILE})")
        for section, values in CONFIG.items():
            print(f"\n[{section}]")
            for key, value in values.items():
                print(f"{key} = {value}")
        return
    if args:
        print(f"Unknown option: {args[0]}\n")
        print(USAGE.format(config=CONFIG_FILE, templates=TEMPLATE_FILE))
        sys.exit(2)

    if not shutil.which("squeue"):
        print("Error: Slurm is not available in your $PATH.")
        print("Make sure to run 'module load slurm' (or equivalent) before launching the dashboard.")
        sys.exit(1)
    # Make the config discoverable on first run instead of documenting a
    # file that does not exist yet.
    try:
        write_default_config()
    except Exception:
        pass
    _fix_stdin_blocking()
    app = SlurmDashboard()
    app.run()
    # If the user asked to open an editor, replace this process with it
    req = getattr(app, "_open_editor_after_exit", None)
    if req:
        binary, path = req
        _exec_editor(binary, path)


if __name__ == "__main__":
    _main()
