"""A2A worker: outbound long-poll loop + one-shot driver execution.

Platform: Windows / Linux / macOS. Single instance via an exclusively-created
lock file holding the pid. The poll loop NEVER stops while a driver runs in
the background — poll is the heartbeat. `python worker.py report <task_id> ...`
fills in manual-driver results via the loopback HTTP endpoint.
"""
import argparse
import atexit
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import drivers

HERE = Path(__file__).parent
HOME = Path.home() / ".a2a-worker"
VALID_REPORT = ("completed", "failed", "input-required")


def _expand(v):
    if isinstance(v, str):
        return os.path.expandvars(v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def _positive(v):
    """Absent (None) or a positive number. bool is excluded: YAML `yes` is an int."""
    return v is None or (not isinstance(v, bool) and isinstance(v, (int, float)) and v > 0)


def load_cfg():
    p = Path(os.environ.get("A2A_WORKER_CONFIG", HERE / "worker.yaml"))
    if not p.exists():
        sys.exit(f"config not found: {p} (copy worker.example.yaml to worker.yaml)")
    cfg = _expand(yaml.safe_load(p.read_text()))
    missing = [k for k in ("server_url", "domain", "domain_token") if not cfg.get(k)]
    a = cfg.get("agent") or {}
    missing += [f"agent.{k}" for k in ("id", "owner") if not a.get(k)]
    nb = cfg.get("notify") or {}
    missing += [f"notify.{k}" for k in ("channel", "id_type", "id") if not nb.get(k)]
    if missing:
        sys.exit(f"worker.yaml is missing: {', '.join(missing)}")
    dd = cfg.get("default_driver", "command")
    if dd not in (cfg.get("drivers") or {}):
        sys.exit(f"worker.yaml error: default_driver '{dd}' has no entry under drivers:")
    for name, dc in (cfg.get("drivers") or {}).items():
        if not isinstance(dc, dict):
            sys.exit(f"worker.yaml error: drivers.{name} must be a mapping")
        kind = dc.get("kind", "command")
        if kind not in ("command", "manual"):
            sys.exit(f"worker.yaml error: drivers.{name}.kind must be command|manual, got {kind!r}")
        if kind == "command" and not (isinstance(dc.get("cmd"), str) and dc["cmd"].strip()):
            sys.exit(f"worker.yaml error: drivers.{name} needs a cmd string")
        if dc.get("output") not in (None, "last_json", "tail"):
            sys.exit(f"worker.yaml error: drivers.{name}.output must be last_json or tail, got {dc.get('output')!r}")
        for num in ("timeout", "manual_timeout_hours"):
            if not _positive(dc.get(num)):
                sys.exit(f"worker.yaml error: drivers.{name}.{num} must be a positive number, got {dc.get(num)!r}")
    for num in ("busy_poll_interval", "local_port"):
        if not _positive(cfg.get(num)):
            sys.exit(f"worker.yaml error: {num} must be a positive number, got {cfg.get(num)!r}")
    if "${" in str(cfg.get("domain_token", "")):
        sys.exit("worker.yaml error: domain_token still contains unexpanded ${ENV} — variable unset?")
    return cfg


def api(cfg, method, path, body=None, timeout=65):
    """Never raises on transport/protocol errors — returns (0, {...}) so the
    loop can back off instead of crashing and orphaning a running driver."""
    req = urllib.request.Request(cfg["server_url"] + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("authorization", f"Bearer {cfg['domain_token']}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return r.status, (json.loads(data) if data.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw.strip() else {})
        except json.JSONDecodeError:
            return e.code, {"error": "non-json response"}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return 0, {"error": str(e)}


def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        # os.kill(pid, 0) TERMINATES the process on Windows — probe via API instead
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:  # exists, just not ours — alive
        return True
    except OSError:
        return False


def worker_lock(cfg):
    return HOME / f"{cfg['agent']['id']}.lock"  # per-agent: several agents may share one machine


def local_port(cfg):
    if cfg.get("local_port"):
        return int(cfg["local_port"])
    # stable per-agent default (sha256, NOT hash() — that is randomized per process)
    import hashlib
    return 7000 + int(hashlib.sha256(cfg["agent"]["id"].encode()).hexdigest()[:4], 16) % 1000


def acquire_lock(cfg):
    HOME.mkdir(exist_ok=True)
    lock = worker_lock(cfg)
    try:  # atomic claim: O_EXCL, no check-then-write race
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return
    except FileExistsError:
        pass
    old = lock.read_text().strip()
    if old and pid_alive(old):
        sys.exit(f"another worker for agent {cfg['agent']['id']} is running (pid {old}); stop it first: kill {old}")
    lock.unlink(missing_ok=True)  # stale (another starter may have removed it first)
    acquire_lock(cfg)


def register(cfg):
    a = cfg["agent"]
    driver_cfg = cfg.get("drivers", {}).get(cfg.get("default_driver", "command"), {})
    payload = {
        "agent_id": a["id"], "owner_username": a["owner"], "description": a.get("description", ""),
        "accept_policy": a.get("accept_policy", "notify_run"), "accept_from": a.get("accept_from", "all"),
        "default_driver": cfg.get("default_driver", "command"),
        "default_driver_kind": driver_cfg.get("kind", "command"),  # server detects manual by kind

        "notify": {"channel": cfg["notify"]["channel"], "id_type": cfg["notify"]["id_type"], "id": cfg["notify"]["id"]}}
    while True:  # server may still be booting/upgrading; keep the worker alive
        s, r = api(cfg, "POST", f"/domains/{cfg['domain']}/agents/register", payload)
        if s == 200:
            break
        if s == 0 or s >= 500:  # transport error or server-side trouble: transient, retry
            print(f"[worker] register not accepted ({s} {r}), retrying in 5s", flush=True)
            time.sleep(5)
        else:  # 4xx: our config is wrong, retrying won't help
            sys.exit(f"register rejected: {s} {r}")
    if not r.get("notify_verified"):
        print("[worker] WARNING: notify channel not verified, check notify.id in worker.yaml", flush=True)
    print(f"[worker] registered as {a['id']} in domain {cfg['domain']}", flush=True)


class Runner:
    """One in-flight task: driver thread + result + reporting state."""

    def __init__(self, cfg, task, holder):
        holder[0] = self  # visible to the signal handler BEFORE the driver thread starts
        self.cfg, self.task = cfg, task
        self.lease = task["lease_id"]
        self.result = None
        self.reported = False
        self.drop = False
        self.cancel_event = threading.Event()
        self.finished = threading.Event()  # set when _run exits (driver cleaned up)
        self.driver_cfg = cfg.get("drivers", {}).get(cfg.get("default_driver", "command"), {})
        self.manual = self.driver_cfg.get("kind") == "manual"
        if self.manual:
            self.expected = int(float(self.driver_cfg.get("manual_timeout_hours", 24)) * 3600)
        else:
            self.expected = min(task.get("timeout_seconds") or 3600, self.driver_cfg.get("timeout") or 3600)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            if self.manual:  # wait for /report or timeout; no process is spawned
                if not self.cancel_event.wait(self.expected) and self.result is None:
                    self.result = {"status": "failed", "fail_reason": "manual_expired",
                                   "output": "no manual report before timeout"}
                return
            try:
                res = drivers.run_command(self.driver_cfg, self.task,
                                          os.path.expanduser(self.cfg.get("workspace_dir", "~/.a2a-worker/tasks")),
                                          self.driver_cfg.get("need_input_marker", self.cfg.get("need_input_marker", r"^NEED_INPUT:")),
                                          self.cancel_event, self.expected)
            except Exception as e:
                res = {"status": "failed", "fail_reason": "driver_error", "output": repr(e)}
            if self.result is None:  # cancel() may have set the result while the driver ran
                self.result = res
        finally:
            self.finished.set()

    def cancel(self):
        self.cancel_event.set()  # kills the driver subprocess promptly
        if self.result is None:
            self.result = {"status": "failed", "fail_reason": "canceled_by_owner",
                           "output": "canceled by owner"}
            self.drop = True  # server is already terminal; reporting would only make late_result noise

    def exec_state(self):
        # stays populated until the result is ACCEPTED by the server, or the
        # server's restart-detection would fail the task in between
        return {"task_id": self.task["id"], "lease_id": self.lease, "phase": "running",
                "expected_seconds": self.expected}


def loop(cfg, holder):
    cur = None
    backoff = 1
    base = f"/domains/{cfg['domain']}/agents/{cfg['agent']['id']}"
    while True:
        hold = cur is not None and not cur.reported
        st = {"block": cur is None, "exec_state": {"tasks": [cur.exec_state()] if hold else []}}
        s, r = api(cfg, "POST", f"{base}/poll", st)
        if s == 200 and "control" in r:
            if cur and cur.task["id"] in (r["control"].get("cancel") or []):
                print(f"[worker] canceling {cur.task['id']}", flush=True)
                cur.cancel()  # the drop block below waits for the driver to die
        elif s == 200 and "task" in r:
            if cur is not None:  # server already failed/finished our held task (e.g. timeout_stale)
                print(f"[worker] task {cur.task['id']} was failed server-side; dropping it", flush=True)
                cur.cancel()  # kill the driver, mark dropped (never report: server is terminal)
                cur.finished.wait(3)  # serial slot freed only after the driver is really dead
                cur.reported = True
            print(f"[worker] task {r['task']['id']} dispatched", flush=True)
            cur = Runner(cfg, r["task"], holder)
        elif s == 410:
            sys.exit("[worker] deregistered from the server (owner action?); stopping. "
                     "Restart manually to rejoin.")  # never silently resurrect
        elif s == 404:
            register(cfg)  # server lost our agent row (e.g. DB reset) — re-register
        elif s == 401:
            sys.exit(f"[worker] 401 from server: domain token wrong in worker.yaml ({r.get('error')})")
        elif s != 204:
            print(f"[worker] poll not ok ({s} {r}), backing off", flush=True)
            time.sleep(min(backoff, 30))  # transport trouble: back off, keep polling
            backoff = min(backoff * 2, 30)
        if cur and (cur.drop or cur.result is not None) and not cur.reported:
            if cur.drop:
                cur.finished.wait(3)
                cur.reported = True
                print(f"[worker] task {cur.task['id']} canceled, dropping", flush=True)
            else:
                res = {k: v for k, v in cur.result.items() if v is not None}
                s2, r2 = api(cfg, "POST", f"{base}/results", {"task_id": cur.task["id"], "lease_id": cur.lease, **res})
                if s2 == 200:
                    cur.reported = True
                    print(f"[worker] task {cur.task['id']} -> {cur.result['status']}"
                          + ("" if r2.get("accepted", True) else " (late_result, dropped)"), flush=True)
                else:
                    # short backoff only: the loop must keep polling (heartbeat)
                    time.sleep(min(backoff, cfg.get("busy_poll_interval", 5)))
                    backoff = min(backoff * 2, 30)
            if cur.reported:
                cur = None
                holder[0] = None
                backoff = 1
        if hold and cur is not None and cur.result is None:
            time.sleep(cfg.get("busy_poll_interval", 5))


class ReportHandler(BaseHTTPRequestHandler):
    holder = None  # [Runner|None], injected at startup

    def do_POST(self):
        if self.path != "/report":
            self.send_error(404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return
        cur = self.holder[0] if self.holder else None
        # only the in-flight MANUAL task accepts a human report — anything else
        # would forge a result and bypass the anti-fake-success chain
        ok = (cur and cur.manual and cur.task["id"] == body.get("task_id")
              and cur.result is None and body.get("status") in VALID_REPORT
              and (body.get("status") != "completed" or str(body.get("output", "")).strip())
              and (body.get("status") != "input-required" or str(body.get("question") or "").strip()))
        if not ok:
            status, msg = 409, {"error": "no matching in-flight manual task"}
        else:
            cur.result = {"status": body["status"], "output": body.get("output", ""),
                          "question": body.get("question"), "fail_reason": body.get("fail_reason")}
            status, msg = 200, {"ok": True}
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(msg).encode())

    def log_message(self, *_):
        pass


def main():
    ap = argparse.ArgumentParser(prog="a2a-worker")
    sub = ap.add_subparsers(dest="cmd")
    rep = sub.add_parser("report", help="fill in the result of a manual-driver task")
    rep.add_argument("task_id")
    rep.add_argument("--status", required=True, choices=VALID_REPORT)
    rep.add_argument("--output", default="")
    rep.add_argument("--question", default=None)
    rep.add_argument("--fail-reason", default=None)
    args = ap.parse_args()
    cfg = load_cfg()
    if args.cmd == "report":
        req = urllib.request.Request(f"http://127.0.0.1:{local_port(cfg)}/report",
                                     data=json.dumps({"task_id": args.task_id, "status": args.status,
                                                      "output": args.output, "question": args.question,
                                                      "fail_reason": args.fail_reason}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                print(r.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            body = e.read().decode() if hasattr(e, "read") else str(e)
            print(body)
            sys.exit(1)
        return
    acquire_lock(cfg)

    def kill_current_driver():
        cur = ReportHandler.holder[0] if ReportHandler.holder else None
        if cur:
            cur.cancel()  # ask the driver thread to kill the process group...
            cur.finished.wait(3)  # ...and WAIT: process exit would slaughter daemon threads mid-kill

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # cleanup rides on atexit
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    atexit.register(lambda: worker_lock(cfg).unlink(missing_ok=True))
    atexit.register(kill_current_driver)  # LIFO: registered LAST, runs FIRST (kill before unlock)
    holder = [None]
    ReportHandler.holder = holder
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", local_port(cfg)), ReportHandler)
    except OSError as e:
        sys.exit(f"[worker] local port {local_port(cfg)} unavailable ({e}); "
                 f"another worker here? set local_port in worker.yaml")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[worker] report endpoint on 127.0.0.1:{local_port(cfg)}", flush=True)
    # No generic crash-restart loop: an unexpected exception is a bug, and masking it
    # with silent restarts hides it. atexit kills the driver; the server fails the task
    # as worker_offline and notifies both owners — failure stays visible.
    register(cfg)
    loop(cfg, holder)


if __name__ == "__main__":
    main()
