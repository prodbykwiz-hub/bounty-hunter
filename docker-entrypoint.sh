#!/bin/sh
# Диск Render монтируется от root: отдаём его пользователю agent и запускаем агента не от root.
set -e

DATA_DIR="${DATA_DIR:-/data}"
export AGENT_STATE_FILE="${AGENT_STATE_FILE:-$DATA_DIR/agent_state.json}"
export WORK_DIR="${WORK_DIR:-$DATA_DIR/work}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$WORK_DIR"
    if [ "$(stat -c %U "$DATA_DIR")" != "agent" ]; then
        chown -R agent:agent "$DATA_DIR"
    fi
    export HOME=/home/agent
    exec setpriv --reuid=agent --regid=agent --init-groups "$@"
fi

mkdir -p "$WORK_DIR"
exec "$@"
