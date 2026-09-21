"""Driver layer: one-shot execution adapters. Worker core never sees tool specifics.

kind=command: local subprocess. Prompt goes via a background stdin thread + task
file (never argv); stdout is drained by another thread so pipes never deadlock;
the supervising loop enforces timeout and cancel by killing the process group.
Anti-fake-success: rc=0 with empty / error-marked / unparseable output is a
failure, not a success.
kind=manual:  handled by worker core (waits for `a2a-worker report`), not here.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time

FAIL_DRIVER = {"status": "failed"}


def _kill(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid via start_new_session
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)  # reap, no zombie leaks
    except subprocess.TimeoutExpired:
        pass


def _marker_hit(marker, output):
    for line in output[-2000:].splitlines():
        if re.search(marker, line):
            q = line.split(":", 1)[1].strip() if ":" in line else ""
            return q or "(question not captured on the marker line — see the task output)"
    return None


def _extract(mode, raw):
    if mode == "last_json":
        for line in reversed(raw.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("is_error") or obj.get("ok") is False:  # vendor envelopes: claude / openclaw exec
                return raw.strip(), "error flag in structured output"
            out = (obj.get("result") or obj.get("text") or obj.get("final") or "").strip()
            if not out:  # {"result": ""} is an empty answer, not the whole envelope
                return raw.strip(), "empty result in structured output"
            return out, None
        return raw.strip(), "no JSON line in output"
    return raw.strip()[-32000:], None  # tail


def run_command(driver_cfg, task, workspace, marker, cancel_event, expected):
    tid = task["id"]
    ws = os.path.abspath(os.path.join(workspace, tid))
    os.makedirs(ws, exist_ok=True)
    prompt = "\n".join(f"[{m['from']}] {m['text']}" for m in task["convo"])
    task_file = os.path.join(ws, "task.txt")
    with open(task_file, "w", encoding="utf-8") as f:  # utf-8, never BOM
        f.write(prompt)
    cmd = driver_cfg["cmd"].replace("{task_file}", task_file)  # NOT .format(): cmd may contain literal {} (JSON args)
    limit = expected
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=ws, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                start_new_session=(os.name != "nt"))  # cwd=task dir: files/ and out/ are relative
    except OSError as e:
        return {**FAIL_DRIVER, "fail_reason": "driver_error", "output": f"spawn failed: {e}"}

    def feed_stdin():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    out = []
    t_out = threading.Thread(target=lambda: out.append(proc.stdout.read() or ""), daemon=True)
    t_in = threading.Thread(target=feed_stdin, daemon=True)
    t_out.start()
    t_in.start()

    deadline = time.time() + limit
    timed_out = False
    while proc.poll() is None:
        if cancel_event.wait(0.3):
            _kill(proc)
            return {**FAIL_DRIVER, "fail_reason": "canceled_by_owner", "output": "driver killed on cancel"}
        if time.time() > deadline:
            _kill(proc)
            timed_out = True
            break
        time.sleep(0.2)
    t_out.join(timeout=10)
    rc = proc.returncode
    body = (out[0] if out else "").strip()
    if timed_out:
        return {**FAIL_DRIVER, "fail_reason": "timeout", "output": body[-4000:]}
    output, parse_err = _extract(driver_cfg.get("output"), body)
    if rc != 0:
        return {**FAIL_DRIVER, "fail_reason": "driver_error", "output": f"rc={rc} {output[:4000]}"}
    if parse_err or not output:
        # rc=0 is NOT success: empty/unparseable output is the worst failure (silent fake success)
        return {**FAIL_DRIVER, "fail_reason": "driver_error",
                "output": (parse_err or "empty output") + f" | raw tail: {body[-2000:]}"}
    if q := _marker_hit(marker, output):
        return {"status": "input-required", "question": q, "output": output}
    return {"status": "completed", "output": output}
