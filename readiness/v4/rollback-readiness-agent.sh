#!/bin/bash
# Undo the AI Readiness Plan Composer install in v4.
#   ./rollback-readiness-agent.sh [appPath]
set -euo pipefail
APP="${1:-/Users/minervaai/minerva-4.0}"
AG="$APP/agents.js"
BAK=""
[ -f "$APP/.readiness-agent-rollback" ] && BAK="$(tr -d '[:space:]' < "$APP/.readiness-agent-rollback")"
[ -n "$BAK" ] && [ -f "$BAK" ] || BAK="$(ls -t "$AG".bak.* 2>/dev/null | head -1 || true)"
[ -n "$BAK" ] && [ -f "$BAK" ] || { echo "No backup found for $AG"; exit 1; }
cp "$AG" "$AG.pre-rollback" 2>/dev/null || true
cp "$BAK" "$AG"
echo "Restored $AG"
echo "  from    $BAK"
echo ""
echo "Now restart v4:  pm2 restart minerva-40"
