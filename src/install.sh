#!/usr/bin/env bash
# install.sh — install dependencies and set up the `sqdash` alias

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Error: '$PY' not found in \$PATH. Set PYTHON=/path/to/python3 and retry." >&2
    exit 1
fi

echo "==> Installing Python dependencies..."
# --user keeps the install inside $HOME, which is what works on a cluster
# login node where site-packages is read-only. Inside a virtualenv or a conda
# env --user is invalid, so fall back to a plain install there.
if [ -n "${VIRTUAL_ENV:-}" ] || [ -n "${CONDA_PREFIX:-}" ]; then
    "$PY" -m pip install textual rich
else
    "$PY" -m pip install --user textual rich
fi

echo ""
echo "==> Adding alias to ~/.bashrc and ~/.zshrc (if they exist)..."

# Quote both paths: either may contain spaces.
ALIAS_LINE="alias sqdash='\"$PY\" \"${SCRIPT_DIR}/slurm_dashboard.py\"'"

for rcfile in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$rcfile" ]; then
        if grep -q "alias sqdash=" "$rcfile"; then
            echo "   [$rcfile] alias already present, skipping"
        else
            {
                echo ""
                echo "# SLURM Dashboard TUI"
                echo "$ALIAS_LINE"
            } >> "$rcfile"
            echo "   [$rcfile] alias added"
        fi
    fi
done

echo ""
echo "✅  Installation finished."
echo "   Reload your shell:  source ~/.bashrc   (or open a new terminal)"
echo "   Launch the dashboard: sqdash"
echo ""
echo "   Shortcuts:"
echo "   1 → All Jobs   2 → My Jobs   3 → Nodes   r → Refresh   q → Quit"
