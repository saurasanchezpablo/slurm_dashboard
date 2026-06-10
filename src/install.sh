#!/usr/bin/env bash
# install.sh — instala dependencias y configura el alias

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Install python dependencies..."
pip install textual rich

echo ""
echo "==> Adding alias to ~/.bashrc y ~/.zshrc (if they exist)..."

ALIAS_LINE="alias sqdash='python3 ${SCRIPT_DIR}/slurm_dashboard.py'"

for rcfile in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$rcfile" ]; then
        if grep -q "sqdash" "$rcfile"; then
            echo "   [$rcfile] skipping"
        else
            echo "" >> "$rcfile"
            echo "# SLURM Dashboard TUI" >> "$rcfile"
            echo "$ALIAS_LINE" >> "$rcfile"
            echo "   [$rcfile] alias added"
        fi
    fi
done

echo ""
echo "✅  Instalation finished."
echo "   reload your shell:  source ~/.bashrc   (or open a new terminal)"
echo "   Launch the dashboard: sqdash"
echo ""
echo "   Shortcuts:"
echo "   1 → All Jobs   2 → My Jobs   3 → Nodes   r → Refresh   q → Salir"