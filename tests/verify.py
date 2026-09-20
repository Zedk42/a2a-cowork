#!/usr/bin/env python3
"""End-to-end verification for A2A Co-Work: real server + real worker processes.

  python tests/verify.py        # exit code 0 = all checks passed

Scratch state lives in tests/_scratch (wiped on start, removed again on success,
kept for inspection when a check fails — the worker logs are in there).
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRV, WRK = os.path.join(ROOT, "a2a-server"), os.path.join(ROOT, "a2a-worker")
SCRATCH = os.path.join(ROOT, "tests", "_scratch")
TOKEN = "tok-verify"
PORT = 0
FAILED = []
WORKERS = []


def venv_python(d):
    p = os.path.join(d, ".venv", "bin", "python")
    return p if os.path.exists(p) else sys.executable


def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def process_alive(pidfile):
    """Liveness of a pid the driver itself recorded — precise, unlike pgrep -f."""
    try:
        pid = int(open(pidfile).read().split()[0])
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def call(method, path, body=None, agent=None, token=TOKEN):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}" + path, method=method,
                               data=json.dumps(body).encode() if body is not None else None)
    r.add_header("authorization", f"Bearer {token}")
    if agent:
        r.add_header("x-agent-id", agent)
    try:
        with urllib.request.urlopen(r, timeout=15) as x:
            return x.status, json.loads(x.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode(errors="replace")[:200]}


def status_only(path):
    """Status of a non-JSON endpoint (/admin returns HTML)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}" + path, timeout=10) as x:
            return x.status
    except urllib.error.HTTPError as e:
        return e.code


def sql(q, args=()):
    c = sqlite3.connect(os.path.join(SCRATCH, "verify.db"))
    c.execute(q, args)
    c.commit()
    c.close()


def q1(q, args=()):
    c = sqlite3.connect(os.path.join(SCRATCH, "verify.db"))
    c.row_factory = sqlite3.Row
    row = c.execute(q, args).fetchone()
    c.close()
    return dict(row) if row else None


def wait_status(tid, want, timeout=25):
    want = want if isinstance(want, tuple) else (want,)
    end, last = time.time() + timeout, {}
    while time.time() < end:
        last = call("GET", f"/domains/team-a/tasks/{tid}")[1]
        if last.get("status") in want:
            return last
        time.sleep(0.3)
    return last


def register(aid, owner="u1", policy="auto", accept_from="all"):
    s, r = call("POST", "/domains/team-a/agents/register",
                {"agent_id": aid, "owner_username": owner, "accept_policy": policy,
                 "accept_from": accept_from, "default_driver": f"drv-{aid}", "default_driver_kind": "command",
                 "notify": {"channel": "log", "id_type": "text", "id": f"nid-{owner}"}})
    assert s == 200, (s, r)
    return r


def poll(aid, tasks=None, block=False):
    return call("POST", f"/domains/team-a/agents/{aid}/poll",
                {"block": block, "exec_state": {"tasks": tasks or []}})


def report(aid, tid, lease, status, **extra):
    return call("POST", f"/domains/team-a/agents/{aid}/results",
                {"task_id": tid, "lease_id": lease, "status": status, **extra})


def new_task(target, text, initiator="boss"):
    s, r = call("POST", "/domains/team-a/tasks", {"target": target, "text": text}, agent=initiator)
    assert s == 201, (s, r)
    return r["task_id"]


def events(tid):
    return [e["type"] for e in call("GET", f"/domains/team-a/tasks/{tid}")[1]["events"]]


# ---------- server protocol (mock worker, no driver) ----------

def server_checks():
    register("boss", owner="boss")
    register("w", owner="u1")
    register("w", owner="u1")  # idempotent overwrite
    s, agents = call("GET", "/domains/team-a/agents")
    check("register/idempotent-directory", s == 200 and [a["agent_id"] for a in agents].count("w") == 1)

    # register persisted the session and driver-kind columns
    row = q1("SELECT * FROM agents WHERE agent_id='w'")
    check("register/persists-session-and-kind",
          len(row["session"]) == 32 and row["default_driver_kind"] == "command",
          f"session={row['session']!r} kind={row['default_driver_kind']!r}")

    # registered for the offline-probe section below; public_base is configured
    # WITH a trailing slash on purpose (checked via notify links further down)
    register("w2", owner="u2")

    # e2e auto: available -> dispatched -> working -> completed
    tid = new_task("w", "hello")
    _, d = poll("w")
    lease = d["task"]["lease_id"]
    poll("w", tasks=[{"task_id": tid, "lease_id": lease, "phase": "running", "expected_seconds": 60}])
    check("e2e/working", wait_status(tid, "working")["status"] == "working")
    _, r = report("w", tid, lease, "completed", output="done!")
    det = call("GET", f"/domains/team-a/tasks/{tid}")[1]
    check("e2e/completed+convo", r.get("accepted") and det["status"] == "completed" and det["convo"][-1]["text"] == "done!")
    check("results/duplicate-retry", report("w", tid, lease, "completed", output="done!")[1].get("duplicate") is True)
    check("results/wrong-lease-is-late",
          report("w", tid, "wrong-lease", "completed", output="x")[1] == {"accepted": False, "reason": "late_result"})
    check("results/late-result-recorded", "late_result" in events(tid))

    # dispatched -> working only with the lease that was issued
    tid = new_task("w", "guard")
    _, d = poll("w")
    lease = d["task"]["lease_id"]
    poll("w", tasks=[{"task_id": tid, "lease_id": "bogus", "phase": "running", "expected_seconds": 999999}])
    check("poll/bogus-lease-ignored", q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"] == "dispatched")
    poll("w", tasks=[{"task_id": tid, "lease_id": lease, "phase": "running", "expected_seconds": 60}])
    check("poll/real-lease-applied", q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"] == "working")
    report("w", tid, lease, "completed", output="ok")

    # cancel: initiator cancels, and a held task gets control.cancel
    tid = new_task("w", "to cancel")
    _, d = poll("w")
    lease = d["task"]["lease_id"]
    call("POST", f"/domains/team-a/tasks/{tid}/cancel", {}, agent="boss")
    check("cancel/initiator", q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"] == "canceled")
    ctl = poll("w", tasks=[{"task_id": tid, "lease_id": lease, "phase": "running", "expected_seconds": 60}])[1]
    check("cancel/control-delivered", tid in (ctl.get("control", {}).get("cancel") or []), ctl)

    # input-required -> follow-up on the same task id
    tid = new_task("w", "ask me")
    _, d = poll("w")
    report("w", tid, d["task"]["lease_id"], "input-required", question="which branch?")
    check("input-required/reported", wait_status(tid, "input-required")["status"] == "input-required")
    check("input-required/initiator-notified",
          any(e["type"] == "notify" and "branch" in e["payload"] for e in call("GET", f"/domains/team-a/tasks/{tid}")[1]["events"]))
    call("POST", f"/domains/team-a/tasks/{tid}/messages", {"text": "main"}, agent="boss")
    _, d = poll("w")
    check("input-required/requeued-same-id", d["task"]["id"] == tid and len(d["task"]["convo"]) == 3, d)
    report("w", tid, d["task"]["lease_id"], "completed", output="on main")

    # manual accept policy: approve / reject / expiry
    register("m", owner="u3", policy="manual")
    tid = new_task("m", "needs approval")
    check("manual-policy/pending-approval", wait_status(tid, "pending-approval")["status"] == "pending-approval")
    call("POST", f"/domains/team-a/tasks/{tid}/action", {"action": "approve"}, agent="m")
    check("manual-policy/approve->available", q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"] == "available")
    tid = new_task("m", "reject me")
    call("POST", f"/domains/team-a/tasks/{tid}/action", {"action": "reject"}, agent="m")
    f = wait_status(tid, "failed")
    check("manual-policy/reject->failed", f.get("fail_reason") == "rejected", f)
    tid = new_task("m", "let it expire")
    sql("UPDATE approvals SET expires_at=? WHERE task_id=?", (time.time() - 1, tid))
    f = wait_status(tid, "failed", timeout=15)
    check("manual-policy/approval-expired", f.get("fail_reason") == "approval_expired", f)

    # whitelist rejection notifies the target owner only; the notify text carries
    # a {base}/domains/... link — the trailing-slash public_base must not double it
    register("strict", owner="u4", accept_from=["someone"])
    tid = new_task("strict", "not allowed")
    f = wait_status(tid, "failed")
    links = [e["payload"] for e in call("GET", f"/domains/team-a/tasks/{tid}")[1]["events"]
             if e["type"] == "notify"]
    check("whitelist/source_not_allowed", f.get("fail_reason") == "source_not_allowed" and "notify" in events(tid), f)
    check("public_base/no-double-slash", links and all("//domains" not in p for p in links), links)

    # isolation + input validation at the trust boundary
    check("isolation/wrong-domain-token", call("GET", "/domains/team-b/agents")[0] == 401)
    check("isolation/admin-token", call("GET", "/admin?token=wrong", token="")[0] == 401)
    s, r = call("POST", "/domains/team-a/tasks", [1, 2, 3], agent="boss")
    check("validation/non-dict-body->400", s == 400, (s, r))
    s, r = call("POST", "/domains/team-a/tasks", {"target": "w", "text": 123}, agent="boss")
    check("validation/non-string-text->400", s == 400, (s, r))
    check("validation/non-string-text-not-queued", not q1("SELECT 1 FROM tasks WHERE target='w' AND text='123'"))
    check("validation/role-without-identity->403", call("GET", "/domains/team-a/tasks?role=target")[0] == 403)

    # client-supplied values land in stored state, so they must be type-checked
    tid = new_task("w", "expected guard")
    _, d = poll("w")
    poll("w", tasks=[{"task_id": tid, "lease_id": d["task"]["lease_id"], "phase": "running", "expected_seconds": "3600"}])
    check("poll/non-int-expected-seconds-dropped",
          q1("SELECT expected_seconds FROM tasks WHERE id=?", (tid,))["expected_seconds"] is None)
    check("results/non-string-fail-reason-sanitized",
          report("w", tid, d["task"]["lease_id"], "failed", fail_reason={"why": "bad"})[1].get("accepted")
          and q1("SELECT fail_reason FROM tasks WHERE id=?", (tid,))["fail_reason"] == "driver_error")

    # api answers ISO-8601 everywhere, not epoch floats in half the payload
    det = call("GET", f"/domains/team-a/tasks/{tid}")[1]
    check("api/iso-timestamps", str(det["created_at"]).endswith("Z") and str(det["convo"][0]["at"]).endswith("Z")
          and str(det["events"][0]["created_at"]).endswith("Z"), det["convo"][:1])

    # a deregistered agent's tombstone must answer every poller in the window, not just the first
    register("tomb", owner="u6")
    call("POST", "/domains/team-a/agents/tomb/deregister")
    check("deregister/tombstone-hits-every-poller", poll("tomb")[0] == 410 and poll("tomb")[0] == 410)

    # a self-dispatched task has one owner, so it must produce one notification
    register("solo", owner="u6")
    tid = new_task("solo", "self", initiator="solo")
    call("POST", f"/domains/team-a/tasks/{tid}/cancel", {}, agent="solo")
    n = [e for e in call("GET", f"/domains/team-a/tasks/{tid}")[1]["events"] if e["type"] == "notify"]
    check("notify/self-dispatch-notifies-once", len(n) == 1, n)

    # a NULL last_seen_at (legacy row) must be handled as "offline" everywhere, not crash
    tid = new_task("w2", "offline probe")
    poll("w2")  # claims it; this refreshes last_seen_at
    sql("UPDATE agents SET last_seen_at=NULL WHERE agent_id='w2'")
    s, agents = call("GET", "/domains/team-a/agents")
    check("robust/null-last-seen-no-500", s == 200 and any(a["agent_id"] == "w2" and a["online"] is False for a in agents), (s, agents))
    check("robust/admin-null-last-seen-no-500", status_only("/admin?token=admtok") == 200)
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/admin/data?token=admtok", timeout=10) as x:
        ad = json.loads(x.read())
    check("robust/admin-data-snapshot", any(a["agent_id"] == "w2" and a["online"] is False for a in ad["agents"])
          and isinstance(ad["events"], list), ad.get("agents"))
    f = wait_status(tid, "failed", timeout=15)
    check("robust/null-last-seen-treated-offline", f.get("fail_reason") == "worker_offline", f)
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/admin/task/{tid}?token=admtok", timeout=10) as x:
        at = json.loads(x.read())
    check("robust/admin-task-detail", at.get("id") == tid and at.get("convo") and at.get("events"), at.get("id"))

    # ---------- sweep & ordering mechanisms (FIFO, restart detection, timeouts, retention) ----------

    # FIFO: backlog delivered in available_at order; an IR follow-up requeues at the BACK
    register("fq", owner="u7")
    ta, tb, tc = new_task("fq", "a"), new_task("fq", "b"), new_task("fq", "c")
    _, d = poll("fq")
    check("fifo/first-is-oldest", d["task"]["id"] == ta, d)
    report("fq", ta, d["task"]["lease_id"], "input-required", question="?")
    call("POST", f"/domains/team-a/tasks/{ta}/messages", {"text": "ans"}, agent="boss")
    _, d = poll("fq")
    check("fifo/requeue-goes-to-back", d["task"]["id"] == tb, d)
    report("fq", tb, d["task"]["lease_id"], "completed", output="b done")
    _, d = poll("fq")
    check("fifo/next-in-order", d["task"]["id"] == tc, d)
    report("fq", tc, d["task"]["lease_id"], "completed", output="c done")
    _, d = poll("fq")
    check("fifo/followup-last", d["task"]["id"] == ta, d)
    report("fq", ta, d["task"]["lease_id"], "completed", output="a done")

    # worker_restart: same-session orphan only fails past dispatch_grace (no false positive inside it)
    register("rs", owner="u8")
    tid = new_task("rs", "orphan me")
    poll("rs")
    poll("rs")  # empty exec_state, fresh dispatched_at, same session
    check("restart/within-grace-no-fail", q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"] == "dispatched")
    time.sleep(5)  # > dispatch_grace(4), < 2*grace (receive_timeout threshold)
    poll("rs")
    f = wait_status(tid, "failed", timeout=5)
    check("restart/past-grace-fails", f.get("fail_reason") == "worker_restart", f)
    # foreign session (re-register issued a new one): fails immediately, no grace wait
    tid = new_task("rs", "foreign lease")
    poll("rs")
    register("rs", owner="u8")
    poll("rs")
    f = wait_status(tid, "failed", timeout=5)
    check("restart/foreign-session-immediate", f.get("fail_reason") == "worker_restart", f)

    # receive_timeout: worker online, task in exec_state, but phase never reaches running
    register("rt", owner="u9")
    tid = new_task("rt", "never starts")
    _, d = poll("rt")
    lease = d["task"]["lease_id"]
    end = time.time() + 15
    f = {}
    while time.time() < end:
        f = call("GET", f"/domains/team-a/tasks/{tid}")[1]
        if f["status"] == "failed":
            break
        poll("rt", tasks=[{"task_id": tid, "lease_id": lease, "phase": "received", "expected_seconds": 60}])
        time.sleep(1)
    check("sweep/receive-timeout", f.get("fail_reason") == "receive_timeout", f)

    # timeout_stale: working past started_at + expected_seconds + 120 (backdated, no real wait)
    register("ts", owner="u10")
    tid = new_task("ts", "hangs forever")
    _, d = poll("ts")
    poll("ts", tasks=[{"task_id": tid, "lease_id": d["task"]["lease_id"], "phase": "running", "expected_seconds": 1}])
    sql("UPDATE tasks SET started_at=? WHERE id=?", (time.time() - 200, tid))
    f = wait_status(tid, "failed", timeout=10)
    check("sweep/timeout-stale", f.get("fail_reason") == "timeout_stale", f)

    # input_timeout: IR with no follow-up; keep polling so ONLY the IR timer can fire
    register("ir", owner="u11")
    tid = new_task("ir", "ask then silence")
    _, d = poll("ir")
    report("ir", tid, d["task"]["lease_id"], "input-required", question="anyone?")
    end = time.time() + 12
    f = {}
    while time.time() < end:
        f = call("GET", f"/domains/team-a/tasks/{tid}")[1]
        if f["status"] == "failed":
            break
        poll("ir")  # heartbeat; an IR task is legitimately absent from exec_state
        time.sleep(1)
    check("sweep/input-timeout", f.get("fail_reason") == "input_timeout", f)

    # agent retention: offline past agent_retention -> owner notified, then agent row deleted
    register("gone", owner="u12")
    sql("UPDATE agents SET last_seen_at=? WHERE agent_id='gone'", (time.time() - 400,))
    end = time.time() + 10
    while time.time() < end and q1("SELECT 1 FROM agents WHERE agent_id='gone'"):
        time.sleep(0.5)
    ev = q1("SELECT payload FROM task_events WHERE task_id='reg:gone' AND type='notify'")
    check("sweep/agent-cleaned", not q1("SELECT 1 FROM agents WHERE agent_id='gone'")
          and ev and "u12" in ev["payload"], ev)


# ---------- worker process ----------

class Worker:
    """One worker process; recreated when the driver under test changes."""

    def __init__(self, agent, driver, home):
        self.agent, self.home, self.proc = agent, home, None
        self.cfg = os.path.join(SCRATCH, f"{agent}.yaml")
        self.logname = os.path.join(SCRATCH, f"{agent}.log")
        open(self.cfg, "w").write(f"""
server_url: http://127.0.0.1:{PORT}
domain: team-a
domain_token: {TOKEN}
agent: {{id: {agent}, owner: u1, accept_policy: auto, accept_from: all}}
notify: {{channel: log, id_type: text, id: u1}}
default_driver: main
need_input_marker: '^NEED_INPUT:'
workspace_dir: {os.path.join(SCRATCH, 'ws')}
local_port: {free_port()}          # explicit: the derived default could collide with a stray process
busy_poll_interval: 1
drivers:
  main: {driver}
""")
        self.log = open(self.logname, "ab")
        self.proc = subprocess.Popen([venv_python(WRK), "worker.py"], cwd=WRK,
                                     env={**os.environ, "HOME": self.home, "A2A_WORKER_CONFIG": self.cfg},
                                     stdout=self.log, stderr=subprocess.STDOUT)
        WORKERS.append(self)

    def _close_log(self):
        if not self.log.closed:
            self.log.close()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._close_log()
        time.sleep(3)  # > poll_wait: the server-side suspended poll must expire before reuse

    def kill(self):  # hard cleanup on abnormal exit
        if self.proc.poll() is None:
            self.proc.kill()
        self._close_log()

    def online(self):
        return any(a["agent_id"] == self.agent and a["online"] for a in call("GET", "/domains/team-a/agents")[1])

    def wait_online(self, timeout=25):
        end = time.time() + timeout
        while not self.online() and time.time() < end:
            time.sleep(0.3)
        return self.online()

    def wait_exit(self, timeout=15):
        end = time.time() + timeout
        while self.proc.poll() is None and time.time() < end:
            time.sleep(0.3)
        return self.proc.poll() is not None


def worker_checks():
    home = os.path.join(SCRATCH, "home")
    os.makedirs(home, exist_ok=True)
    register("boss", owner="boss")

    # stdin delivery: the prompt comes back as the result
    w = Worker("wk", "{kind: command, cmd: 'cat', timeout: 60, output: tail}", home)
    check("worker/registers", w.wait_online())
    tid = new_task("wk", "ping-stdin-42")
    det = wait_status(tid, "completed")
    check("worker/stdin-e2e", det["status"] == "completed" and "ping-stdin-42" in det["convo"][-1]["text"], det)

    # a second worker for the same agent id must refuse to start
    p2 = subprocess.run([venv_python(WRK), "worker.py"], cwd=WRK,
                        env={**os.environ, "HOME": home, "A2A_WORKER_CONFIG": w.cfg},
                        capture_output=True, text=True, timeout=20)
    check("worker/single-instance-lock", p2.returncode != 0 and "another worker" in p2.stdout + p2.stderr)

    # rc=0 without output is a failure, never a silent success
    w.stop()
    w = Worker("wk", "{kind: command, cmd: 'true', timeout: 60, output: tail}", home)
    w.wait_online()
    det = wait_status(new_task("wk", "no output"), "failed")
    check("worker/anti-fake-success", det.get("fail_reason") == "driver_error", det)

    # NEED_INPUT marker -> input-required, question carried over
    w.stop()
    w = Worker("wk", '{kind: command, cmd: \'echo "NEED_INPUT: which branch?"\', timeout: 60, output: tail}', home)
    w.wait_online()
    det = wait_status(new_task("wk", "ask"), "input-required")
    check("worker/need-input-marker", det["status"] == "input-required" and "branch" in json.dumps(det["convo"]), det)

    # a long task survives past online_timeout (poll is the heartbeat), then cancel kills the driver
    w.stop()
    pidfile = os.path.join(SCRATCH, "cancel.pid")
    w = Worker("wk", f"{{kind: command, cmd: 'echo $$ > {pidfile}; sleep 47', timeout: 120, output: tail}}", home)
    w.wait_online()
    tid = new_task("wk", "long")
    check("worker/long-task-working", wait_status(tid, "working", timeout=25)["status"] == "working")
    time.sleep(12)  # longer than online_timeout: a dead poll loop would fail the task as worker_offline
    det = call("GET", f"/domains/team-a/tasks/{tid}")[1]
    check("worker/heartbeat-survives-online-timeout", det["status"] == "working", det)
    call("POST", f"/domains/team-a/tasks/{tid}/cancel", {}, agent="boss")
    check("worker/cancel->canceled", wait_status(tid, "canceled")["status"] == "canceled")
    time.sleep(2)
    check("worker/cancel-kills-driver-process", process_alive(pidfile) is False, f"pidfile={pidfile}")

    # owner deregisters while a driver runs: the worker exits AND the child does not survive
    pidfile = os.path.join(SCRATCH, "dereg.pid")
    w.stop()
    w = Worker("wk", f"{{kind: command, cmd: 'echo $$ > {pidfile}; sleep 47', timeout: 120, output: tail}}", home)
    w.wait_online()
    tid = new_task("wk", "long again")
    wait_status(tid, "working", timeout=25)
    call("POST", "/domains/team-a/agents/wk/deregister")
    check("worker/410-exit", w.wait_exit())
    check("worker/deregister-fails-inflight", wait_status(tid, "failed").get("fail_reason") == "agent_deregistered")
    time.sleep(1)
    check("worker/exit-kills-driver-process", process_alive(pidfile) is False, f"pidfile={pidfile}")

    # manual driver: report CLI fills the result; an empty completed report is refused
    register("wk", owner="u1")
    w = Worker("wk", "{kind: manual, manual_timeout_hours: 0.02}", home)
    w.wait_online()
    tid = new_task("wk", "human job")
    wait_status(tid, "working", timeout=25)
    subprocess.run([venv_python(WRK), "worker.py", "report", tid, "--status", "completed", "--output", "human did it"],
                   cwd=WRK, env={**os.environ, "HOME": home, "A2A_WORKER_CONFIG": w.cfg},
                   capture_output=True, text=True, timeout=20)
    det = wait_status(tid, "completed", timeout=20)
    check("worker/manual-report", "human did it" in det["convo"][-1]["text"], det)
    tid = new_task("wk", "empty report")
    wait_status(tid, "working", timeout=25)
    rep = subprocess.run([venv_python(WRK), "worker.py", "report", tid, "--status", "completed", "--output", ""],
                         cwd=WRK, env={**os.environ, "HOME": home, "A2A_WORKER_CONFIG": w.cfg},
                         capture_output=True, text=True, timeout=20)
    still = q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"]
    check("worker/empty-completed-report-refused", rep.returncode == 1 and still == "working", (rep.stdout, still))
    subprocess.run([venv_python(WRK), "worker.py", "report", tid, "--status", "completed", "--output", "late ok"],
                   cwd=WRK, env={**os.environ, "HOME": home, "A2A_WORKER_CONFIG": w.cfg},
                   capture_output=True, text=True, timeout=20)
    w.stop()
    cli_checks(w.cfg)


def cli(cfg_path, *args):
    p = subprocess.run([venv_python(WRK), os.path.join(ROOT, "a2a-skill", "api.py"), *args],
                       cwd=os.path.join(ROOT, "a2a-skill"),
                       env={**os.environ, "A2A_WORKER_CONFIG": cfg_path},
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def cli_checks(wcfg):
    """a2a-skill/api.py is the surface the skill actually drives."""
    rc, out, err = cli(wcfg, "agents")
    check("cli/agents", rc == 0 and "wk" in out, (rc, out[:80], err[:80]))
    rc, out, _ = cli(wcfg, "new", "--to", "wk", "--text", "cli smoke")
    tid = (json.loads(out) or {}).get("task_id") if rc == 0 and out.startswith("{") else None
    check("cli/new", bool(tid), out[:120])
    if tid:
        rc, out, _ = cli(wcfg, "get", "--task", tid)
        check("cli/get", rc == 0 and json.loads(out).get("id") == tid, out[:120])
        rc, out, _ = cli(wcfg, "list", "--role", "initiator")
        check("cli/list", rc == 0 and tid in out, out[:120])
        rc, out, _ = cli(wcfg, "cancel", "--task", tid)
        check("cli/cancel", rc == 0 and q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"] == "canceled", out[:120])
    rc, out, _ = cli(wcfg, "deregister")
    check("cli/deregister", rc == 0 and not q1("SELECT 1 FROM agents WHERE agent_id='wk'"), out[:120])


def main():
    global PORT
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH)
    PORT = free_port()
    cfg = os.path.join(SCRATCH, "server.yaml")
    open(cfg, "w").write(f"""
host: 127.0.0.1
port: {PORT}
language: en
public_base: http://127.0.0.1:{PORT}/    # trailing slash on purpose
db_path: {os.path.join(SCRATCH, 'verify.db')}
domains:
  - id: team-a
    token: {TOKEN}
  - id: team-b
    token: tok-b
admin_token: admtok
default_channel: log
online_timeout: 10
poll_wait: 2
sweep_interval: 1
dispatch_grace: 4
agent_retention: 300
task_retention: 300
approval_timeout: 3
input_required_timeout: 5
default_task_timeout: 60
""")
    srv = subprocess.Popen([venv_python(SRV), "server.py"], cwd=SRV,
                           env={**os.environ, "A2A_SERVER_CONFIG": cfg},
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        up = False
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/admin?token=admtok", timeout=2).read()
                up = True
                break
            except Exception:
                time.sleep(0.2)
        check("server/boots", up)
        if up:
            server_checks()
            worker_checks()
    finally:
        for w in WORKERS:  # abnormal exit must not leave workers (and their drivers) behind
            try:
                w.kill()
            except Exception:
                pass
        srv.terminate()
        log = srv.communicate(timeout=8)[0] or ""
        if FAILED:
            print("\n--- server log (tail) ---")
            print("\n".join(log.splitlines()[-12:]))
            for w in WORKERS:
                if os.path.exists(w.logname):
                    print(f"--- {os.path.basename(w.logname)} (tail) ---")
                    print("\n".join(open(w.logname).read().splitlines()[-8:]))
    print(f"\n{'ALL CHECKS PASSED' if not FAILED else 'FAILED: ' + ', '.join(FAILED)}")
    if not FAILED:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
