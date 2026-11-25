"""ClickHouse Cloud provisioner for the demo (OpenAPI: https://api.clickhouse.cloud/v1).

Needs only CLICKHOUSE_CLOUD_API_KEY + CLICKHOUSE_CLOUD_API_SECRET in .env (an Admin key; see README).
Everything else is discovered or created, and written back to .env / .pg_conn (mode 600). Idempotent:
re-running reuses what already exists. Secrets are never printed.

  python scripts/cloud.py check        validate the key, show what would be created/reused
  python scripts/cloud.py up           ClickHouse service + Managed Postgres (writes .env, .pg_conn)
  python scripts/cloud.py pipe         ClickPipe Postgres CDC trade_demo -> trade_demo_cdc (after bootstrap)
  python scripts/cloud.py down [--all] delete the ClickPipe + Managed Postgres (--all: also the ClickHouse service)
  add --dry-run to print the API calls (secrets redacted) without changing anything.
"""
import argparse, json, os, sys, time, urllib.parse
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402

API = os.environ.get("CLICKHOUSE_CLOUD_API_URL", "https://api.clickhouse.cloud").rstrip("/")
PG_CONN = envfile.ROOT / ".pg_conn"
PG_TABLES = ["demo_runs", "account_policies", "trade_requests", "recommendations", "decisions", "audit_events"]
PIPE_NAME = "trade_demo_pg_cdc"
PUBLICATION = "trade_demo_cdc_pub"
SECRET_KEYS = {"password", "caCertificate", "connectionString"}
DRY = False


def say(msg):
    print(msg, flush=True)


def die(msg):
    say(f"\nCLOUD: ERROR - {msg}")
    sys.exit(1)


def redact(o):
    if isinstance(o, dict):
        return {k: ("***" if k in SECRET_KEYS and v else redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    return o


class Api:
    def __init__(self):
        key, sec = envfile.get("CLICKHOUSE_CLOUD_API_KEY"), envfile.get("CLICKHOUSE_CLOUD_API_SECRET")
        if not (key and sec):
            die("CLICKHOUSE_CLOUD_API_KEY and CLICKHOUSE_CLOUD_API_SECRET must be set in .env "
                "(ClickHouse Cloud console -> Organization -> API keys -> New API key, role Admin).")
        self.h = httpx.Client(base_url=API, auth=(key, sec), timeout=60)

    def call(self, method, path, body=None, accept_json=True, mutate=False):
        if DRY and mutate:
            say(f"  [dry-run] {method} {path}" + (f"\n            {json.dumps(redact(body))}" if body else ""))
            return None
        for attempt in range(6):
            r = self.h.request(method, path, json=body,
                               headers=None if accept_json else {"Accept": "application/x-pem-file"})
            if r.status_code == 429:  # 10 requests / 10 s per key
                time.sleep(2 + attempt * 2)
                continue
            break
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or r.text[:300]
            except Exception:
                err = r.text[:300]
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code}: {err}")
        return r.json().get("result") if accept_json else r.text


def org_id(api):
    oid = envfile.get("CH_CONSOLE_ORG_ID")
    if oid:
        return oid
    orgs = api.call("GET", "/v1/organizations") or []
    if len(orgs) != 1:
        die(f"the API key sees {len(orgs)} organizations; set CH_CONSOLE_ORG_ID in .env to pick one")
    oid = orgs[0]["id"]
    if not DRY:
        envfile.set_many({"CH_CONSOLE_ORG_ID": oid})
    say(f"  organization: {orgs[0].get('name')} ({oid})")
    return oid


def wait_state(fetch, good, label, timeout=1200):
    t0, last = time.time(), None
    while True:
        st = (fetch() or {}).get("state")
        if st != last:
            say(f"  {label}: {st}")
            last = st
        if st in good:
            return
        if st in ("failed", "terminated", "deleting", "unavailable") and time.time() - t0 > 60:
            die(f"{label} is {st}")
        if time.time() - t0 > timeout:
            die(f"{label} not ready after {timeout // 60} min (state {st}); rerun the same command to keep waiting")
        time.sleep(10)


def public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            ip = httpx.get(url, timeout=10).text.strip()
            if ip.count(".") == 3:
                return ip
        except Exception:
            pass
    return None


# ---------------------------------------------------------------- ClickHouse service
def ensure_service(api, oid):
    sid = envfile.get("CH_CONSOLE_SERVICE_ID")
    if sid:
        svc = api.call("GET", f"/v1/organizations/{oid}/services/{sid}")
        say(f"  ClickHouse service: reusing {svc.get('name')} ({sid}), state {svc.get('state')}")
        if svc.get("state") in ("stopped", "idle"):
            api.call("PATCH", f"/v1/organizations/{oid}/services/{sid}/state", {"command": "start"}, mutate=True)
        if not envfile.get("CLICKHOUSE_PASSWORD"):
            die("CH_CONSOLE_SERVICE_ID is set but CLICKHOUSE_PASSWORD is empty. Put that service's default-user "
                "password in .env, or clear CH_CONSOLE_SERVICE_ID to let the installer create a new service.")
    else:
        region = envfile.get("CLICKHOUSE_REGION", "us-east-1")
        ip = envfile.get("CLICKHOUSE_IP_ALLOW") or public_ip()
        allow = [{"source": ip if "/" in ip else f"{ip}/32", "description": "installer machine"}] if ip else \
                [{"source": "0.0.0.0/0", "description": "anywhere (public IP not detected)"}]
        body = {"name": envfile.get("CLICKHOUSE_SERVICE_NAME", "trade-decision-demo"), "provider": "aws",
                "region": region, "ipAccessList": allow, "idleScaling": True, "idleTimeoutMinutes": 15}
        say(f"  ClickHouse service: creating '{body['name']}' in aws/{region} (IP allow: {allow[0]['source']})")
        res = api.call("POST", f"/v1/organizations/{oid}/services", body, mutate=True)
        if res is None:
            return None
        svc, sid = res["service"], res["service"]["id"]
        # the default user's password is returned once, here: store it, never print it
        envfile.set_many({"CH_CONSOLE_SERVICE_ID": sid, "CLICKHOUSE_PASSWORD": res["password"], "CLICKHOUSE_USER": "default"})
    if DRY:
        return sid
    wait_state(lambda: api.call("GET", f"/v1/organizations/{oid}/services/{sid}"), {"running"}, "ClickHouse service")
    svc = api.call("GET", f"/v1/organizations/{oid}/services/{sid}")
    https = next((e for e in svc.get("endpoints") or [] if e.get("protocol") == "https"), None)
    if not https:
        die("service has no HTTPS endpoint")
    envfile.set_many({"CLICKHOUSE_HOST": https["host"], "CLICKHOUSE_PORT": str(int(https.get("port") or 8443)),
                      "CLICKHOUSE_SECURE": "true", "CLICKHOUSE_REGION": svc.get("region", "")})
    say(f"  ClickHouse service ready: {https['host']}")
    return sid


# ---------------------------------------------------------------- Managed Postgres
def pg_url_from(res):
    url = res.get("connectionString") or (
        f"postgresql://{urllib.parse.quote(res['username'])}:{urllib.parse.quote(res['password'], safe='')}"
        f"@{res['hostname']}:5432/postgres")
    return url + ("&" if "?" in url else "?") + "sslmode=require" if "sslmode=" not in url else url


def ensure_postgres(api, oid):
    pid = envfile.get("PG_CONSOLE_ID")
    if pid:
        try:
            pg = api.call("GET", f"/v1/organizations/{oid}/postgres/{pid}")
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            pg = None
        if pg and PG_CONN.exists():
            say(f"  Managed Postgres: reusing {pg.get('name')} ({pid}), state {pg.get('state')}")
            if pg.get("state") == "stopped":  # the API has no "start" for Postgres (only restart/promote/switchover)
                die(f"Managed Postgres {pid} is stopped. Start it in the ClickHouse Cloud console (Postgres -> "
                    f"{pg.get('name')} -> Start), then rerun.")
        elif pg:
            die(f"PG_CONSOLE_ID {pid} exists but .pg_conn is missing (its password is only returned at creation). "
                "Run `python scripts/cloud.py down` and rerun, or write its connection string to .pg_conn.")
        else:
            say(f"  Managed Postgres {pid} no longer exists; creating a new one")
            pid = None
    if not pid:
        region = envfile.get("CLICKHOUSE_REGION", "us-east-1")
        body = {"name": envfile.get("PG_SERVICE_NAME", "trade-demo-pg"), "provider": "aws", "region": region,
                "size": envfile.get("PG_SIZE", "r8gd.medium"), "postgresVersion": "17", "haType": "none"}
        say(f"  Managed Postgres: creating '{body['name']}' ({body['size']}) in aws/{region}")
        res = api.call("POST", f"/v1/organizations/{oid}/postgres", body, mutate=True)
        if res is None:
            return None
        pid = res["id"]
        PG_CONN.write_text(pg_url_from(res) + "\n")  # password only returned here: store it, never print it
        os.chmod(PG_CONN, 0o600)
        envfile.set_many({"PG_CONSOLE_ID": pid, "POSTGRES_MODE": "managed", "CLICKPIPE_ID": ""})
    if DRY:
        return pid
    wait_state(lambda: api.call("GET", f"/v1/organizations/{oid}/postgres/{pid}"), {"running"}, "Managed Postgres")
    envfile.set_many({"POSTGRES_MODE": "managed"})
    say("  Managed Postgres ready (connection string saved to .pg_conn)")
    return pid


# ---------------------------------------------------------------- ClickPipe
def ensure_pipe(api, oid):
    sid, pid = envfile.get("CH_CONSOLE_SERVICE_ID"), envfile.get("PG_CONSOLE_ID")
    if not (sid and pid and PG_CONN.exists()):
        die("run `python scripts/cloud.py up` first (needs CH_CONSOLE_SERVICE_ID, PG_CONSOLE_ID and .pg_conn)")
    existing = [p for p in (api.call("GET", f"/v1/organizations/{oid}/services/{sid}/clickpipes") or [])
                if p.get("name") == PIPE_NAME]
    if existing:
        p = existing[0]
        envfile.set_many({"CLICKPIPE_ID": p["id"]}) if not DRY else None
        say(f"  ClickPipe: reusing {PIPE_NAME} ({p['id']}), state {p.get('state')}")
        return p["id"]
    u = urllib.parse.urlparse(PG_CONN.read_text().strip())
    prepare_source(sid)
    ca = api.call("GET", f"/v1/organizations/{oid}/postgres/{pid}/caCertificates", accept_json=False)
    body = {
        "name": PIPE_NAME,
        "source": {"postgres": {
            "type": "postgres", "host": u.hostname, "port": u.port or 5432, "database": (u.path or "/postgres").lstrip("/"),
            "authentication": "basic",
            "credentials": {"username": urllib.parse.unquote(u.username), "password": urllib.parse.unquote(u.password)},
            "caCertificate": ca or None, "disableTls": False, "skipCertVerification": not bool(ca),
            "settings": {"replicationMode": "cdc", "syncIntervalSeconds": 60, "publicationName": PUBLICATION},
            "tableMappings": [{"sourceSchemaName": "trade_demo", "sourceTable": t, "targetTable": f"trade_demo_{t}",
                               "tableEngine": "ReplacingMergeTree"} for t in PG_TABLES],
        }},
        "destination": {"database": "trade_demo_cdc"},
    }
    if not body["source"]["postgres"]["caCertificate"]:
        body["source"]["postgres"].pop("caCertificate")
    say(f"  ClickPipe: creating {PIPE_NAME} (Postgres CDC, schema trade_demo, {len(PG_TABLES)} tables -> trade_demo_cdc)")
    res = api.call("POST", f"/v1/organizations/{oid}/services/{sid}/clickpipes", body, mutate=True)
    if res is None:
        return None
    envfile.set_many({"CLICKPIPE_ID": res["id"]})
    say(f"  ClickPipe created ({res['id']}), state {res.get('state')}. Initial load + CDC start in the background (~1-2 min).")
    return res["id"]


def prepare_source(sid):
    """Destination database + an explicit publication for the six tables (both idempotent)."""
    if DRY:
        say("  [dry-run] CREATE DATABASE IF NOT EXISTS trade_demo_cdc; CREATE PUBLICATION " + PUBLICATION)
        return
    sys.path.insert(0, str(envfile.ROOT))
    from app import config, postgres_store as pg
    config.ch_client().command("CREATE DATABASE IF NOT EXISTS trade_demo_cdc")
    with pg.conn() as c:
        if not c.execute("SELECT 1 FROM pg_publication WHERE pubname = %s", (PUBLICATION,)).fetchone():
            c.execute(f"CREATE PUBLICATION {PUBLICATION} FOR TABLE " + ", ".join(f"trade_demo.{t}" for t in PG_TABLES))


# ---------------------------------------------------------------- teardown
def delete(api, path):
    """DELETE; on 409 (deletion protection on) turn protection off and retry once."""
    try:
        api.call("DELETE", path, mutate=True)
    except RuntimeError as e:
        if "HTTP 409" not in str(e):
            raise
        say("    deletion protection is on; turning it off and retrying")
        api.call("PATCH", path, {"deletionProtection": False}, mutate=True)
        api.call("DELETE", path, mutate=True)


def down(api, oid, everything):
    sid, pid, cpid = envfile.get("CH_CONSOLE_SERVICE_ID"), envfile.get("PG_CONSOLE_ID"), envfile.get("CLICKPIPE_ID")
    if sid and cpid:
        say(f"  deleting ClickPipe {cpid}")
        try:
            api.call("DELETE", f"/v1/organizations/{oid}/services/{sid}/clickpipes/{cpid}", mutate=True)
        except RuntimeError as e:
            say(f"    (skipped: {e})")
    if pid:
        say(f"  deleting Managed Postgres {pid}")
        try:
            delete(api, f"/v1/organizations/{oid}/postgres/{pid}")
        except RuntimeError as e:
            say(f"    (skipped: {e})")
    if everything and sid:
        say(f"  stopping and deleting ClickHouse service {sid} (all its data)")
        try:
            api.call("PATCH", f"/v1/organizations/{oid}/services/{sid}/state", {"command": "stop"}, mutate=True)
            if not DRY:
                wait_state(lambda: api.call("GET", f"/v1/organizations/{oid}/services/{sid}"), {"stopped"},
                           "ClickHouse service", timeout=3600)
            delete(api, f"/v1/organizations/{oid}/services/{sid}")
        except RuntimeError as e:
            say(f"    (skipped: {e})")
    elif sid:
        say(f"  keeping ClickHouse service {sid} (it idles to zero after 15 min; `make cloud-destroy` deletes it)")
    if not DRY:
        PG_CONN.unlink(missing_ok=True)
        clear = {"PG_CONSOLE_ID": "", "CLICKPIPE_ID": ""}
        if everything:
            clear.update({"CH_CONSOLE_SERVICE_ID": "", "CLICKHOUSE_HOST": "", "CLICKHOUSE_PASSWORD": "",
                          "CLICKHOUSE_RO_PASSWORD": ""})
        envfile.set_many(clear)


def main():
    global DRY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["check", "up", "pipe", "down"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="down: also delete the ClickHouse service")
    a = ap.parse_args()
    DRY = a.dry_run or a.cmd == "check"
    envfile.ensure()
    api = Api()
    say(f"==> ClickHouse Cloud {a.cmd}{' (dry run)' if DRY else ''}")
    try:
        oid = org_id(api)
        if a.cmd in ("check", "up"):
            ensure_service(api, oid)
            ensure_postgres(api, oid)
        if a.cmd == "check":
            say("  [dry-run] then: make bootstrap, and POST .../clickpipes (Postgres CDC) once the schema exists")
        if a.cmd == "pipe":
            ensure_pipe(api, oid)
        if a.cmd == "down":
            down(api, oid, a.all)
    except RuntimeError as e:
        die(str(e))
    say(f"CLOUD {a.cmd}: OK")


if __name__ == "__main__":
    main()
