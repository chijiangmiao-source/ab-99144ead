"""束流保护裁决服务入口。"""

from __future__ import annotations

import os
import signal
import time

from .server import Server
from .statemachine import DrillStore


def main() -> None:
    db_path = os.environ.get("DB_PATH", "/data/beamprotect.db")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    store = DrillStore(db_path)
    store.recover()
    print(f"[main] recovered; listening on {host}:{port} (db={db_path})",
          flush=True)

    server = Server((host, port), store)

    def _shutdown(*_):
        print("[main] shutting down", flush=True)
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.serve_forever()
    server.server_close()
    time.sleep(0.2)  # 让 WAL 在关闭前落盘


if __name__ == "__main__":
    main()
