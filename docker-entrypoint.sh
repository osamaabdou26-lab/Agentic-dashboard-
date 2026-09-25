#!/bin/sh
# Seeds a sample analytics store on first boot, same fallback the Streamlit
# deploy already relies on, so `docker run` works with zero configuration.
set -eu

db_path="${SEARCHIQ_DB:-/app/data/searchiq.db}"

if [ ! -f "$db_path" ]; then
    echo "No analytics store at $db_path - generating sample data..."
    searchiq sample-data
fi

exec searchiq serve --host 0.0.0.0 --port "${PORT:-8000}"
