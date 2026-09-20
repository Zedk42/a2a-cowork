#!/usr/bin/env python3
"""Zero-dependency CLI for A2A Co-Work (used by the a2a-team skill and humans).

Reads connection info from worker.yaml next to this file (or A2A_WORKER_CONFIG).
Commands: agents | new | get | list | cancel | msg | action | deregister
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml


def load():
    # repo layout: worker.yaml lives in ../a2a-worker/; fall back to this dir
    env_cfg = os.environ.get("A2A_WORKER_CONFIG", "").strip()
    cands = ([Path(env_cfg)] if env_cfg else []) + \
        [Path(__file__).parent / "worker.yaml", Path(__file__).parent.parent / "a2a-worker" / "worker.yaml"]
    p = next((c for c in cands if c.is_file()), None)
    if not p:
        sys.exit("no worker.yaml found (looked in $A2A_WORKER_CONFIG, here, and ../a2a-worker/); "
                 "run onboarding per SKILL.md first")
    def expand(v):
        if isinstance(v, str):
            return os.path.expandvars(v)
        if isinstance(v, dict):
            return {k: expand(x) for k, x in v.items()}
        if isinstance(v, list):
            return [expand(x) for x in v]
        return v
    cfg = expand(yaml.safe_load(p.read_text()))
    missing = [k for k in ("server_url", "domain", "domain_token") if not cfg.get(k)]
    if missing:
        sys.exit(f"worker.yaml missing keys: {', '.join(missing)} — is this the right config file?")
    return cfg


def call(cfg, method, path, body=None, agent=None):
    req = urllib.request.Request(cfg["server_url"] + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("authorization", f"Bearer {cfg['domain_token']}")
    if agent := (agent or cfg.get("agent", {}).get("id")):
        req.add_header("x-agent-id", agent)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        return json.loads(data) if data else {}


TERMINAL = ("completed", "failed", "canceled")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="path to worker.yaml (default: sibling of this file)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("agents")
    p = sub.add_parser("new"); p.add_argument("--to", required=True); p.add_argument("--text", required=True); p.add_argument("--timeout", type=int)
    p = sub.add_parser("get"); p.add_argument("--task", required=True); p.add_argument("--wait", type=int, default=0, help="seconds to wait for a terminal state")
    p = sub.add_parser("list"); p.add_argument("--status"); p.add_argument("--role", choices=["initiator", "target"])
    p = sub.add_parser("cancel"); p.add_argument("--task", required=True)
    p = sub.add_parser("msg"); p.add_argument("--task", required=True); p.add_argument("--text", required=True)
    p = sub.add_parser("action"); p.add_argument("--task", required=True); p.add_argument("--action", required=True, choices=["approve", "reject", "abort"])
    p = sub.add_parser("deregister", help="leave the team (stop your worker first)")
    a = ap.parse_args()
    if a.config:
        os.environ["A2A_WORKER_CONFIG"] = a.config
    cfg = load()
    d = cfg["domain"]
    try:
        if a.cmd == "agents":
            for x in call(cfg, "GET", f"/domains/{d}/agents"):
                print(f"{'●' if x['online'] else '○'} {x['agent_id']:<24} {x['accept_policy']:<10} {x['default_driver']:<10} {x['description']}")
        elif a.cmd == "new":
            print(json.dumps(call(cfg, "POST", f"/domains/{d}/tasks", {"target": a.to, "text": a.text, **({"timeout_seconds": a.timeout} if a.timeout else {})}), ensure_ascii=False))
        elif a.cmd == "get":
            deadline = time.time() + a.wait
            while True:
                r = call(cfg, "GET", f"/domains/{d}/tasks/{a.task}")
                # input-required is actionable for the caller: return there too
                if r["status"] in TERMINAL or r["status"] == "input-required" or time.time() >= deadline:
                    print(json.dumps(r, ensure_ascii=False, indent=1))
                    return
                time.sleep(2)
        elif a.cmd == "list":
            out = []
            for st in (a.status.split(",") if a.status and "," in a.status else [a.status]):
                q = f"?status={st}" if st else ""
                if a.role:
                    q += ("&" if q else "?") + f"role={a.role}"
                out += call(cfg, "GET", f"/domains/{d}/tasks{q}")
            print(json.dumps(out, ensure_ascii=False, indent=1))
        elif a.cmd == "cancel":
            print(json.dumps(call(cfg, "POST", f"/domains/{d}/tasks/{a.task}/cancel", {}), ensure_ascii=False))
        elif a.cmd == "msg":
            print(json.dumps(call(cfg, "POST", f"/domains/{d}/tasks/{a.task}/messages", {"text": a.text}), ensure_ascii=False))
        elif a.cmd == "action":
            print(json.dumps(call(cfg, "POST", f"/domains/{d}/tasks/{a.task}/action", {"action": a.action}), ensure_ascii=False))
        elif a.cmd == "deregister":
            call(cfg, "POST", f"/domains/{d}/agents/{cfg['agent']['id']}/deregister")
            print("deregistered")
    except urllib.error.HTTPError as e:
        print(e.read().decode() or str(e), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"server unreachable at {cfg['server_url']} ({e})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
