"""Driver layer: one-shot execution adapters. Worker core never sees tool specifics.

kind=command: local subprocess. Prompt goes via a background stdin thread + task
file (never argv); stdout is drained by another thread so pipes never deadlock;
the supervising loop enforces timeout and cancel by killing the process group.
Anti-fake-success: rc=0 with empty / error-marked / unparseable output is a
failure, not a success.
kind=manual:  handled by worker core (waits for `a2a-worker report`), not here.
"""
import collections
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
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid via start_new_session
        elif proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)  # reap, no zombie leaks
    except subprocess.TimeoutExpired:
        pass


def _marker_hit(marker, output):
    # the marker must be the LAST output line: a completed answer that merely
    # mentions the protocol mid-text must not stall the task as input-required
    lines = output[-2000:].rstrip().splitlines()
    if not lines or not re.search(marker, lines[-1]):
        return None
    line = lines[-1]
    q = line.split(":", 1)[1].strip() if ":" in line else ""
    return q or (lines[-2].strip() if len(lines) > 1 else "(question not captured on the marker line — see the task output)")


def _last_json(raw):
    """The driver's result envelope: the whole output if it is one JSON object
    (single-line or pretty-printed), else the LAST {...} block or line."""
    cands = ([raw[m.start():].strip() for m in reversed(list(re.finditer(r"(?m)^\s*\{", raw)))]
             + [ln.strip() for ln in reversed(raw.splitlines())])
    for cand in cands:
        if cand.startswith("{"):
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
    return None


def _extract(mode, raw):
    if mode == "last_json":
        obj = _last_json(raw)
        if obj is None:
            return raw.strip(), "no JSON line in output"
        if obj.get("is_error") or obj.get("ok") is False:  # vendor envelopes: claude / openclaw exec
            return raw.strip(), "error flag in structured output"
        # key presence, not truthiness: 0/false are real results; null is not
        out = next((str(obj[k]).strip() for k in ("result", "text", "final")
                    if k in obj and obj[k] is not None), "")
        if not out:  # {"result": ""} is an empty answer, not the whole envelope
            return raw.strip(), "empty result in structured output"
        return out, None
    return raw.strip()[-32000:], None  # tail


def run_command(driver_cfg, task, ws, marker, cancel_event, expected):
    os.makedirs(ws, exist_ok=True)
    cwd = os.path.abspath(os.path.expanduser(driver_cfg.get("cwd") or ws))
    os.makedirs(cwd, exist_ok=True)
    base_dir = "." if cwd == os.path.abspath(ws) else ws
    fnames = ", ".join(f["name"] for f in (task.get("files") or []))
    m_hint = marker.lstrip("^").rstrip(":") + ":"
    ctx = (f"[A2A context: workspace={ws}"
           + (f" | input files in {base_dir}/files/: {fnames}" if fnames else "")
           + f" | write output files to {base_dir}/out/ | to ask a follow-up question, print `{m_hint} <question>` as the very last line]")
    prompt = "\n".join(f"[{m['from']}] {m['text']}" for m in task["convo"]) + f"\n\n{ctx}\n"
    task_file = os.path.join(ws, "task.txt")
    with open(task_file, "w", encoding="utf-8") as f:
        f.write(prompt)
    cmd = driver_cfg["cmd"].replace("{task_file}", task_file).replace("{task_dir}", ws)
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, "A2A_TASK_DIR": ws},
                                start_new_session=(os.name != "nt"))
    except OSError as e:
        return {**FAIL_DRIVER, "fail_reason": "driver_error", "output": f"spawn failed: {e}"}

    def feed_stdin():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    out_tail, err_tail = collections.deque(), collections.deque()

    def drain(pipe, tail):
        size = 0
        try:
            for line in iter(pipe.readline, ""):
                tail.append(line)
                size += len(line)
                if size > 1_048_576:
                    size -= len(tail.popleft())
        except (OSError, ValueError):
            pass

    t_out = threading.Thread(target=drain, args=(proc.stdout, out_tail), daemon=True)
    t_err = threading.Thread(target=drain, args=(proc.stderr, err_tail), daemon=True)
    t_out.start()
    t_err.start()
    threading.Thread(target=feed_stdin, daemon=True).start()

    deadline = time.time() + expected
    timed_out = False
    while proc.poll() is None:
        if cancel_event.wait(0.3):
            _kill(proc)
            return {**FAIL_DRIVER, "fail_reason": "canceled_by_owner", "output": "driver killed on cancel"}
        if time.time() > deadline:
            timed_out = True
            break
    _kill(proc)  # reap any lingering child processes in the process group
    t_out.join(timeout=2)
    t_err.join(timeout=2)
    rc = proc.returncode
    out_body = "".join(out_tail).strip()
    err_body = "".join(err_tail).strip()
    combined = (f"{err_body}\n{out_body}".strip() if err_body and out_body else (out_body or err_body))
    if timed_out:
        return {**FAIL_DRIVER, "fail_reason": "timeout", "output": combined[-4000:]}
    mode = driver_cfg.get("output")
    output, parse_err = _extract(mode, out_body if mode == "last_json" and out_body else combined)
    if rc != 0:
        return {**FAIL_DRIVER, "fail_reason": "driver_error", "output": f"rc={rc} {combined[:4000]}"}
    if parse_err or not output:
        return {**FAIL_DRIVER, "fail_reason": "driver_error",
                "output": (parse_err or "empty output") + f" | raw tail: {combined[-2000:]}"}
    if q := _marker_hit(marker, output):
        return {"status": "input-required", "question": q, "output": output}
    return {"status": "completed", "output": output}
