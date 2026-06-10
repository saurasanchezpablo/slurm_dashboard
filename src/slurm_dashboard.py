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
from datetime import datetime
from collections import defaultdict
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import (
    Header, Footer, DataTable, Static, Label,
    RichLog, Tabs, Tab, Button, TextArea, Input
)
from textual.containers import Vertical, Horizontal, Container
from textual.screen import ModalScreen
from textual import work
from textual.timer import Timer
from rich.text import Text

# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────
REFRESH_INTERVAL  = 3    # squeue table (ligero, ~10ms)
LOG_TAIL_LINES    = 200
LOG_REFRESH_SECS  = 2    # log viewer auto-refresh
MY_USER           = os.environ.get("USER", "")
HISTORY_FILE      = Path.home() / ".slurm_dashboard_history.json"
EVENT_LOG_FILE    = Path.home() / ".slurm_dashboard_events.log"
MAX_EVENT_LOG     = 5000   # max lines kept in the event log file
MAX_HISTORY       = 500   # max entries to keep

# ──────────────────────────────────────────────
#  JOB HISTORY
# ──────────────────────────────────────────────
def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []

def save_history(history: list[dict]) -> None:
    try:
        HISTORY_FILE.write_text(json.dumps(history[-MAX_HISTORY:], indent=2))
    except Exception:
        pass


def append_event_log(ts: str, msg: str) -> None:
    """Append a single event line to the persistent log file."""
    try:
        with open(EVENT_LOG_FILE, "a", encoding="utf-8") as f:
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
    entry = next((e for e in history if e["jobid"] == job["jobid"]), None)
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if entry is None:
        entry = {
            "jobid":     job["jobid"],
            "name":      job["name"],
            "user":      job["user"],
            "partition": job["partition"],
            "cpus":      job["cpus"],
            "mem":       job["mem"],
            "gpus":      job["gpus"],
            "state":     job["state"],
            "first_seen": now,
            "last_seen":  now,
            "stdout":    "",
            "stderr":    "",
        }
        history.append(entry)
    else:
        entry["state"]     = job["state"]
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
    return {
        "R": "bold green",   "RUNNING": "bold green",
        "PD": "bold cyan",   "PENDING": "bold cyan",
        "CG": "green",       "COMPLETING": "green",
        "CD": "dim",         "COMPLETED": "dim",
        "F":  "bold red",    "FAILED": "bold red",
        "CA": "red",         "CANCELLED": "red",
        "TO": "yellow",      "TIMEOUT": "yellow",
        "OOM": "bold red",
    }.get(state.upper().strip(), "white")

def sacct_final_state(jobids: list[str]) -> dict[str, str]:
    """
    Query sacct for the best final state of each jobid.
    Handles suffixes like 123.batch, 123.extern, array jobs 123_4.
    Prioritises terminal states over live states when multiple rows exist.
    """
    if not jobids:
        return {}
    ids_arg = ",".join(jobids)
    try:
        out = run_out([
            "sacct", "-j", ids_arg, "-n", "-P",
            "--format=JobID,State"
        ])
    except Exception:
        return {}

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

    fmt = "%i|%P|%j|%u|%T|%M|%L|%C|%m|%b|%R|%N"
    out = run_out(["squeue", f"--format={fmt}", "--noheader"])
    jobs = []
    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 12:
            continue
        raw_gpu = parts[9]
        gpu_val = ""
        m = re.search(r"\d+", raw_gpu)
        if m and "gpu" in raw_gpu.lower():
            gpu_val = m.group()
        jobs.append({
            "jobid":     parts[0],
            "partition": parts[1],
            "name":      parts[2][:20],
            "user":      parts[3],
            "state":     parts[4],
            "time":      parts[5],
            "time_left": parts[6],
            "cpus":      parts[7],
            "mem":       parts[8],
            "gpus":      gpu_val,
            "reason":    parts[10] if parts[10] != "None" else "",
            "nodes":     parts[11],
        })
    return jobs

def parse_sinfo() -> list[dict]:
    fmt = "%N|%P|%t|%C|%m|%G|%f"
    out = run_out(["sinfo", f"--format={fmt}", "--noheader"])
    nodes = []
    for line in out.strip().splitlines():
        parts = line.split("|")
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
    out = run_out(["scontrol", "show", "job", jobid])
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
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"(Error reading file: {e})"


# ──────────────────────────────────────────────
#  RESOURCE MONITORING
# ──────────────────────────────────────────────
def get_job_nodes(jobid: str) -> list[str]:
    """Devuelve lista de nodes asignados a un job."""
    out = run_out(["squeue", "-j", jobid, "-h", "--format=%N"])
    nodelist = out.strip()
    if not nodelist or nodelist in ("(None)", "N/A", ""):
        return []
    try:
        exp = run_out(["scontrol", "show", "hostnames", nodelist])
        return [n.strip() for n in exp.strip().splitlines() if n.strip()]
    except Exception:
        return [nodelist]

def ssh_cmd(node: str, cmd: str, timeout: int = 8) -> str:
    """Execute a command on a remote node via SSH (no password)."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=5",
             "-o", "BatchMode=yes",
             node, cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout
        )
        return r.stdout
    except Exception:
        return ""

def get_node_gpu_info(node: str) -> list[dict]:
    """
    Intenta obtener info de GPU via nvidia-smi.
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
            kv = dict(item.split("=") for item in line.split() if "=" in item)
            mt = int(kv.get("mem_total", 0))
            ma = int(kv.get("mem_avail", 0))
            result["mem_total_kb"] = mt
            result["mem_used_kb"]  = mt - ma
            result["mem_pct"] = int((mt - ma) / mt * 100) if mt > 0 else 0
        if line.startswith("load="):
            result["load"] = line[5:].strip().split(",")[0].strip()
    # CPU% via mpstat si available, sino top -bn1
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
    out = run_out([
        "sstat", "-j", jobid, "--noheader",
        "--format=AveCPU,MaxRSS,MaxVMSize,NTasks"
    ])
    result = {"avg_cpu": "N/A", "max_rss": "N/A", "tasks": "N/A"}
    line = out.strip().splitlines()[0] if out.strip() else ""
    if line:
        parts = line.split()
        if len(parts) >= 3:
            result["avg_cpu"] = parts[0]
            result["max_rss"] = parts[1]
            result["tasks"]   = parts[3] if len(parts) > 3 else "N/A"
    return result

def parse_time_to_secs(t: str) -> int:
    """Convierte time SLURM (DD-HH:MM:SS o HH:MM:SS o MM:SS) a segundos."""
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
    """Genera barra de progreso ASCII."""
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return fill * filled + empty * (width - filled)

def bar_color(pct: int) -> str:
    if pct >= 90: return "bold red"
    if pct >= 70: return "yellow"
    return "bold green"

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
    # Fuente 1: sacct SubmitLine
    try:
        out = run_out(["sacct", "-j", jobid, "-X", "-n", "-P", "--format=SubmitLine"])
        for line in out.splitlines():
            line = line.strip()
            if line and line not in ("SubmitLine", "Unknown", "N/A", ""):
                return line
    except Exception:
        pass

    # Fuente 2: scontrol show job -> campo Command=
    try:
        out = run_out(["scontrol", "show", "job", jobid])
        m = re.search(r"Command=(\S+)", out)
        if m:
            script = m.group(1)
            if script and script != "(null)" and os.path.isfile(script):
                return f"sbatch {script}"
    except Exception:
        pass

    return ""

def get_job_script_info(jobid: str) -> dict:
    """
    Extrae Command, WorkDir y ExtraArgs de scontrol show job
    to allow building an sbatch command manually.
    """
    info = {"command": "", "workdir": "", "extra": ""}
    try:
        out = run_out(["scontrol", "show", "job", jobid])
        m_cmd  = re.search(r"Command=(\S+)", out)
        m_work = re.search(r"WorkDir=(\S+)", out)
        if m_cmd:  info["command"] = m_cmd.group(1)
        if m_work: info["workdir"] = m_work.group(1)
    except Exception:
        pass
    return info

def run_sbatch(args: list[str]) -> tuple[bool, str]:
    """Launch sbatch with the given args. Returns (ok, message)."""
    try:
        proc = subprocess.run(
            args, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=30
        )
        if proc.returncode == 0:
            return True, proc.stdout.strip() or "Job resubmitted"
        return False, proc.stderr.strip() or "sbatch error desconocido"
    except Exception as e:
        return False, str(e)

def rerun_history_job(jobid: str) -> tuple[bool, str]:
    """
    Multi-source strategy to resubmit a job:
      1. sacct SubmitLine -> sbatch <original line>
      2. scontrol Command= -> sbatch <script>
      3. scontrol requeue  -> back to PENDING (only jobs still in Slurm)
    """
    # Estrategia 1 + 2: get_submit_line ya combina ambas
    submit_line = get_submit_line(jobid)
    if submit_line:
        try:
            args = shlex.split(submit_line)
            if args and args[0] == "sbatch":
                return run_sbatch(args)
            # If it is just the script path, wrap it in sbatch
            if args and os.path.isfile(args[0]):
                return run_sbatch(["sbatch"] + args)
        except Exception as e:
            return False, str(e)

    # Estrategia 3: scontrol requeue
    proc = subprocess.run(
        ["scontrol", "requeue", jobid],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=20,
    )
    if proc.returncode == 0:
        return True, f"Job {jobid} requeued → vuelve a PENDING"

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
    import math

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

    # ── GPU-horas aproximadas (solo jobs COMPLETED) ──
    # Usamos last_seen - first_seen as a proxy for actual execution time
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
        return parts if parts else DEFAULT_PARTITIONS
    except Exception:
        return DEFAULT_PARTITIONS

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

    Reglas:
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
    static_parent = parent
    for part in Path(parent).parts:
        if _SLURM_TOKENS.search(part):
            break
        static_parent = str(Path(static_parent))

    # Walk up until we reach a token-free directory
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
        return False, "No se especifico un script"

    # Resolver y sanear paths de logs ANTES de llamar sbatch
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
    try:
        out = run_out([
            "sacct", "-j", jobid_base, "-X", "-n", "-P",
            "--format=JobID,JobName,State,ExitCode,Elapsed,NodeList,Start,End"
        ])
    except Exception:
        return []
    tasks = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 8:
            continue
        jid = parts[0].strip()
        if not jid or jid == jobid_base:
            continue
        tasks.append({
            "jobid": jid, "name": parts[1].strip(),
            "state": parts[2].strip().split()[0], "exitcode": parts[3].strip(),
            "elapsed": parts[4].strip(), "nodes": parts[5].strip(),
            "start": parts[6].strip(), "end": parts[7].strip(),
        })
    return tasks

def get_dependency_tree(jobid: str, depth: int = 0, visited: set = None) -> list:
    if visited is None:
        visited = set()
    if jobid in visited or depth > 5:
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
            m = re.match(r"(afterok|afterany|afternotok|after):(\d+)", dep.strip())
            if m:
                result += get_dependency_tree(m.group(2), depth + 1, visited)
    return result

# ──────────────────────────────────────────────
#  MODAL: CONFIRM
# ──────────────────────────────────────────────
class SubmitJobModal(ModalScreen):
    """TUI form to build and submit an sbatch job. Supports saving/loading templates."""
    DEFAULT_CSS = """
    SubmitJobModal { align: center middle; }
    #submit-dialog {
        width: 80; background: #161b22; border: solid #30363d;
        padding: 1 2; layout: vertical;
    }
    #submit-title { color: #58a6ff; text-style: bold; margin-bottom: 1; }
    .field-row { height: 3; layout: horizontal; margin-bottom: 0; }
    .field-lbl { width: 18; color: #8b949e; content-align: right middle; padding-right: 1; }
    .field-inp { width: 1fr; height: 3; border: solid #30363d; background: #0d1117; color: #c9d1d9; }
    .field-inp:focus { border: solid #58a6ff; }
    #submit-btn-row { height: 3; layout: horizontal; margin-top: 1; }
    #btn-submit-run    { background: #238636; color: white; border: none; min-width: 18; margin-right: 1; }
    #btn-submit-cancel { background: #21262d; color: #c9d1d9; border: none; min-width: 12; }
    #btn-submit-run:hover    { background: #2ea043; }
    #btn-submit-cancel:hover { background: #30363d; }
    #submit-status { color: #8b949e; margin-top: 1; }
    """

    def __init__(self):
        super().__init__()

    def _field(self, label: str, fid: str, placeholder: str, val: str = ""):
        with Horizontal(classes="field-row"):
            yield Label(label, classes="field-lbl")
            yield Input(value=val, placeholder=placeholder, id=f"si-{fid}",
                        classes="field-inp")

    def compose(self) -> ComposeResult:
        with Vertical(id="submit-dialog"):
            yield Label("🚀  Submit new job (sbatch)", id="submit-title")
            yield from self._field("Script (.sh):",  "script",    "/path/to/job.sh")
            yield from self._field("Job name:",      "job_name",  "my_job")
            yield from self._field("Partition:",      "partition", "test")
            yield from self._field("Account:",       "account",   "user")
            yield from self._field("Nodes:",         "nodes",     "1")
            yield from self._field("GPUs per node:", "gpus",      "4")
            yield from self._field("CPUs per task:", "cpus",      "40")
            yield from self._field("Memory:",        "mem",       "64G")
            yield from self._field("Max time:",       "time",      "2:00:00")
            yield from self._field("Output log:",    "output",    "logs/%j.out")
            yield from self._field("Error log:",     "error",     "logs/%j.err")
            yield from self._field("Args extra:",    "extra",     "--exclusive")
            with Horizontal(id="submit-btn-row"):
                yield Button("▶  Submit job",          id="btn-submit-run")
                yield Button("✕  Close",              id="btn-submit-cancel")
            yield Label("", id="submit-status")

    def _get_values(self) -> dict:
        fields = ["script","job_name","partition","account","nodes",
                  "gpus","cpus","mem","time","output","error","extra"]
        return {f: self.query_one(f"#si-{f}", Input).value.strip() for f in fields}

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-submit-cancel":
            self.dismiss(None)
        elif bid == "btn-submit-run":
            vals = self._get_values()
            ok, msg = submit_job(vals)
            status = self.query_one("#submit-status", Label)
            if ok:
                status.update(f"[bold green]✓ Submitted: {msg}[/]")
                self.app.notify(f"Job submitted: {msg}", timeout=5)
                self.set_timer(2.0, lambda: self.dismiss(vals))
            else:
                status.update(f"[bold red]✗ Error: {msg}[/]")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)



class ArrayJobModal(ModalScreen):
    """Shows all tasks of an array job with individual state."""
    DEFAULT_CSS = """
    ArrayJobModal { align: center middle; }
    #array-dialog { width: 90; height: 30; background: #161b22;
                    border: solid #30363d; padding: 1 2; layout: vertical; }
    #array-title  { color: #f0883e; text-style: bold; margin-bottom: 1; }
    #array-table  { height: 1fr; }
    #array-summary { color: #8b949e; margin-top: 1; }
    #btn-array-close { background: #21262d; color: #c9d1d9; border: none;
                       min-width: 14; margin-top: 1; }
    """

    def __init__(self, jobid: str):
        super().__init__()
        self._jobid = jobid

    def compose(self) -> ComposeResult:
        with Vertical(id="array-dialog"):
            yield Label(f"Array job {self._jobid} — loading tasks...", id="array-title")
            tbl = DataTable(id="array-table")
            tbl.add_columns("TASK ID", "NAME",   "STATE",  "EXIT", "ELAPSED", "NODES", "START")
            yield tbl
            yield Label("", id="array-summary")
            yield Button("✕  Close  [Esc]", id="btn-array-close")

    def on_mount(self) -> None:
        self._worker_load()

    @work(thread=True)
    def _worker_load(self) -> None:
        tasks = expand_array_job(self._jobid)
        self.app.call_from_thread(self._populate, tasks)

    def _populate(self, tasks: list[dict]) -> None:
        tbl = self.query_one("#array-table", DataTable)
        if not tasks:
            self.query_one("#array-title", Label).update(
                f"[yellow]Array job {self._jobid} — no tasks found in sacct[/]")
            return
        state_count = {}
        for t in tasks:
            st = t["state"]
            state_count[st] = state_count.get(st, 0) + 1
            style = state_style(st)
            tbl.add_row(
                Text(t["jobid"],   style="cyan"),
                Text(t["name"][:22]),
                Text(st,           style=style),
                Text(t["exitcode"],style="red" if t["exitcode"] not in ("0:0","") else "dim"),
                Text(t["elapsed"], style="white"),
                Text(t["nodes"][:20]),
                Text(t["start"][:16], style="dim"),
            )
        total = len(tasks)
        ok    = state_count.get("COMPLETED", 0)
        fail  = sum(v for k, v in state_count.items() if k in ("FAILED","TIMEOUT","OUT_OF_MEMORY"))
        run   = state_count.get("RUNNING", 0)
        pend  = state_count.get("PENDING", 0)
        self.query_one("#array-title", Label).update(
            f"Array job [bold cyan]{self._jobid}[/] — {total} tasks")
        self.query_one("#array-summary", Label).update(
            f"  [green]✓ {ok} COMPLETED[/]  "
            f"[red]✗ {fail} FAILED/TIMEOUT[/]  "
            f"[cyan]▶ {run} RUNNING[/]  "
            f"[white]⏳ {pend} PENDING[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-array-close":
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class DependencyTreeModal(ModalScreen):
    """Shows the dependency tree of a job in ASCII."""
    DEFAULT_CSS = """
    DependencyTreeModal { align: center middle; }
    #dep-dialog { width: 80; height: 28; background: #161b22;
                  border: solid #30363d; padding: 1 2; layout: vertical; }
    #dep-title  { color: #8957e5; text-style: bold; margin-bottom: 1; }
    #dep-log    { height: 1fr; border: solid #21262d; background: #0d1117; }
    #btn-dep-close { background: #21262d; color: #c9d1d9; border: none;
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
            yield Button("✕  Close  [Esc]", id="btn-dep-close")

    def on_mount(self) -> None:
        self._worker_load()

    @work(thread=True)
    def _worker_load(self) -> None:
        tree = get_dependency_tree(self._jobid)
        self.app.call_from_thread(self._render_dep_tree, tree)

    def _render_dep_tree(self, tree: list) -> None:
        log = self.query_one("#dep-log", RichLog)
        STATE_ICONS = {
            "RUNNING":   ("▶", "bold cyan"),
            "COMPLETED": ("✓", "bold green"),
            "PENDING":   ("⏳", "white"),
            "FAILED":    ("✗", "bold red"),
            "TIMEOUT":   ("⏱", "bold yellow"),
            "CANCELLED": ("⊘", "yellow"),
            "UNKNOWN":   ("?", "dim"),
        }
        if not tree:
            log.write(Text("  No dependencies found or job not in scontrol.", style="dim"))
            self.query_one("#dep-title", Label).update(
                f"Dependencies for [bold cyan]{self._jobid}[/] — none")
            return
        self.query_one("#dep-title", Label).update(
            f"Dependency tree — job [bold cyan]{self._jobid}[/]")
        for row in tree:
            depth, jid, state, dep_str, name = row
            icon, col = STATE_ICONS.get(state, ("?", "dim"))
            if depth == 0:
                prefix = ""
            else:
                prefix = "  " * (depth - 1) + "  └─ depends on: "
            dep_info = f"  [dep: {dep_str}]" if dep_str not in ("(none)", "(null)") else ""
            line = Text()
            line.append(prefix, style="#484f58")
            line.append(f"{icon} ", style=col)
            line.append(f"Job {jid}", style="bold white")
            if name:
                line.append(f" ({name})", style="#8b949e")
            line.append(f"  [{state}]", style=col)
            if dep_info:
                line.append(dep_info, style="#484f58")
            log.write(line)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-dep-close":
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    #confirm-box {
        width: 62; height: 11;
        background: #161b22; border: double #f85149; padding: 1 2;
    }
    #confirm-title { text-style: bold; color: #f85149; margin-bottom: 1; }
    #confirm-msg   { color: #c9d1d9; margin-bottom: 1; }
    #confirm-buttons { margin-top: 1; align: center middle; height: 3; }
    Button { margin: 0 1; }
    #btn-yes { background: #da3633; color: white; border: none; }
    #btn-no  { background: #21262d; color: #c9d1d9; border: none; }
    #btn-yes:hover { background: #f85149; }
    #btn-no:hover  { background: #30363d; }
    """
    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title, self._message = title, message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._title,   id="confirm-title")
            yield Label(self._message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("✗  Go back",  id="btn-no",  variant="default")
                yield Button("✓  Confirm",  id="btn-yes", variant="error")

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
    #detail-box { width: 90%; height: 80%; background: #0d1117; border: solid #30363d; }
    #detail-title { background: #161b22; color: #58a6ff; text-style: bold; padding: 0 2; height: 1; }
    #detail-text  { height: 1fr; padding: 1 2; }
    #detail-close { background: #21262d; color: #c9d1d9; border: none; margin: 0 2 1 2; width: 100%; }
    #detail-close:hover { background: #30363d; }
    """
    def __init__(self, jobid: str) -> None:
        super().__init__()
        self._jobid = jobid

    def compose(self) -> ComposeResult:
        out = run_out(["scontrol", "show", "job", self._jobid])
        if not out.strip():
            out = f"Could not retrieve info for job {self._jobid}.\n(Already finished?)"
        with Vertical(id="detail-box"):
            yield Label(f"  📋  scontrol show job {self._jobid}", id="detail-title")
            yield TextArea(out, id="detail-text", read_only=True)
            yield Button("✕  Close  [Esc]", id="detail-close")

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
        try:
            r = subprocess.run(["which", binary], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                found.append((label, r.stdout.strip()))
        except Exception:
            pass
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
        width: 58; height: auto; background: #161b22;
        border: double #1f6feb; padding: 1 2;
    }
    #picker-title { color: #58a6ff; text-style: bold; margin-bottom: 1; }
    #picker-path  { color: #484f58; margin-bottom: 1; }
    .editor-btn {
        width: 100%; background: #21262d; color: #c9d1d9;
        border: none; margin-bottom: 1;
    }
    .editor-btn:hover { background: #1f6feb; color: white; }
    #btn-picker-cancel {
        width: 100%; background: #0d1117; color: #484f58;
        border: solid #30363d; margin-top: 1;
    }
    #btn-picker-cancel:hover { background: #21262d; color: #c9d1d9; }
    """

    def __init__(self, file_path: str) -> None:
        super().__init__()
        self._file_path = file_path

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("✏  Open with editor", id="picker-title")
            yield Label(f"  {self._file_path}", id="picker-path")
            if AVAILABLE_EDITORS:
                for label, binary in AVAILABLE_EDITORS:
                    yield Button(f"  {label}   ({binary})",
                                 id=f"editor--{label}", classes="editor-btn")
            else:
                yield Label("  No editors found in PATH.", id="no-editors")
            yield Button("✕  Cancel  [Esc]", id="btn-picker-cancel")

    def on_mount(self) -> None:
        btns = list(self.query(Button))
        if btns: btns[0].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
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
        width: 95%; height: 92%; background: #0d1117;
        border: solid #1f6feb; layout: vertical;
    }
    #mon-title {
        background: #0d1f3c; color: #58a6ff; text-style: bold;
        padding: 0 2; height: 1;
    }
    #mon-keys-row {
        height: 1; background: #090f17; border-bottom: solid #1c2128;
        align: left middle; padding: 0 2;
    }
    #mon-keys-label { color: #3d444d; }
    #mon-scroll { height: 1fr; border: none; }
    #mon-content { height: auto; padding: 1 2; }
    #mon-footer {
        height: 3; background: #0d1f3c; border-top: solid #1f2d3d;
        align: left middle; padding: 0 2;
    }
    #mon-refresh-lbl  { color: #58a6ff; margin-right: 2; }
    #btn-mon-refresh  { background: #21262d; color: #c9d1d9; border: none; margin-right: 1; min-width: 18; }
    #btn-mon-close    { background: #21262d; color: #c9d1d9; border: none; min-width: 16; }
    #btn-mon-refresh:hover { background: #1f6feb; color: white; }
    #btn-mon-close:hover   { background: #30363d; }
    """

    _timer: Timer | None = None
    REFRESH_SECS = 8     # resource monitor (ssh to nodes, more expensive)

    def __init__(self, jobid: str, job_name: str = "", state: str = "") -> None:
        super().__init__()
        self._jobid    = jobid
        self._job_name = job_name
        self._state    = state

    def compose(self) -> ComposeResult:
        with Vertical(id="mon-box"):
            yield Label(
                f"  📊  Monitor — Job {self._jobid}  ·  {self._job_name}  ·  {self._state}",
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
                yield Button("↻  Refresh  [r]", id="btn-mon-refresh")
                yield Button("✕  Close  [Esc]", id="btn-mon-close")

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

    @work(thread=True)
    def _fetch_resources(self, jobid: str) -> None:
        lines: list[tuple[str, str]] = []  # (text, style)

        def sep(title: str = "") -> None:
            if title:
                lines.append((f"── {title} {'─'*(60-len(title))}", "bold #30363d"))
            else:
                lines.append(("─" * 64, "#21262d"))

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
                lines.append((f"  Partition : {p[0]}   State: {p[1]}   CPUs: {p[4]}   Mem: {p[5]}   GPUs: {p[6]}", "white"))
                lines.append((f"  Nodes     : {p[7]}", "white"))
                lines.append((f"  Elapsed   : {p[2]}  /  Left: {p[3]}", "white"))
                col = bar_color(pct_time)
                lines.append((f"  Timeline  : [{bar}] {pct_time}%", col))
        else:
            lines.append(("  Job not found in queue (may have already finished)", "dim"))

        # ── SSTAT (actual CPU/MEM for the job) ──
        sep("USAGE VIA SSTAT (job accounting)")
        sstat = get_job_sstat(jobid)
        lines.append((f"  AvgCPU: {sstat['avg_cpu']}   MaxRSS: {sstat['max_rss']}   Tasks: {sstat['tasks']}", "cyan"))

        # ── NODES ──
        nodes = get_job_nodes(jobid)
        if not nodes:
            sep("NODES")
            lines.append(("  No nodes assigned (job still PENDING?)", "dim"))
        else:
            for node in nodes[:8]:  # limit to 8 nodes to avoid overflow
                sep(f"NODE: {node}")

                # ── GPU ──
                gpus = get_node_gpu_info(node)
                if gpus:
                    lines.append(("  GPU  IDX  NAME                      UTIL       MEM USED / TOTAL       TEMP    POWER", "bold #58a6ff"))
                    for g in gpus:
                        util_bar = make_bar(g["util"], 16)
                        util_col = bar_color(g["util"])
                        mem_pct  = int(g["mem_used"] / g["mem_total"] * 100) if g["mem_total"] > 0 else 0
                        mem_bar  = make_bar(mem_pct, 16)
                        mem_col  = bar_color(mem_pct)
                        lines.append((
                            f"  GPU  [{g['index']:>2}]  {g['name']:<24}  "
                            f"[{util_bar}] {g['util']:>3}%  "
                            f"[{mem_bar}] {g['mem_used']:>6}/{g['mem_total']:<6} MB  "
                            f"{g['temp']:>4}°C  {g['power']:>6}W",
                            util_col
                        ))
                else:
                    lines.append(("  GPU  (no GPUs or no nvidia-smi access on this node)", "dim"))

                # ── CPU / MEM ──
                cpu_mem = get_node_cpu_mem(node)
                cpu_pct = cpu_mem["cpu_pct"]
                mem_pct = cpu_mem["mem_pct"]
                mem_used_gb = cpu_mem["mem_used_kb"] / 1024 / 1024
                mem_total_gb = cpu_mem["mem_total_kb"] / 1024 / 1024

                cpu_bar = make_bar(cpu_pct, 20)
                mem_bar_s = make_bar(mem_pct, 20)

                lines.append((f"  CPU  [{cpu_bar}] {cpu_pct:>3}%   Load: {cpu_mem['load']}", bar_color(cpu_pct)))
                lines.append((
                    f"  MEM  [{mem_bar_s}] {mem_pct:>3}%   "
                    f"{mem_used_gb:.1f} / {mem_total_gb:.1f} GB",
                    bar_color(mem_pct)
                ))

        sep()
        lines.append((f"  Last update: {ts}  │  Job {jobid}", "dim"))

        self.app.call_from_thread(self._apply_monitor, lines, ts)

    def _apply_monitor(self, lines: list[tuple[str, str]], ts: str) -> None:
        self.query_one("#mon-refresh-lbl", Label).update(f"  ↻ {ts}")
        log = self.query_one("#mon-content", RichLog)
        log.clear()
        for text, style in lines:
            log.write(Text(text, style=style))


class LogViewerModal(ModalScreen):
    DEFAULT_CSS = """
    LogViewerModal { align: center middle; }
    #log-box { width: 95%; height: 90%; background: #0d1117; border: solid #238636; }
    #log-title { background: #0f2b0f; color: #3fb950; text-style: bold; padding: 0 2; height: 1; }
    #log-tab-row {
        height: 3; background: #161b22; border-bottom: solid #30363d;
        align: left middle; padding: 0 2;
    }
    #btn-show-stdout { background: #1f6feb; color: white; border: none; margin-right: 1; min-width: 18; }
    #btn-show-stderr { background: #9e6a03; color: white; border: none; margin-right: 1; min-width: 18; }
    #btn-show-stdout:hover { background: #388bfd; }
    #btn-show-stderr:hover { background: #d29922; }
    #path-label { color: #484f58; margin-left: 2; }
    #log-content { height: 1fr; background: #0d1117; color: #c9d1d9; border: none; padding: 0 1; }
    #log-footer-row {
        height: 3; background: #161b22; border-top: solid #30363d;
        align: left middle; padding: 0 2;
    }
    #refresh-label   { color: #3fb950; margin-right: 2; }
    #btn-log-refresh { background: #21262d; color: #c9d1d9; border: none; margin-right: 1; min-width: 18; }
    #btn-log-refresh  { background: #21262d; color: #c9d1d9; border: none; margin-right: 1; min-width: 16; }
    #btn-open-editor  { background: #6e40c9; color: white;   border: none; margin-right: 1; min-width: 22; }
    #btn-log-close    { background: #21262d; color: #c9d1d9; border: none; min-width: 16; }
    #btn-log-refresh:hover  { background: #30363d; }
    #btn-open-editor:hover  { background: #8957e5; }
    #btn-log-close:hover    { background: #30363d; }
    #log-keys-row {
        height: 1; background: #0a0f14; border-top: solid #1c2128;
        align: left middle; padding: 0 2;
    }
    #log-keys-label { color: #3d444d; }
    """

    _showing: str = "stdout"
    _timer: Timer | None = None
    _live: bool = True
    _current_path: str = ""

    def __init__(self, jobid: str, stdout_path: str, stderr_path: str,
                 job_name: str = "", state: str = "", live: bool = True) -> None:
        super().__init__()
        self._jobid        = jobid
        self._stdout_path  = stdout_path
        self._stderr_path  = stderr_path
        self._job_name     = job_name
        self._state        = state
        self._live         = live

    def compose(self) -> ComposeResult:
        live_indicator = "  🔴 LIVE" if self._live else "  📁 HISTORY"
        title = f"  📄  Job {self._jobid}  ·  {self._job_name}  ·  {self._state}{live_indicator}"
        with Vertical(id="log-box"):
            yield Label(title, id="log-title")
            with Horizontal(id="log-tab-row"):
                yield Button("📤 stdout", id="btn-show-stdout")
                yield Button("⚠  stderr", id="btn-show-stderr")
                yield Label("", id="path-label")
            yield RichLog(id="log-content", highlight=True, markup=False, wrap=True)
            with Horizontal(id="log-keys-row"):
                yield Label(
                    "  Tab/S+Tab: buttons  │  ↑↓: scroll  │  PgUp/PgDn  │  r: refresh  │  e: open editor  │  Esc: close",
                    id="log-keys-label"
                )
            with Horizontal(id="log-footer-row"):
                refresh_txt = f"↻ auto-refresh every {LOG_REFRESH_SECS}s" if self._live else "📁 static log (job finished)"
                yield Label(refresh_txt, id="refresh-label")
                yield Button("↻  Refresh  [r]",   id="btn-log-refresh")
                yield Button("✏  Editor  [e]",     id="btn-open-editor")
                yield Button("✕  Close  [Esc]",    id="btn-log-close")

    def on_mount(self) -> None:
        self._show_stream("stdout")
        if self._live:
            self._timer = self.set_interval(LOG_REFRESH_SECS, self._auto_refresh)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if   bid == "btn-show-stdout":  self._show_stream("stdout")
        elif bid == "btn-show-stderr":  self._show_stream("stderr")
        elif bid == "btn-log-refresh":  self._show_stream(self._showing)
        elif bid == "btn-open-editor":  self._pick_editor()
        elif bid == "btn-log-close":    self._close()

    def on_key(self, event) -> None:
        if   event.key == "escape":    self._close()
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

    def _auto_refresh(self) -> None:
        self._show_stream(self._showing)

    @work(thread=True)
    def _show_stream(self, which: str) -> None:
        self._showing = which
        path    = self._stdout_path if which == "stdout" else self._stderr_path
        content = tail_file(path)
        self.app.call_from_thread(self._apply_log_content, which, path, content)

    def _apply_log_content(self, which: str, path: str, content: str) -> None:
        self._current_path = path
        self.query_one("#btn-show-stdout", Button).set_class(which == "stdout", "active-log")
        self.query_one("#btn-show-stderr", Button).set_class(which == "stderr", "active-log")
        self.query_one("#path-label", Label).update(f"  {path}")
        log = self.query_one("#log-content", RichLog)
        log.clear()
        if not content.strip() or content.startswith("("):
            log.write(Text(content, style="dim italic"))
            return
        for line in content.splitlines():
            lower = line.lower()
            if any(k in lower for k in ("error", "exception", "traceback", "fatal", "oom")):
                log.write(Text(line, style="bold red"))
            elif any(k in lower for k in ("warning", "warn")):
                log.write(Text(line, style="yellow"))
            elif any(k in lower for k in ("success", "done", "finished", "completed")):
                log.write(Text(line, style="bold green"))
            else:
                log.write(Text(line, style="#c9d1d9"))
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
        text.append(f"  ⏱ {last_update}", style="dim")
        text.append("    │    ", style="dim")
        text.append(f"TOTAL {stats['total']}", style="bold white")
        text.append("  •  ", style="dim")
        text.append(f"MINE {stats['mine']}", style="bold yellow")
        text.append("    │    ", style="dim")
        text.append(f"▶ RUNNING {r}", style="bold green")
        text.append("  ")
        text.append(f"⧗ PENDING {pd}", style="bold cyan")
        text.append("  ")
        text.append(f"↺ COMPLETING {cg}", style="green")
        text.append("    │    ", style="dim")
        text.append(f"🖥  GPUs in use: {stats['running_gpus']}", style="bold magenta")
        self.update(text)


class ActionBar(Static):
    DEFAULT_CSS = """
    ActionBar {
        height: 5; background: #0d1117;
        border-top: solid #30363d; padding: 0 1; layout: vertical;
    }
    #action-row-1 { height: 2; align: left middle; }
    #action-row-2 { height: 2; align: left middle; }
    ActionBar Button {
        margin: 0 1 0 0; height: 3; border: none; min-width: 22;
        content-align: center middle;
    }
    #selected-label  { color: #8b949e; margin-right: 2; width: 20; height: 3; content-align: left middle; }
    #selected-spacer { width: 20; margin-right: 2; height: 3; }
    #btn-detail  { background: #1f6feb; color: white; }
    #btn-logs    { background: #238636; color: white; }
    #btn-monitor { background: #6e40c9; color: white; }
    #btn-hold    { background: #9e6a03; color: white; }
    #btn-release { background: #1a7f37; color: white; }
    #btn-scancel { background: #da3633; color: white; }
    #btn-detail:hover   { background: #388bfd; }
    #btn-logs:hover     { background: #2ea043; }
    #btn-monitor:hover  { background: #8957e5; }
    #btn-hold:hover     { background: #d29922; }
    #btn-release:hover  { background: #2ea043; }
    #btn-scancel:hover  { background: #f85149; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="action-row-1"):
            yield Label("Job: -", id="selected-label")
            yield Button("D - Details",  id="btn-detail")
            yield Button("L - View Logs", id="btn-logs")
            yield Button("M - Monitor",  id="btn-monitor")
        with Horizontal(id="action-row-2"):
            yield Label("", id="selected-spacer")
            yield Button("H - Hold",     id="btn-hold")
            yield Button("U - Release",  id="btn-release")
            yield Button("X - Cancel", id="btn-scancel")

    def set_selected(self, jobid: str | None) -> None:
        lbl = self.query_one("#selected-label", Label)
        if jobid:
            lbl.update("Job: " + jobid)
        else:
            lbl.update("Job: -")

class HistoryTable(DataTable):
    """Panel 4: searchable history of all past jobs."""
    COLS = [
        ("JOBID", 10), ("NAME", 22), ("USER", 12), ("PARTITION", 11),
        ("STATE", 11), ("CPUs", 5), ("MEM", 7), ("GPUs", 5),
        ("FIRST SEEN", 18), ("LAST SEEN", 18),
    ]

    def on_mount(self) -> None:
        self.cursor_type = "row"
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    def populate(self, history: list[dict], filter_text: str = "") -> None:
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
            self.add_row(
                Text(e["jobid"],     style="bold yellow"),
                Text(e["name"],      style="white"),
                Text(e["user"],      style="white"),
                Text(e["partition"], style="white"),
                Text(st,             style=state_style(st)),
                Text(e.get("cpus", ""), style="white"),
                Text(e.get("mem",  ""), style="white"),
                Text(e.get("gpus", ""), style="cyan"),
                Text(e.get("first_seen", ""), style="dim"),
                Text(e.get("last_seen",  ""), style="dim"),
            )
            if e["jobid"] == selected_jobid:
                new_cursor = idx
            idx += 1
        if new_cursor is not None:
            self.move_cursor(row=new_cursor)

    def get_selected_jobid(self) -> str | None:
        if self.row_count == 0: return None
        try: return str(self.get_cell_at((self.cursor_row, 0))).strip() or None
        except: return None


class SqueueTable(DataTable):
    COLS = [
        ("JOBID", 10), ("PARTITION", 11), ("NAME", 20), ("USER", 12),
        ("STATE", 11), ("TIME", 8), ("TIME LEFT", 9), ("CPUs", 5),
        ("MEM", 7), ("GPUs", 5), ("NODES", 12), ("REASON", 24),
    ]
    def on_mount(self) -> None:
        self.cursor_type = "row"
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    def refresh_jobs(self, jobs: list[dict]) -> None:
        selected_jobid = None
        if self.row_count > 0:
            try: selected_jobid = str(self.get_cell_at((self.cursor_row, 0))).strip()
            except: pass
        self.clear()
        new_cursor = None
        for idx, j in enumerate(jobs):
            rs = "bold yellow" if j["user"] == MY_USER else "white"
            st = j["state"]
            def c(val, extra=""): return Text(val, style=f"{rs} {extra}".strip())
            self.add_row(
                c(j["jobid"]), c(j["partition"]), c(j["name"]), c(j["user"]),
                Text(st, style=state_style(st)),
                c(j["time"]), c(j["time_left"]), c(j["cpus"]),
                c(j["mem"]),  c(j["gpus"]),      c(j["nodes"]), c(j["reason"]),
            )
            if j["jobid"] == selected_jobid: new_cursor = idx
        if new_cursor is not None: self.move_cursor(row=new_cursor)

    def get_selected_jobid(self) -> str | None:
        if self.row_count == 0: return None
        try: return str(self.get_cell_at((self.cursor_row, 0))).strip() or None
        except: return None

    def get_selected_user(self) -> str | None:
        if self.row_count == 0: return None
        try: return str(self.get_cell_at((self.cursor_row, 3))).strip() or None
        except: return None


class MyJobsTable(DataTable):
    COLS = [
        ("JOBID", 10), ("NAME", 24), ("STATE", 11), ("TIME", 10),
        ("TIME LEFT", 10), ("CPUs", 5), ("MEM", 8), ("GPUs", 8),
        ("NODES", 16), ("REASON", 28),
    ]
    def on_mount(self) -> None:
        self.cursor_type = "row"
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    def refresh_jobs(self, jobs: list[dict]) -> None:
        selected_jobid = None
        if self.row_count > 0:
            try: selected_jobid = str(self.get_cell_at((self.cursor_row, 0))).strip()
            except: pass
        self.clear()
        mine = [j for j in jobs if j["user"] == MY_USER]
        if not mine:
            self.add_row(
                Text("—", style="dim"),
                Text(f"No jobs found for user {MY_USER}", style="dim italic"),
                *[Text("", style="dim")] * 8,
            )
            return
        new_cursor = None
        for idx, j in enumerate(mine):
            st = j["state"]
            self.add_row(
                Text(j["jobid"],     style="bold yellow"),
                Text(j["name"],      style="bold yellow"),
                Text(st,             style=state_style(st)),
                Text(j["time"],      style="yellow"),
                Text(j["time_left"], style="yellow"),
                Text(j["cpus"],      style="yellow"),
                Text(j["mem"],       style="yellow"),
                Text(j["gpus"],      style="yellow"),
                Text(j["nodes"],     style="yellow"),
                Text(j["reason"],    style="dim"),
            )
            if j["jobid"] == selected_jobid: new_cursor = idx
        if new_cursor is not None: self.move_cursor(row=new_cursor)

    def get_selected_jobid(self) -> str | None:
        if self.row_count == 0: return None
        try:
            val = str(self.get_cell_at((self.cursor_row, 0))).strip()
            return val if val != "—" else None
        except: return None

    def get_selected_user(self) -> str | None:
        return MY_USER


class SinfoTable(DataTable):
    COLS = [
        ("NODE", 16), ("PARTITION", 12), ("STATE", 10),
        ("CPU A/I/O/T", 12), ("MEM (MB)", 10), ("GRES", 20), ("FEATURES", 30),
    ]
    STATE_COLORS = {
        "idle": "green", "alloc": "bold green", "mix": "yellow",
        "down": "bold red", "drain": "red", "drng": "red",
    }
    def on_mount(self) -> None:
        for col, width in self.COLS:
            self.add_column(col, width=width, key=col)

    def refresh_nodes(self, nodes: list[dict]) -> None:
        self.clear()
        if not nodes:
            self.add_row(*[Text("n/a", style="dim")] * 7); return
        for n in nodes:
            st = n["state"]
            color = self.STATE_COLORS.get(st.lower().rstrip("*"), "white")
            self.add_row(
                Text(n["node"], style="white"),     Text(n["partition"], style="white"),
                Text(st, style=color),              Text(n["cpu_aiotd"], style="white"),
                Text(n["mem"], style="white"),      Text(n["gres"], style="cyan"),
                Text(n["features"], style="dim"),
            )


class EventLog(RichLog):
    """Persistent event log — survives dashboard restarts."""

    def on_mount(self) -> None:
        past = load_event_log(n=200)
        if past:
            self.write(Text.assemble(
                ("─" * 22 + " previous session " + "─" * 21, "dim #484f58")))
            for line in past:
                if line.startswith("[") and "] " in line:
                    end = line.index("] ")
                    self.write(Text.assemble(
                        (line[:end + 1] + " ", "dim #484f58"),
                        (line[end + 2:],        "#6e7681"),
                    ))
                else:
                    self.write(Text(line, style="dim"))
            self.write(Text.assemble(
                ("─" * 22 + " current session " + "─" * 23,  "dim #30363d")))
        self.scroll_end(animate=False)

    def log_event(self, msg: str, style: str = "white") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.write(Text.assemble((f"[{ts}] ", "dim"), (msg, style)))
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
        height: 1fr; layout: vertical; background: #0d1117;
    }
    #stats-toolbar {
        height: 3; background: #161b22; border-bottom: solid #30363d;
        align: left middle; padding: 0 2;
    }
    #stats-toolbar-label { color: #58a6ff; text-style: bold; margin-right: 2; }
    #btn-stats-refresh { background: #21262d; color: #c9d1d9; border: none; min-width: 20; }
    #btn-stats-refresh:hover { background: #1f6feb; color: white; }
    #stats-content {
        height: 1fr; border: none;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="stats-toolbar"):
            yield Label("📊  History analysis",        id="stats-toolbar-label")
            yield Button("↻  Recalculate",                   id="btn-stats-refresh")
        yield RichLog(id="stats-content", highlight=False, markup=False,
                      wrap=True, max_lines=5000)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-stats-refresh":
            self.app.refresh_stats()

    def on_key(self, event) -> None:
        log = self.query_one("#stats-content", RichLog)
        if   event.key == "up":       log.scroll_relative(y=-3)
        elif event.key == "down":     log.scroll_relative(y=3)
        elif event.key == "pageup":   log.scroll_relative(y=-20)
        elif event.key == "pagedown": log.scroll_relative(y=20)
        elif event.key == "home":     log.scroll_home()
        elif event.key == "end":      log.scroll_end(animate=False)

    def render_stats(self, stats: dict, error_patterns: list | None = None) -> None:
        log = self.query_one("#stats-content", RichLog)
        log.clear()

        def line(text: str = "", style: str = "white") -> None:
            log.write(Text(text, style=style))

        def sep(title: str = "") -> None:
            if title:
                bar = "─" * max(0, 62 - len(title))
                line(f"── {title} {bar}", "bold #30363d")
            else:
                line("─" * 66, "#21262d")

        if not stats:
            line("  No history data yet.", "dim")
            return

        ts = datetime.now().strftime("%H:%M:%S  %d/%m/%Y")

        # ── RESUMEN GENERAL ──
        sep("RESUMEN GENERAL")
        line(f"  Total jobs in history : {stats['total']}", "white")
        line(f"  Completed successfully    : {stats['succeeded']}  ({stats['success_rt']}%)", "bold green")
        line(f"  Failed / Timeout        : {stats['failed']}    ({stats['fail_rt']}%)", "bold red")
        line(f"  Otros (running/pending) : {stats['other']}", "dim")

        # ── SUCCESS/FAILURE BAR ──
        sep()
        total  = stats["total"]
        ok_w   = int(40 * stats["succeeded"] / total) if total else 0
        fail_w = int(40 * stats["failed"]    / total) if total else 0
        rest_w = 40 - ok_w - fail_w
        bar    = "█" * ok_w + "█" * fail_w + "░" * rest_w
        ok_bar   = Text("█" * ok_w,   style="bold green")
        fail_bar = Text("█" * fail_w, style="bold red")
        rest_bar = Text("░" * rest_w, style="#30363d")
        full_bar = Text("  [") + ok_bar + fail_bar + rest_bar + Text("]")
        full_bar += Text(f"  ✓ {stats['success_rt']}%  ✗ {stats['fail_rt']}%", style="white")
        log.write(full_bar)

        # ── RECURSOS CONSUMIDOS ──
        sep("RECURSOS CONSUMIDOS (jobs COMPLETED)")
        line(f"  GPU-horas totales       : {stats['gpu_hours']:.1f} h", "bold #f0883e")
        line(f"  CPU-horas totales       : {stats['cpu_hours']:.1f} h", "#79c0ff")
        avg_h = int(stats["avg_wall_hrs"])
        avg_m = int((stats["avg_wall_hrs"] - avg_h) * 60)
        line(f"  Time medio per job    : {avg_h}h {avg_m:02d}m", "cyan")

        # ── BY PARTITION ──
        sep("JOBS BY PARTITION")
        by_part = sorted(stats["by_partition"].items(), key=lambda x: -x[1])
        max_count = max((v for _, v in by_part), default=1)
        for part, count in by_part:
            bar_w = int(20 * count / max_count)
            bar = "█" * bar_w + "░" * (20 - bar_w)
            pct  = round(count / total * 100, 1) if total else 0
            line(f"  {part:<18}  [{bar}]  {count:>4} jobs  ({pct}%)", "#79c0ff")

        # ── TOP JOB NAMES ──
        sep("TOP 10 JOB NAMES")
        by_name = sorted(stats["by_name"].items(), key=lambda x: -x[1])
        for name, count in by_name[:10]:
            bar_w = int(20 * count / max(v for _, v in by_name)) if by_name else 0
            bar = "█" * bar_w + "░" * (20 - bar_w)
            line(f"  {name[:24]:<24}  [{bar}]  {count:>4}", "white")

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
                    col = "bold green" if cnt > 0 else "#21262d"
                    short_day = day[5:]  # MM-DD
                    bars_txt += Text(f"{short_day} ", style="#484f58")
                    bars_txt += Text("█" * h + "░" * (8 - h) + " ", style=col)
                log.write(bars_txt)
            line(f"  Max in one day: {max_day} jobs", "dim")

        # ── ESTADOS DESGLOSADOS ──
        sep("DESGLOSE DE ESTADOS")
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
            col = state_cols.get(state, "dim")
            pct = round(count / total * 100, 1) if total else 0
            line(f"  {state:<22}  {count:>4} jobs  ({pct}%)", col)

        sep()
        line(f"  Updated: {ts}  │  {len(stats.get('jobs_by_day', {}))} days analysed", "dim")


class SlurmDashboard(App):

    CSS = """
    Screen { background: #0d1117; }
    Header { background: #161b22; color: #58a6ff; text-style: bold; }
    Footer { background: #161b22; color: #8b949e; }
    #jobs-panel { height: 1fr; layout: vertical; background: #0d1117; }
    #jobs-toolbar {
        height: 3; background: #161b22; border-bottom: solid #30363d;
        align: left middle; padding: 0 2;
    }
    #jobs-toolbar-lbl { color: #58a6ff; text-style: bold; margin-right: 2; }
    #jobs-info-panel { height: 1fr; padding: 0; }
    #jobs-info-log   { height: 1fr; border: none; background: #0d1117; }
    #btn-jobs-new       { background: #238636; color: white; border: none; min-width: 18; margin-right: 1; }
    #btn-jobs-array     { background: #9e6a03; color: white; border: none; min-width: 20; margin-right: 1; }
    #btn-jobs-deps      { background: #6e40c9; color: white; border: none; min-width: 18; margin-right: 1; }
    #btn-jobs-new:hover       { background: #2ea043; }
    #btn-jobs-array:hover     { background: #d29922; }
    #btn-jobs-deps:hover      { background: #8957e5; }

    Button:focus      { border: tall #58a6ff; }
    Button.active-log { border: tall #3fb950; text-style: bold; }
    DataTable:focus   { border: solid #1f6feb; }
    Input:focus       { border: solid #58a6ff; }
    StatsBar {
        height: 1; background: #161b22; color: #c9d1d9;
        padding: 0 1; border-bottom: solid #30363d;
    }
    Tabs { background: #161b22; border-bottom: solid #30363d; }
    Tab { color: #8b949e; }
    Tab.-active { color: #58a6ff; background: #0d1117; text-style: bold; }
    DataTable { background: #0d1117; color: #c9d1d9; border: solid #30363d; }
    DataTable > .datatable--header {
        background: #161b22; color: #58a6ff; text-style: bold;
    }
    DataTable > .datatable--cursor { background: #1f6feb; color: white; }
    DataTable > .datatable--even-row { background: #0d1117; }
    DataTable > .datatable--odd-row  { background: #161b22; }
    EventLog { background: #0d1117; border: solid #30363d; height: 12; }
    #main-layout { height: 1fr; }
    #log-label {
        background: #161b22; color: #58a6ff;
        padding: 0 1; border-bottom: solid #30363d; height: 1;
    }
    #history-panel { height: 1fr; }
    #history-toolbar {
        height: 3; background: #161b22; border-bottom: solid #30363d;
        align: left middle; padding: 0 2;
    }
    #history-search {
        width: 40; border: solid #30363d; background: #0d1117;
        color: #c9d1d9; margin-right: 2; height: 1;
    }
    #history-hint   { color: #484f58; margin-left: 2; }
    #history-action-row {
        height: 3; background: #161b22; border-top: solid #30363d;
        align: left middle; padding: 0 2;
    }
    #history-selected { color: #8b949e; width: 30; margin-right: 2; }
    #btn-hist-logs    { background: #238636; color: white; border: none; min-width: 18; margin-right: 1; }
    #btn-hist-logs:hover    { background: #2ea043; }
    #btn-hist-monitor { background: #6e40c9; color: white; border: none; min-width: 18; margin-right: 1; }
    #btn-hist-monitor:hover { background: #8957e5; }
    #btn-hist-rerun   { background: #1f6feb; color: white; border: none; min-width: 18; }
    #btn-hist-rerun:hover   { background: #388bfd; }
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
    ]

    TITLE = "SLURM Dashboard"
    SUB_TITLE = f"user: {MY_USER}  │  refresh: {REFRESH_INTERVAL}s"

    _prev_states: dict[str, str] = {}
    _active_tab:  str = "tab-all"
    _history:     list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatsBar(id="stats-bar")
        yield Tabs(
            Tab("⚡ All Jobs",  id="tab-all"),
            Tab("👤 My Jobs",   id="tab-mine"),
            Tab("🖥  Nodes",    id="tab-nodes"),
            Tab("📋 Event Log", id="tab-log"),
            Tab("🕑 History",   id="tab-history"),
            Tab("📊 Stats",     id="tab-stats"),
            Tab("🚀 Jobs",      id="tab-jobs"),
        )
        with Container(id="main-layout"):
            yield SqueueTable(id="squeue-table")
            yield MyJobsTable(id="mine-table")
            yield SinfoTable(id="sinfo-table")
            with Vertical(id="log-panel"):
                yield Label("📋 Event Log — job state changes", id="log-label")
                yield EventLog(id="event-log", max_lines=200)
            # ── History panel ──
            with Vertical(id="history-panel"):
                with Horizontal(id="history-toolbar"):
                    yield Label("🔎 Filter: ", id="history-filter-lbl")
                    yield Input(placeholder="job name / id / state / partition…",
                                id="history-search")
                    yield Label(
                        f"↑↓ navigate  ·  l: logs  ·  m: monitor  ·  b: resubmit  ·  {HISTORY_FILE}",
                        id="history-hint"
                    )
                yield HistoryTable(id="history-table")
                with Horizontal(id="history-action-row"):
                    yield Label("Selected: —", id="history-selected")
                    yield Button("📄 [l] View Logs",     id="btn-hist-logs")
                    yield Button("📊 [m] Monitor",       id="btn-hist-monitor")
                    yield Button("↻  [b] Rerun",         id="btn-hist-rerun")
            # ── Stats panel ──
            with Vertical(id="stats-panel"):
                yield HistoryStatsPanel(id="stats-widget")
            # ── Jobs management panel ──
            with Vertical(id="jobs-panel"):
                with Horizontal(id="jobs-toolbar"):
                    yield Label("🚀  Job management", id="jobs-toolbar-lbl")
                    yield Button("▶  New job [n]",            id="btn-jobs-new")
                    yield Button("⣿  Array expand [a]",      id="btn-jobs-array")
                    yield Button("🔗  Dependencies [e]",     id="btn-jobs-deps")
                with Vertical(id="jobs-info-panel"):
                    yield RichLog(id="jobs-info-log", highlight=False,
                                  markup=False, wrap=True, max_lines=2000)
        yield ActionBar(id="action-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#mine-table").display    = False
        self.query_one("#sinfo-table").display   = False
        self.query_one("#log-panel").display     = False
        self.query_one("#history-panel").display = False
        self.query_one("#stats-panel").display   = False
        self.query_one("#jobs-panel").display    = False
        self.query_one(ActionBar).display        = True
        self._history = load_history()
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
        self.query_one(ActionBar).display        = tid in ("tab-all", "tab-mine")
        self._sync_action_bar()
        if tid == "tab-history":
            self._refresh_history_table()
        if tid == "tab-stats":
            self.refresh_stats()
        if tid == "tab-jobs":
            self._render_jobs_panel()

    # ── history search ──
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "history-search":
            self._refresh_history_table(event.value)

    def on_data_table_cursor_moved(self, _) -> None:
        self._sync_action_bar()
        if self._active_tab == "tab-history":
            jobid = self.query_one(HistoryTable).get_selected_jobid()
            lbl   = self.query_one("#history-selected", Label)
            lbl.update(f"Selected: [bold yellow]{jobid}[/]" if jobid else "Selected: —")

    def _refresh_history_table(self, filter_text: str = "") -> None:
        ft = self.query_one("#history-search", Input).value if not filter_text else filter_text
        self.query_one(HistoryTable).populate(self._history, ft)

    # ── action bar sync ──
    def _sync_action_bar(self) -> None:
        self.query_one(ActionBar).set_selected(self._get_selected_jobid())

    def _get_active_table(self):
        if self._active_tab == "tab-all":  return self.query_one(SqueueTable)
        if self._active_tab == "tab-mine": return self.query_one(MyJobsTable)
        return None

    def _get_selected_jobid(self) -> str | None:
        if self._active_tab == "tab-history":
            return self.query_one(HistoryTable).get_selected_jobid()
        t = self._get_active_table()
        return t.get_selected_jobid() if t else None

    def _get_selected_user(self) -> str | None:
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
        entry = next((e for e in self._history if e["jobid"] == jobid), None)
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
        # Update history with resolved paths
        entry = next((e for e in self._history if e["jobid"] == jobid), None)
        if entry and (paths["stdout"] or paths["stderr"]):
            entry["stdout"] = paths["stdout"]
            entry["stderr"] = paths["stderr"]
            save_history(self._history)
        self.call_from_thread(self._push_log_viewer_from_paths, jobid, paths, live)

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
        entry = next((e for e in self._history if e["jobid"] == jobid), None)
        name  = entry.get("name", "") if entry else ""
        sl    = entry.get("submit_line", "") if entry else ""
        if sl:
            hint = f"SubmitLine cached: {sl[:70]}{'…' if len(sl)>70 else ''}"
        else:
            hint = "No SubmitLine cached → will be searched in sacct/scontrol/requeue"
        self.push_screen(
            ConfirmModal("↻  Confirm resubmission", f"Job {jobid}  [{name}]\n{hint}"),
            callback=lambda ok: self._do_history_rerun(ok, jobid),
        )

    def _do_history_rerun(self, confirmed: bool, jobid: str) -> None:
        if not confirmed:
            return
        self.notify(f"Resubmitiendo job {jobid}…", timeout=3)
        self._worker_rerun(jobid)

    @work(thread=True)
    def _worker_rerun(self, jobid: str) -> None:
        entry     = next((e for e in self._history if e["jobid"] == jobid), None)
        cached_sl = entry.get("submit_line", "") if entry else ""
        ok, msg   = False, ""

        # Try cached submit_line first
        if cached_sl:
            try:
                args = shlex.split(cached_sl)
                if not args:
                    raise ValueError("empty submit_line")
                if args[0] != "sbatch":
                    args = ["sbatch"] + args
                ok, msg = run_sbatch(args)
            except Exception as e:
                ok, msg = False, str(e)

        # If no cache or it failed, search in sacct + scontrol
        if not ok:
            fresh_sl = get_submit_line(jobid)
            if fresh_sl:
                try:
                    args = shlex.split(fresh_sl)
                    if args and args[0] != "sbatch":
                        args = ["sbatch"] + args
                    ok, msg = run_sbatch(args)
                    # Save for next time
                    if ok and entry:
                        entry["submit_line"] = fresh_sl
                        save_history(self._history)
                except Exception as e:
                    ok, msg = False, str(e)

        # Last fallback: scontrol requeue
        if not ok:
            proc = subprocess.run(
                ["scontrol", "requeue", jobid],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=20,
            )
            if proc.returncode == 0:
                ok  = True
                msg = f"Job {jobid} requeued → vuelve a PENDING"
            else:
                # Construir mensaje de error informativo
                requeue_err = proc.stderr.strip()
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
        self.query_one(EventLog).log_event(f"rerun {jobid} → {msg}", "bold green")
        self.refresh_data()

    def _rerun_fail(self, jobid: str, err: str) -> None:
        self.notify(f"Error resubmitting {jobid}: {err}", severity="error", timeout=10)
        self.query_one(EventLog).log_event(f"rerun {jobid} FAIL: {err}", "bold red")

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
        entry = next((e for e in self._history if e["jobid"] == jobid), None)
        state = entry.get("state", "") if entry else ""
        # Refresca estado live desde squeue
        live_jobs = {j["jobid"]: j for j in parse_squeue()}
        live = live_jobs.get(jobid)
        if live:
            state = live["state"]
        if not self._is_live_state(state):
            self.notify(
                f"Job {jobid} is not active ({state or 'UNKNOWN'}). "
                "Solo se puede monitorizar jobs RUNNING/PENDING.",
                severity="warning", timeout=5)
            return
        name = live["name"] if live else (entry.get("name", "") if entry else "")
        self.push_screen(ResourceMonitorModal(jobid, job_name=name, state=state))

    def action_job_monitor(self) -> None:
        jobid = self._get_selected_jobid()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        # Si estamos en History, delegate to the specific helper
        if self._active_tab == "tab-history":
            self._open_history_monitor(); return
        job_name, state = "", ""
        t = self._get_active_table()
        if t and t.row_count > 0:
            try:
                job_name = str(t.get_cell_at((t.cursor_row, 2))).strip()
                state    = str(t.get_cell_at((t.cursor_row, 4))).strip()
            except Exception:
                pass
        self.push_screen(ResourceMonitorModal(jobid, job_name=job_name, state=state))

    def action_job_scancel(self) -> None:
        jobid = self._get_selected_jobid()
        user  = self._get_selected_user()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        if user != MY_USER:
            self.notify(f"Cannot cancel jobs belonging to another user ({user})",
                        severity="error"); return
        self.push_screen(
            ConfirmModal("⚠  Confirm scancel",
                         f"Cancel job  {jobid}?  This cannot be undone."),
            callback=lambda ok: self._do_scancel(ok, jobid),
        )

    def _do_scancel(self, confirmed: bool, jobid: str) -> None:
        if not confirmed: return
        _, stderr = run(["scancel", jobid])
        if stderr.strip():
            self.notify(f"scancel error: {stderr.strip()}", severity="error", timeout=6)
            self.query_one(EventLog).log_event(f"scancel {jobid} ERROR: {stderr.strip()}", "bold red")
        else:
            self.notify(f"Job {jobid} cancelled", severity="information", timeout=4)
            self.query_one(EventLog).log_event(f"scancel {jobid} → OK  (by {MY_USER})", "bold yellow")
        self.refresh_data()

    def action_job_hold(self) -> None:
        jobid = self._get_selected_jobid()
        user  = self._get_selected_user()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        if user != MY_USER:
            self.notify("Cannot hold jobs belonging to another user", severity="error"); return
        _, stderr = run(["scontrol", "hold", jobid])
        if stderr.strip():
            self.notify(f"hold error: {stderr.strip()}", severity="error", timeout=6)
        else:
            self.notify(f"Job {jobid} placed on hold", severity="information", timeout=3)
            self.query_one(EventLog).log_event(f"scontrol hold {jobid} → OK", "yellow")
        self.refresh_data()

    def action_job_release(self) -> None:
        jobid = self._get_selected_jobid()
        user  = self._get_selected_user()
        if not jobid:
            self.notify("Select a job first", severity="warning"); return
        if user != MY_USER:
            self.notify("Cannot release jobs belonging to another user", severity="error"); return
        _, stderr = run(["scontrol", "release", jobid])
        if stderr.strip():
            self.notify(f"release error: {stderr.strip()}", severity="error", timeout=6)
        else:
            self.notify(f"Job {jobid} released", severity="information", timeout=3)
            self.query_one(EventLog).log_event(f"scontrol release {jobid} → OK", "bold green")
        self.refresh_data()

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
            # Normalise "CANCELLED by 1234" → "CANCELLED"
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
            entry = next((e for e in self._history if e["jobid"] == jid), None)
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

    @work(thread=True)
    def refresh_data(self) -> None:
        jobs  = parse_squeue()
        nodes = parse_sinfo()
        stats = compute_stats(jobs)
        ts    = datetime.now().strftime("%H:%M:%S")
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
        for j in jobs:
            if j["jobid"] not in gone_jids:
                self._history = upsert_history(self._history, j)
        save_history(self._history)

        # Only refresh history table if there are no pending sacct lookups.
        # If there are gone jobs, _apply_resolve_gone will do the refresh
        # once sacct returns the real final states.
        if self._active_tab == "tab-history" and not gone_jids:
            self._refresh_history_table()

        if gone_jids:
            self._worker_resolve_gone(list(gone_jids))
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
        for jid, real_state, now in updates:
            entry = next((e for e in self._history if e["jobid"] == jid), None)
            if entry:
                old_state = entry.get("state", "?")
                entry["state"]     = real_state
                entry["last_seen"] = now
                changed = True
            else:
                old_state = "?"
            col = state_style(real_state)
            log.log_event(f"Job {jid}: {old_state} → {real_state} (sacct)", col)
        if changed:
            save_history(self._history)
            # Always refresh so data is correct whenever user switches to History
            self._refresh_history_table()

    def refresh_stats(self) -> None:
        self._worker_stats()

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

    def _render_jobs_panel(self) -> None:
        log = self.query_one("#jobs-info-log", RichLog)
        log.clear()
        log.write(Text("── Keyboard shortcuts "
                       + "─" * 45, style="bold #30363d"))
        shortcuts = [
            ("N", "New job — open sbatch form with all fields"),
            ("A", "Array expand — expand the selected array job into tasks"),
            ("E", "Dependencies — show the dependency tree for the selected job"),
        ]
        for key, desc in shortcuts:
            line = Text(f"  [{key}]  ", style="bold yellow")
            line.append(desc, style="white")
            log.write(line)
        log.write(Text(""))
        log.write(Text("── Workflow "
                       + "─" * 55, style="bold #30363d"))
        tips = [
            "1. Press N  →  fill the sbatch form  →  submit your job",
            "2. Select an array job in All Jobs  →  A to see individual tasks",
            "3. Select any job  →  E to see its dependency tree",
        ]
        for tip in tips:
            log.write(Text(f"  {tip}", style="#8b949e"))


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
    En Python 3.9 + Textual, if the environment (HPC modules, nvcc, pipes)
    deja stdin en O_NONBLOCK, linux_driver lanza BlockingIOError [Errno 11].
    Forzamos stdin a modo blocking antes de arrancar la TUI.
    """
    import sys, fcntl
    try:
        fd    = sys.stdin.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if flags & os.O_NONBLOCK:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    except Exception:
        pass  # stdin no es un fd real (ej. redirigido) — ignorar


if __name__ == "__main__":
    _fix_stdin_blocking()
    app = SlurmDashboard()
    app.run()
    # If the user asked to open an editor, replace this process with it
    req = getattr(app, "_open_editor_after_exit", None)
    if req:
        binary, path = req
        _exec_editor(binary, path)