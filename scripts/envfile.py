"""Tiny .env editor used by the installer and the cloud provisioner.
Values are written, never printed. The file is kept at mode 600."""
import os, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def ensure() -> bool:
    """Create .env from .env.example if missing. Returns True if it was created."""
    if ENV.exists():
        return False
    ENV.write_text(EXAMPLE.read_text())
    os.chmod(ENV, 0o600)
    return True


def read() -> dict:
    out = {}
    if not ENV.exists():
        return out
    for line in ENV.read_text().splitlines():
        m = _LINE.match(line)
        if m:
            v = m.group(2).strip()
            if v and v[0] in "\"'" and v[-1:] == v[0]:
                v = v[1:-1]
            elif " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            out[m.group(1)] = v
    return out


def get(key: str, default: str = "") -> str:
    return (os.environ.get(key) or read().get(key) or default).strip()


def set_many(values: dict) -> None:
    """Set KEY=value lines in .env (replacing an existing or commented-out line, else appending)."""
    ensure()
    lines = ENV.read_text().splitlines()
    todo = dict(values)
    for i, line in enumerate(lines):
        m = _LINE.match(line.lstrip("# ").rstrip()) if line.lstrip().startswith("#") else _LINE.match(line)
        if m and m.group(1) in todo:
            if line.lstrip().startswith("#") and not line.lstrip("# ").startswith(m.group(1) + "="):
                continue
            lines[i] = f"{m.group(1)}={todo.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in todo.items()]
    ENV.write_text("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)
    for k, v in values.items():
        os.environ[k] = str(v)
