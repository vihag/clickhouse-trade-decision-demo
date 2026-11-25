"""Point .env at the local docker-compose stack (keeps keys you already set). Prints names, never values.
Picks free host ports for the containers on first run (5433 / 8123 unless taken) and remembers them in .env."""
import socket, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile


def free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # ignore TIME_WAIT leftovers; only a live listener counts
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick(key: str, default: int, cur: dict) -> str:
    if cur.get(key):
        return cur[key]                      # already chosen on an earlier run: keep it (idempotent)
    for p in (default, *range(default + 10000, default + 10050)):
        if p < 65536 and free(p):
            if p != default:
                print(f"port {default} is in use on this machine; using {p} for {key}")
            return str(p)
    sys.exit(f"no free port near {default} for {key}")


mode = (sys.argv[1:] or ["local"])[0]
if mode == "app-port":                    # run after `make stop`: keep APP_PORT if set, else 8000 or the next free one
    envfile.ensure()
    port = pick("APP_PORT", 8000, envfile.read())
    envfile.set_many({"APP_PORT": port})
    sys.exit(0)
if mode != "local":
    sys.exit(f"unknown mode {mode!r} (use: local | app-port)")
created = envfile.ensure()
cur = envfile.read()
pg_port, ch_port = pick("LOCAL_PG_PORT", 5433, cur), pick("LOCAL_CH_PORT", 8123, cur)
envfile.set_many({
    "LOCAL_PG_PORT": pg_port, "LOCAL_CH_PORT": ch_port,
    "CLICKHOUSE_HOST": "127.0.0.1", "CLICKHOUSE_PORT": ch_port, "CLICKHOUSE_SECURE": "false",
    "CLICKHOUSE_USER": "default", "CLICKHOUSE_PASSWORD": "demo",   # the throwaway local container (docker-compose.yml)
    "POSTGRES_MODE": "local",
    "DATABASE_URL_LOCAL": f"postgresql://demo:demo@127.0.0.1:{pg_port}/trade_demo",
})
(envfile.ROOT / ".compose.env").write_text(f"LOCAL_PG_PORT={pg_port}\nLOCAL_CH_PORT={ch_port}\n")  # ports only
print(f".env {'created' if created else 'updated'} for the local stack: ClickHouse 127.0.0.1:{ch_port}, "
      f"Postgres 127.0.0.1:{pg_port}, POSTGRES_MODE=local")
