#!/usr/bin/env python3
"""CLI for A2A Co-Work (used by the a2a-team skill and humans).

Reads connection info from worker.yaml next to this file (or A2A_WORKER_CONFIG).
Commands: agents | new | get | list | cancel | msg | action | file | deregister | join
(join is standalone and stdlib-only: it redeems an invite code before any worker.yaml exists)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _expand(v):
    if isinstance(v, str):
        return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", lambda m: os.environ.get(m.group(1)) or m.group(2) or "", v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


DIRS = (Path(__file__).parent, Path(__file__).parent.parent / "a2a-worker", Path.home() / "a2a-cowork" / "a2a-worker")


def load():
    import yaml  # lazy import: `join` runs before any venv/pyyaml exists
    env_cfg = os.environ.get("A2A_WORKER_CONFIG", "").strip()
    if env_cfg:
        p = Path(env_cfg)
        if not p.is_file():
            sys.exit(f"$A2A_WORKER_CONFIG is set to {p}, which does not exist")
    else:
        p = next((d / "worker.yaml" for d in DIRS if (d / "worker.yaml").is_file()), None)
    if not p:
        sys.exit("no worker.yaml found (looked in $A2A_WORKER_CONFIG, here, ../a2a-worker, "
                 "and ~/a2a-cowork/a2a-worker); run onboarding per SKILL.md first")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        sys.exit(f"worker.yaml error: {p} must be a YAML mapping")
    cfg = _expand(raw)
    missing = [k for k in ("server_url", "domain", "domain_token") if not cfg.get(k)]
    if missing:
        sys.exit(f"worker.yaml missing keys: {', '.join(missing)} — is this the right config file?")
    return cfg


def call(cfg, method, path, body=None, raw=False, data=None, timeout=60):
    """raw=True sends `data` bytes untouched and returns (bytes, headers): file staging."""
    req = urllib.request.Request(cfg["server_url"] + path, method=method,
                                 data=data if raw else (json.dumps(body).encode() if body is not None else None))
    req.add_header("authorization", f"Bearer {cfg['domain_token']}")
    if agent := (cfg.get("agent") or {}).get("id"):
        req.add_header("x-agent-id", agent)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            got = r.read()
        return (got, r.headers) if raw else (json.loads(got) if got else {})
    except urllib.error.HTTPError:
        raise
    except OSError as e:
        raise urllib.error.URLError(str(e)) from e


def upload_file(cfg, path):
    with open(path, "rb") as f:
        data = f.read()
    body, _ = call(cfg, "POST", f"/domains/{cfg['domain']}/files?name="
                   + urllib.parse.quote(os.path.basename(path)), raw=True, data=data, timeout=300)
    return json.loads(body)


def fetch_file(cfg, fid, out=None):
    data, headers = call(cfg, "GET", f"/domains/{cfg['domain']}/files/{fid}", raw=True, timeout=300)
    dest = out or (n if (n := os.path.basename(headers.get_filename() or "")) not in ("", ".", "..") else fid)
    dest = f"{fid[:8]}_{dest}" if not out and os.path.exists(dest) else dest
    with open(dest, "wb") as f:
        f.write(data)
    return dest


TERMINAL = ("completed", "failed", "canceled")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("agents")
    p = sub.add_parser("new"); p.add_argument("--to", required=True); p.add_argument("--text", required=True); p.add_argument("--timeout", type=int)
    p.add_argument("--file", action="append", help="attach a file (repeatable)")
    p = sub.add_parser("get"); p.add_argument("--task", required=True); p.add_argument("--wait", type=int, default=0, help="seconds to wait for a terminal state")
    p = sub.add_parser("list"); p.add_argument("--status"); p.add_argument("--role", choices=["initiator", "target"])
    p = sub.add_parser("cancel"); p.add_argument("--task", required=True)
    p = sub.add_parser("msg"); p.add_argument("--task", required=True); p.add_argument("--text", required=True)
    p.add_argument("--file", action="append", help="attach a file (repeatable)")
    p2 = sub.add_parser("file"); p2.add_argument("--get", required=True, help="download a staged file by id")
    p2.add_argument("--out", default=None, help="local output path (default: stored name)")
    p = sub.add_parser("action"); p.add_argument("--task", required=True); p.add_argument("--action", required=True, choices=["approve", "reject", "abort"])
    sub.add_parser("deregister", help="leave the team (stop your worker first)")
    p = sub.add_parser("join", help="redeem an invite code and write worker.yaml")
    p.add_argument("--server", required=True); p.add_argument("--domain", required=True); p.add_argument("--code", required=True)
    p.add_argument("--agent-id", required=True); p.add_argument("--owner", required=True)
    p.add_argument("--desc", default=""); p.add_argument("--policy", default="notify_run")
    p.add_argument("--notify-channel", required=True); p.add_argument("--notify-id-type", default="auto"); p.add_argument("--notify-id", required=True)
    p.add_argument("--cmd", dest="driver_cmd", default="claude -p --output-format json --permission-mode acceptEdits")
    p.add_argument("--output", default="last_json", choices=["last_json", "tail"]); p.add_argument("--cwd", default="")
    a = ap.parse_args()
    cfg = {"server_url": a.server.rstrip("/"), "domain_token": ""} if a.cmd == "join" else load()
    d = a.domain if a.cmd == "join" else cfg["domain"]
    try:
        if a.cmd == "join":
            body = {"code": a.code, "agent_id": a.agent_id, "owner_username": a.owner,
                    "description": a.desc, "accept_policy": a.policy,
                    "driver_kind": "manual" if a.driver_cmd == "manual" else "command",
                    "notify": {"channel": a.notify_channel, "id_type": a.notify_id_type, "id": a.notify_id}}
            res = call(cfg, "POST", f"/domains/{d}/join", body)
            env_cfg = os.environ.get("A2A_WORKER_CONFIG", "").strip()
            wp = Path(env_cfg) if env_cfg else next((c for c in DIRS[1:] if c.is_dir()), DIRS[0]) / "worker.yaml"
            q = lambda v: json.dumps(v, ensure_ascii=False)
            drv = "  kind: manual\n  manual_timeout_hours: 24" if a.driver_cmd == "manual" else (
                "  kind: command\n" + (f"  cwd: {q(a.cwd)}\n" if a.cwd else "")
                + f"  cmd: {q(a.driver_cmd)}\n  timeout: 3600\n  output: {a.output}")
            wp.write_text(f"server_url: {q(cfg['server_url'])}\ndomain: {q(d)}\ndomain_token: {q(res['domain_token'])}\n"
                          f"agent:\n  id: {q(a.agent_id)}\n  owner: {q(a.owner)}\n  description: {q(a.desc)}\n"
                          f"  accept_policy: {q(a.policy)}\n  accept_from: all\n"
                          f"notify:\n  channel: {q(a.notify_channel)}\n  id_type: {q(a.notify_id_type)}\n  id: {q(a.notify_id)}\n"
                          f"workspace_dir: ~/.a2a-worker/tasks\nbusy_poll_interval: 5\ndriver:\n{drv}\n", encoding="utf-8")
            print(json.dumps({**res, "config": str(wp)}, ensure_ascii=False))
        elif a.cmd == "agents":
            for x in call(cfg, "GET", f"/domains/{d}/agents"):
                print(f"{'●' if x['online'] else '○'} {x['agent_id']:<24} {x['accept_policy']:<10} {x['driver_kind']:<10} {x['description']}")
        elif a.cmd == "new":
            ids = [upload_file(cfg, f)["id"] for f in (a.file or [])]
            print(json.dumps(call(cfg, "POST", f"/domains/{d}/tasks",
                                  {"target": a.to, "text": a.text,
                                   **({"timeout_seconds": a.timeout} if a.timeout else {}),
                                   **({"files": ids} if ids else {})}), ensure_ascii=False))
        elif a.cmd == "get":
            deadline = time.time() + a.wait
            while True:
                r = call(cfg, "GET", f"/domains/{d}/tasks/{a.task}")
                if r["status"] in TERMINAL or r["status"] == "input-required" or time.time() >= deadline:
                    print(json.dumps(r, ensure_ascii=False, indent=1))
                    return
                time.sleep(2)
        elif a.cmd == "list":
            qs = urllib.parse.urlencode({k: v for k, v in (("status", a.status), ("role", a.role)) if v})
            print(json.dumps(call(cfg, "GET", f"/domains/{d}/tasks" + (f"?{qs}" if qs else "")), ensure_ascii=False, indent=1))
        elif a.cmd == "cancel":
            print(json.dumps(call(cfg, "POST", f"/domains/{d}/tasks/{a.task}/cancel", {}), ensure_ascii=False))
        elif a.cmd == "msg":
            ids = [upload_file(cfg, f)["id"] for f in (a.file or [])]
            print(json.dumps(call(cfg, "POST", f"/domains/{d}/tasks/{a.task}/messages",
                                  {"text": a.text, **({"files": ids} if ids else {})}), ensure_ascii=False))
        elif a.cmd == "file":
            print(fetch_file(cfg, a.get, a.out))
        elif a.cmd == "action":
            print(json.dumps(call(cfg, "POST", f"/domains/{d}/tasks/{a.task}/action", {"action": a.action}), ensure_ascii=False))
        elif a.cmd == "deregister":
            aid = (cfg.get("agent") or {}).get("id") or sys.exit("worker.yaml has no agent.id — nothing to deregister")
            call(cfg, "POST", f"/domains/{d}/agents/{aid}/deregister")
            print("deregistered")
    except urllib.error.HTTPError as e:
        print(e.read().decode() or str(e), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"server unreachable at {cfg['server_url']} ({e})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
