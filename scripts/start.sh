#!/usr/bin/env sh
set -e

export PYTHONPATH="/app:${PYTHONPATH}"

POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

wait_for_db_dns() {
  python - <<'PY'
import os
import socket
import sys
import time

host = os.getenv("POSTGRES_HOST", "db")
port = int(os.getenv("POSTGRES_PORT", "5432"))
deadline = time.time() + 90

while time.time() < deadline:
    try:
        socket.getaddrinfo(host, port)
        print(f"Resolved database host: {host}")
        sys.exit(0)
    except OSError as exc:
        print(f"Waiting for DB DNS ({host}): {exc}")
        time.sleep(2)

print(f"Timed out waiting for DB DNS: {host}")
sys.exit(1)
PY
}

run_migrations_with_retry() {
  attempts=15
  i=1
  while [ "$i" -le "$attempts" ]; do
    if python -m app.db.migrate upgrade; then
      echo "Database migrations complete."
      return 0
    fi
    echo "Migration attempt $i/$attempts failed, retrying..."
    i=$((i + 1))
    sleep 3
  done

  echo "Database migrations failed after $attempts attempts."
  return 1
}

if [ "${DB_AUTO_MIGRATE:-false}" = "true" ]; then
  echo "Waiting for database DNS and running migrations..."
  wait_for_db_dns
  run_migrations_with_retry
else
  echo "Skipping in-app migrations (DB_AUTO_MIGRATE=false)."
fi

echo "Starting Streamlit app..."
exec streamlit run app/main.py --server.address=0.0.0.0 --server.port=8501
