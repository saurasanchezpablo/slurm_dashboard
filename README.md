# Slurm Dashboard

A fast, terminal-based user interface (TUI) for monitoring and managing Slurm jobs. 

Running `squeue`, `sacct`, and `tail -f` repeatedly can get tedious. This dashboard provides a centralized, interactive view of your HPC jobs directly from your SSH session, without requiring X11 forwarding, web servers, or complex setups.

![Main Dashboard View](assets/screenshot-main.png)

## Features

- **Live Queue Monitoring:** Watch your jobs progress in real-time. 
- **Integrated Log Viewer:** Read `stdout` and `stderr` directly in the UI. Auto-resolves Slurm patterns (like `%j` and `%J`) and live-tails the file every 5 s while the job is active.
- **History & Analytics:** Keeps track of your past jobs, displaying success rates, average execution times, and wall-time usage.
- **Array Job Support:** Expand array jobs to inspect individual task statuses and exit codes.
- **Dependency Trees:** Visually trace job dependencies (`afterok`, `afterany`, etc.) to understand why a job is pending.
- **Quick Actions:** Hold, release, cancel, or resubmit jobs with a single keystroke.

## Requirements

- Python 3.9+
- `textual` (Python library for the TUI)
- Access to a Slurm cluster (`squeue`, `sacct`, `scontrol`, `sstat`)

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/slurm-dashboard.git
   cd slurm-dashboard
   ```

2. Install the required dependencies:
   ```bash
   cd src && sh install.sh && source ~/.bashrc
   ```
   *(Note: Depending on your cluster environment, you might want to install this in a virtual environment or use `pip install --user textual rich`).*

## Usage

Simply run the Python script from your terminal:

```bash
sqdash
```
or
```bash
python src/slurm_dashboard.py
```

### Keyboard Shortcuts

The UI is heavily keyboard-driven. Most panels have a footer indicating available shortcuts, but here are the global ones:

| Key | Action |
| :--- | :--- |
| `1`-`6` | Switch between main tabs (All Jobs, My Jobs, Nodes, History, Stats) |
| `n` | Submit a new job (opens sbatch form) |
| `l` | View logs for the selected job |
| `m` | Open node monitor (CPU/Mem usage) |
| `a` | Expand array job tasks |
| `e` | Show dependency tree for the selected job |
| `r` | Manual refresh |
| `c` | Cancel selected job |
| `b` | Resubmit job |
| `h` / `u` | Hold / Unhold job |
| `Esc` / `q` | Close current modal / Quit application |

Tables adapt to the terminal width: below 140 columns some columns are hidden,
and below 90 columns only the essentials remain. Actions keep working at any
width.

## How it works

The dashboard runs entirely in user-space. It acts as a wrapper around standard Slurm binaries. 
- Live data is fetched using `squeue` and `sinfo`.
- Historical data relies on `sacct`, queried only for jobs the dashboard already tracks.
- Resource monitoring uses `sstat` for running jobs and SSH for raw node metrics.
- Internal state (history caches) is saved to `~/.slurm_dashboard_events.log` and a local JSON cache to keep load times fast without spamming the Slurm controller.

### Local state

| File | Contents |
| :--- | :--- |
| `~/.slurm_dashboard_history.json` | Your tracked jobs, their final states and resolved log paths |
| `~/.slurm_dashboard_events.log` | Rolling event log of observed job state changes |

Both files are created with mode `0600`, since they record job names, working
directories and log paths. The history file only tracks **your own** jobs
(`HISTORY_ONLY_MINE` in the script) — it is capped at 500 entries, so recording
every job on the cluster would evict your own within minutes on a busy system.

## Security notes

- The dashboard never runs a shell on your input: every Slurm command is
  executed as an argument vector, and job ids and node names are validated
  before they are passed to `scancel`, `scontrol`, `sstat` or `ssh`.
- Resubmission (`b`) only accepts a recovered `SubmitLine` that is an actual
  `sbatch` invocation. Anything else is reported rather than executed.
- Node metrics use `ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes`:
  a first-time host is trusted, but a host whose key *changed* is refused.
  Passwordless key auth to the compute nodes is required; no password is read.
- Hold / release / cancel are refused unless the selected job is owned by
  `$USER`. This is a convenience guard — Slurm itself remains the authority.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

[MIT](LICENSE)