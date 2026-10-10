"""A2A worker: outbound long-poll loop + one-shot driver execution.

Platform: Windows / Linux / macOS. Single instance via an exclusively-created
lock file holding the pid. The poll loop NEVER stops while a driver runs in the
background — poll is the heartbeat. Two loopback subcommands: `python worker.py
report <task_id> ...` fills in manual-driver results, `python worker.py stop`
stops the worker and kills its driver (the graceful stop on every OS).
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
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import drivers

HERE = Path(__file__).parent
HOME = Path.home() / ".a2a-worker"
VALID_REPORT = ("completed", "failed", "input-required")
CURRENT = None  # the in-flight Runner; shared with the signal path and manual reports
LOCK_FILE = None  # set in main; the /stop hard-exit cleans it (atexit does not run there)


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


NEED_INPUT_MARKER = r"^NEED_INPUT:"  # driver prints this line LAST to ask one question


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
    dd = cfg.get("driver")
    if not isinstance(dd, dict):
        sys.exit("worker.yaml error: expected a `driver:` block (kind, cmd, timeout, output)")
    kind = dd.get("kind", "command")
    if kind not in ("command", "manual"):
        sys.exit(f"worker.yaml error: driver.kind must be command|manual, got {kind!r}")
    if kind == "command" and not (isinstance(dd.get("cmd"), str) and dd["cmd"].strip()):
        sys.exit("worker.yaml error: driver needs a cmd string")
    if dd.get("output") not in (None, "last_json", "tail"):
        sys.exit(f"worker.yaml error: driver.output must be last_json or tail, got {dd.get('output')!r}")
    for num in ("timeout", "manual_timeout_hours"):
        if not _positive(dd.get(num)):
            sys.exit(f"worker.yaml error: driver.{num} must be a positive number, got {dd.get(num)!r}")
    for num in ("busy_poll_interval", "local_port"):
        if not _positive(cfg.get(num)):
            sys.exit(f"worker.yaml error: {num} must be a positive number, got {cfg.get(num)!r}")
    if "${" in str(cfg.get("domain_token", "")):
        sys.exit("worker.yaml error: domain_token still contains unexpanded ${ENV} — variable unset?")
    return cfg


def api(cfg, method, path, body=None, raw=False, data=None, timeout=65):
    """Never raises on transport/protocol errors — returns (0, ...) so the loop
    can back off instead of crashing and orphaning a running driver. raw=True
    sends `data` bytes untouched and returns (status, bytes): file staging."""
    payload = data if raw else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(cfg["server_url"] + path, method=method, data=payload)
    req.add_header("authorization", f"Bearer {cfg['domain_token']}")
    req.add_header("x-agent-id", cfg["agent"]["id"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            got = r.read()
        return r.status, got if raw else (json.loads(got) if got.strip() else {})
    except urllib.error.HTTPError as e:
        got = e.read()
        if raw:
            return e.code, got
        try:
            return e.code, (json.loads(got) if got.strip() else {})
        except json.JSONDecodeError:
            return e.code, {"error": "non-json response"}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return 0, str(e).encode() if raw else {"error": str(e)}


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


def loopback_post(cfg, path, payload):
    """One loopback subcommand call (report / stop); exits 1 when the worker
    is not running or refuses."""
    req = urllib.request.Request(f"http://127.0.0.1:{local_port(cfg)}{path}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(e.read().decode() if hasattr(e, "read") else str(e))
        sys.exit(1)


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
        sys.exit(f"another worker for agent {cfg['agent']['id']} is running (pid {old}); "
                 f"stop it first: python worker.py stop")
    lock.unlink(missing_ok=True)  # stale (another starter may have removed it first)
    acquire_lock(cfg)


def register(cfg):
    a = cfg["agent"]
    payload = {
        "agent_id": a["id"], "owner_username": a["owner"], "description": a.get("description", ""),
        "accept_policy": a.get("accept_policy", "notify_run"), "accept_from": a.get("accept_from", "all"),
        "driver_kind": (cfg.get("driver") or {}).get("kind", "command"),  # server notifies manual dispatch by kind
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

    def __init__(self, cfg, task):
        global CURRENT
        CURRENT = self  # visible to the signal handler BEFORE the driver thread starts
        self.cfg, self.task = cfg, task
        self.ws_root = os.path.expanduser(cfg.get("workspace_dir", "~/.a2a-worker/tasks"))
        self.lease = task["lease_id"]
        self.result = None
        self.reported = False
        self.drop = False
        self.uploaded = {}   # outbox name -> staged file id; retries upload only what never landed
        self.cancel_event = threading.Event()
        self.finished = threading.Event()  # set when _run exits (driver cleaned up)
        self.driver_cfg = cfg.get("driver") or {}
        self.manual = self.driver_cfg.get("kind") == "manual"
        if self.manual:
            self.expected = int(float(self.driver_cfg.get("manual_timeout_hours", 24)) * 3600)
        else:
            self.expected = min(task.get("timeout_seconds") or 3600, self.driver_cfg.get("timeout") or 3600)
        threading.Thread(target=self._run, daemon=True).start()

    def _ws(self):
        return os.path.abspath(os.path.join(self.ws_root, self.task["id"]))

    def _fetch_files(self):
        # materialize attached input files next to task.txt: <ws>/files/<name>
        # (the server already sanitized the name at upload; the sha is verified
        # so a corrupted staging write can never pose as the sent file)
        import hashlib
        fdir = os.path.join(self._ws(), "files")
        os.makedirs(fdir, exist_ok=True)
        for f in self.task.get("files") or []:
            s2, data = api(self.cfg, "GET", f"/domains/{self.cfg['domain']}/files/{f['id']}", raw=True, timeout=300)
            if s2 != 200:
                raise RuntimeError(f"fetch {f['name']}: HTTP {s2}")
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise RuntimeError(f"sha256 mismatch on {f['name']}")
            with open(os.path.join(fdir, f["name"]), "wb") as fh:
                fh.write(data)

    def collect_outbox(self):
        """Upload driver-produced files from <ws>/out/, ONE per call so the poll
        heartbeat never goes quiet for more than a single request. True = a file
        landed (poll again, next file next tick); False = transient trouble
        (back off); a list = every file is staged, ready to report. Per-name ids
        are cached so a retry uploads only what never landed. A permanent
        rejection (e.g. 413 over cap) raises: the task must fail with the real
        reason, not spin until timeout_stale."""
        out = os.path.join(self._ws(), "out")
        for nm in sorted(os.listdir(out)) if os.path.isdir(out) else []:
            fp = os.path.join(out, nm)
            if not os.path.isfile(fp) or nm in self.uploaded:
                continue
            with open(fp, "rb") as fh:
                s2, body = api(self.cfg, "POST",
                               f"/domains/{self.cfg['domain']}/files?name={urllib.parse.quote(nm)}",
                               raw=True, data=fh.read(), timeout=60)
            if s2 != 200:
                print(f"[worker] outbox upload {nm} -> HTTP {s2}", flush=True)
                if 400 <= s2 < 500:
                    raise RuntimeError(f"outbox upload rejected: HTTP {s2} for {nm}")
                return False
            self.uploaded[nm] = json.loads(body)["id"]
            return True
        return list(self.uploaded.values())

    def _run(self):
        try:
            if self.task.get("files"):
                try:
                    self._fetch_files()
                except Exception as e:
                    self.result = {"status": "failed", "fail_reason": "driver_error",
                                   "output": f"input file fetch failed: {e}"}
                    return
            if self.manual:  # wait for /report or timeout; no process is spawned
                if not self.cancel_event.wait(self.expected) and self.result is None:
                    self.result = {"status": "failed", "fail_reason": "manual_expired",
                                   "output": "no manual report before timeout"}
                return
            try:
                res = drivers.run_command(self.driver_cfg, self.task, self._ws(),
                                          NEED_INPUT_MARKER,
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


def loop(cfg):
    global CURRENT
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
            cur = Runner(cfg, r["task"])
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
                try:
                    fids = cur.collect_outbox()
                except RuntimeError as e:  # permanent rejection: report failure with the real reason
                    cur.result = {"status": "failed", "fail_reason": "driver_error",
                                  "output": f"{e}; driver output tail: {str(cur.result.get('output', ''))[-1000:]}"}
                    fids = list(cur.uploaded.values())
                if fids is False:  # upload trouble: retry next tick, heartbeat keeps running
                    time.sleep(min(backoff, cfg.get("busy_poll_interval", 5)))
                    backoff = min(backoff * 2, 30)
                elif fids is not True:
                    res = {k: v for k, v in cur.result.items() if v is not None}
                    s2, r2 = api(cfg, "POST", f"{base}/results",
                                 {"task_id": cur.task["id"], "lease_id": cur.lease, **res,
                                  **({"files": fids} if fids else {})})
                    if s2 == 200:
                        cur.reported = True
                        print(f"[worker] task {cur.task['id']} -> {cur.result['status']}"
                              + ("" if r2.get("accepted", True) else " (late_result, dropped)"), flush=True)
                    elif 400 <= s2 < 500:
                        cur.reported = True  # a 4xx is permanent: retrying would wedge the slot forever
                        print(f"[worker] result rejected ({s2} {r2}); dropping {cur.task['id']}", flush=True)
                    else:
                        # short backoff only: the loop must keep polling (heartbeat)
                        time.sleep(min(backoff, cfg.get("busy_poll_interval", 5)))
                        backoff = min(backoff * 2, 30)
            if cur.reported:
                cur = None
                CURRENT = None
                backoff = 1
        if hold and cur is not None and cur.result is None:
            time.sleep(cfg.get("busy_poll_interval", 5))


def kill_current_driver():
    cur = CURRENT
    if cur:
        cur.cancel()  # ask the driver thread to kill the process group...
        cur.finished.wait(3)  # ...and WAIT: process exit would slaughter daemon threads mid-kill


class ReportHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/stop":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            self.wfile.flush()
            # the one graceful stop on every OS: taskkill /F skips atexit and would
            # orphan the driver tree on Windows. Cleanup happens HERE, then a hard exit.
            kill_current_driver()
            LOCK_FILE.unlink(missing_ok=True)
            os._exit(0)
        if self.path != "/report":
            self.send_error(404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return
        cur = CURRENT
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
            cur.cancel_event.set()  # wake the parked manual wait in _run; it must not linger for hours
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
    sub.add_parser("stop", help="stop the running worker for this agent (kills its driver too)")
    args = ap.parse_args()
    cfg = load_cfg()
    if args.cmd == "report":
        loopback_post(cfg, "/report", {"task_id": args.task_id, "status": args.status,
                                       "output": args.output, "question": args.question,
                                       "fail_reason": args.fail_reason})
        return
    if args.cmd == "stop":
        loopback_post(cfg, "/stop", {})
        return
    global LOCK_FILE
    acquire_lock(cfg)
    LOCK_FILE = worker_lock(cfg)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # cleanup rides on atexit
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))
    atexit.register(kill_current_driver)  # LIFO: registered LAST, runs FIRST (kill before unlock)
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
    loop(cfg)


if __name__ == "__main__":
    main()
