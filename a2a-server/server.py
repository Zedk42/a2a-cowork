import asyncio, json, os, re, time, uuid
from html import escape
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import im_channels
import notify
from db import INFLIGHT, connect

# Concurrency contract: everything runs on ONE asyncio event loop with SYNCHRONOUS
# sqlite calls. The read-modify-write sequences in emit()/deliver() rely on no
# `await` appearing inside a DB section — adding one there silently breaks
# atomicity. Keep DB sections await-free. Notification sends are blocking IM
# HTTP calls and therefore always run in a thread (asyncio.to_thread), AFTER the
# commit of the state transition they describe.

DEFAULTS = {
    "host": "0.0.0.0", "port": 8100, "language": "zh", "db_path": "a2a.db",
    "default_channel": "log", "public_base": "", "admin_token": "", "online_timeout": 90,
    "poll_wait": 30, "sweep_interval": 5, "dispatch_grace": 60, "agent_retention": 259200,
    "task_retention": 2592000, "approval_timeout": 1800, "input_required_timeout": 86400,
    "default_task_timeout": 3600,
}

def env_expand(v):
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), str(v))


_cfg_path = Path(os.environ.get("A2A_SERVER_CONFIG", Path(__file__).parent / "server.yaml"))
if not _cfg_path.exists():
    raise SystemExit(f"config not found: {_cfg_path} (copy server.example.yaml to server.yaml)")
CFG = {**DEFAULTS, **yaml.safe_load(_cfg_path.read_text())}
for _k in ("online_timeout", "poll_wait", "sweep_interval", "dispatch_grace", "agent_retention",
           "task_retention", "approval_timeout", "input_required_timeout", "default_task_timeout"):
    _v = CFG[_k]
    if not isinstance(_v, (int, float)) or _v <= 0:  # e.g. "90s" strings silently kill the sweeper
        raise SystemExit(f"config error: {_k} must be a positive number of seconds, got {_v!r}")
if CFG["poll_wait"] >= 60:
    raise SystemExit("config error: poll_wait must be < 60 — the worker's HTTP client gives up at 65s; "
                     "a longer suspend leaves dead-socket pollers that can lease tasks nobody receives")
if not isinstance(CFG.get("domains"), list) or not CFG["domains"]:
    raise SystemExit("config error: domains must be a non-empty list")
for _d in CFG["domains"]:
    if not _d.get("id") or not _d.get("token"):
        raise SystemExit(f"config error: every domain needs id and token, got {_d!r}")
# IM adapters register only when their credentials are complete; the rest stay
# unavailable (register answers 400 channel_unavailable)
notify.CHANNELS.update(im_channels.build({
    **({k: env_expand(v) for k, v in CFG["feishu"].items()} if isinstance(CFG.get("feishu"), dict) else {}),
    **({"telegram_bot_token": env_expand(CFG["telegram_bot_token"])} if CFG.get("telegram_bot_token") else {}),
    **({"slack_bot_token": env_expand(CFG["slack_bot_token"])} if CFG.get("slack_bot_token") else {}),
}))
DOMAINS = {dm["id"]: dm for dm in CFG["domains"]}
for _dm in CFG["domains"]:  # one IM per domain; that channel must actually exist
    _ch = _dm.get("channel") or CFG["default_channel"]
    if _ch not in notify.CHANNELS:
        raise SystemExit(f"config error: channel {_ch!r} of domain {_dm['id']!r} is not available "
                         f"(configured: {', '.join(notify.CHANNELS)})")
if CFG["language"] not in notify.MESSAGES:
    raise SystemExit(f"config error: language {CFG['language']!r} not in catalog {list(notify.MESSAGES)}")
CFG["public_base"] = str(CFG["public_base"] or "").rstrip("/")  # no doubled slash in notify links
notify.set_lang(CFG["language"])
DB = connect(CFG["db_path"])
WAITERS = {}  # (domain, agent_id) -> asyncio.Event, woken when work appears
DEREGISTERED = {}  # (domain, agent_id) -> expiry: short tombstone so a live
# worker's next poll gets 410 and exits instead of silently re-registering

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(sweep())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


class ApiErr(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


@app.exception_handler(ApiErr)
async def api_err(request, exc):  # async: stays on the event loop, rollback stays single-threaded
    DB.rollback()  # never leave a half-written transaction to a later unrelated commit
    return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)


@app.exception_handler(Exception)
async def any_err(request, exc):  # same rollback duty for uncaught errors (500s)
    DB.rollback()
    print(f"[server] unhandled {request.url.path}: {exc!r}", flush=True)
    return JSONResponse({"error": {"code": "internal"}}, status_code=500)


def now():
    return time.time()


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def domain_channel(d) -> str:
    """The one IM this domain uses; owners bind on exactly it (uniformity contract).
    .get(): DB rows may outlive a domain removed from the config — they must not
    crash the sweeper or /admin, they simply keep the default channel."""
    return DOMAINS.get(d, {}).get("channel") or CFG["default_channel"]


def domain_of(request, path_domain) -> str:
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    for d in CFG["domains"]:
        if token and token == env_expand(d["token"]) and path_domain == d["id"]:
            return d["id"]
    raise ApiErr(401, "bad token")


async def json_body(req):
    try:
        b = await req.json()
    except Exception:
        b = None
    if not isinstance(b, dict):  # not an assert: this must survive python -O
        raise ApiErr(400, "bad json body")
    return b


def q(sql, args=()):
    return DB.execute(sql, args).fetchall()


def q1(sql, args=()):
    r = DB.execute(sql, args).fetchone()
    return dict(r) if r else None


def x(sql, args=()):
    return DB.execute(sql, args).rowcount


def emit(task_id, domain, etype, payload):
    seq = (q1("SELECT MAX(seq) m FROM task_events WHERE domain_id=? AND task_id=?", (domain, task_id))["m"] or 0) + 1
    DB.execute("INSERT INTO task_events (domain_id,task_id,seq,type,payload,created_at) VALUES (?,?,?,?,?,?)",
               (domain, task_id, seq, etype, json.dumps(payload, ensure_ascii=False), now()))
    DB.commit()


def wake(domain, agent_id):
    ev = WAITERS.get((domain, agent_id))
    if ev:
        ev.set()


def drop_waiter(domain, agent_id):
    WAITERS.pop((domain, agent_id), None)


# ---------- notifications ----------

def owner_binding(domain, username):
    """The binding notifications actually go through: default channel first, else any.
    Single source of truth — directory health must reflect the real send path."""
    return (q1("SELECT * FROM owner_bindings WHERE domain_id=? AND username=? AND channel=?",
               (domain, username, domain_channel(domain)))
            or q1("SELECT * FROM owner_bindings WHERE domain_id=? AND username=? LIMIT 1", (domain, username)))


async def send_to_owner(domain, username, key, task_id=None, ctx=None):
    """Best-effort notify; failures are recorded, never block task flow.
    Sends run in a worker thread — a slow IM must never stall the event loop."""
    b = owner_binding(domain, username)
    try:
        text = notify.t(key, **(ctx or {}))
        if not b or not b["verified"]:
            raise LookupError("no verified binding")
        # send through the channel the binding belongs to, not blindly the default
        await asyncio.to_thread(notify.CHANNELS[b["channel"]].send_text, b["platform_uid"], text)
        kind = "notify"
    except Exception as e:
        kind, text = "notify_failed", f"{key}: {e}"
        print(f"[notify] failed to={username} {text}", flush=True)  # failures must be visible even without a task
    if task_id:
        emit(task_id, domain, kind, {"to": username, "text": text})


async def notify_task(task, key, to="both", **ctx):
    """to: both | initiator | target — which owners get the message."""
    d = task["domain_id"]
    kv = {"base": CFG["public_base"], "domain": d, "task_id": task["id"],
          "initiator": task["initiator"], "target": task["target"], **ctx}
    aids = [a for a, want in ((task["initiator"], to in ("both", "initiator")),
                              (task["target"], to in ("both", "target"))) if want]
    for aid in dict.fromkeys(aids):  # a self-dispatched task must not notify the same owner twice
        if a := q1("SELECT owner_username o FROM agents WHERE domain_id=? AND agent_id=?", (d, aid)):
            await send_to_owner(d, a["o"], key, task["id"], kv)


async def fail_task(task, reason, to="both", output_head=None):
    # guarded: a task already in a terminal state can never be re-failed (no revival, no double notify)
    if x("UPDATE tasks SET status='failed', fail_reason=?, finished_at=? WHERE id=? AND status NOT IN ('completed','failed','canceled')",
         (reason, now(), task["id"])) == 0:
        return False
    emit(task["id"], task["domain_id"], "state_change", {"to": "failed", "reason": reason,
                                                         **({"output": output_head[:2000]} if output_head else {})})
    await notify_task(task, "task_failed", to=to, reason=notify.reason_desc(reason))
    wake(task["domain_id"], task["target"])
    return True


# ---------- serialization ----------

def task_json(t, convo=False):
    out = {k: t[k] for k in ("id", "initiator", "target", "text", "status", "fail_reason", "timeout_seconds")}
    out["created_at"] = iso(t["created_at"])
    if convo:
        out["convo"] = [{**m, "at": iso(m["at"])} for m in json.loads(t["convo"])]
        out["events"] = [{**dict(r), "created_at": iso(r["created_at"])}
                         for r in q("SELECT seq,type,payload,created_at FROM task_events WHERE domain_id=? AND task_id=? ORDER BY seq", (t["domain_id"], t["id"]))]
    return out


def require_task(domain, task_id):
    t = q1("SELECT * FROM tasks WHERE domain_id=? AND id=?", (domain, task_id))
    if not t:
        raise ApiErr(404, "no task")
    return t


def require_agent(domain, aid):
    a = q1("SELECT * FROM agents WHERE domain_id=? AND agent_id=?", (domain, aid))
    if not a:
        raise ApiErr(404, "no agent")
    return a


# ---------- agent lifecycle ----------

@app.post("/domains/{d}/agents/register")
async def register(d, req: Request):
    domain_of(req, d)
    b = await json_body(req)
    for k in ("agent_id", "owner_username", "notify"):
        if k not in b:
            raise ApiErr(400, f"missing {k}")
    af = b.get("accept_from", "all")
    if af != "all" and not (isinstance(af, list) and all(isinstance(i, str) for i in af)):
        raise ApiErr(400, "accept_from must be 'all' or a list of agent ids")
    if b.get("accept_policy", "notify_run") not in ("auto", "notify_run", "manual"):
        raise ApiErr(400, "accept_policy must be auto | notify_run | manual")
    nb = b["notify"] if isinstance(b["notify"], dict) else {}
    if not nb.get("channel") or not nb.get("id") or not nb.get("id_type"):
        raise ApiErr(400, "notify must have channel, id_type and id")
    if nb["channel"] not in notify.CHANNELS:
        raise ApiErr(400, "channel_unavailable")
    if nb["channel"] != domain_channel(d):
        raise ApiErr(400, f"notify.channel must be {domain_channel(d)} — the uniform channel of this domain")
    ch = notify.CHANNELS[nb["channel"]]
    try:  # normalize may hit the platform API (email→open_id): keep it off the event loop
        uid = await asyncio.to_thread(ch.normalize, nb)
        verified = 1
    except Exception:
        uid, verified = "", 0
    ts = now()
    session = uuid.uuid4().hex  # distinguishes worker processes: an orphan lease
    DEREGISTERED.pop((d, b["agent_id"]), None)  # an explicit re-register revives the agent
    DB.execute("INSERT OR REPLACE INTO agents "
               "(domain_id, agent_id, owner_username, description, accept_policy, accept_from, "
               "default_driver, default_driver_kind, session, last_seen_at, registered_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (d, b["agent_id"], b["owner_username"], b.get("description", ""),
                b.get("accept_policy", "notify_run"),
                "all" if af == "all" else json.dumps(af), b.get("default_driver", "command"),
                b.get("default_driver_kind", "command"), session, ts, ts))
    old = q1("SELECT verified FROM owner_bindings WHERE domain_id=? AND username=? AND channel=?",
             (d, b["owner_username"], nb["channel"]))
    if verified or not (old and old["verified"]):  # a failed declaration never overwrites a verified binding
        DB.execute("INSERT OR REPLACE INTO owner_bindings "
                   "(domain_id, username, channel, id_type, id, platform_uid, verified, updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (d, b["owner_username"], nb["channel"], nb["id_type"], nb["id"], uid, verified, ts))
    DB.commit()
    if verified:
        try:
            await asyncio.to_thread(ch.send_text, uid,
                                    notify.t("register_verify", owner=b["owner_username"], agent=b["agent_id"]))
        except Exception:  # the delivery is part of verification, not just the id conversion
            DB.execute("UPDATE owner_bindings SET verified=0 WHERE domain_id=? AND username=? AND channel=?",
                       (d, b["owner_username"], nb["channel"]))
            DB.commit()
            verified = 0
    # emit exactly one event: notify_failed on a broken binding, register_verify otherwise
    emit(f"reg:{b['agent_id']}", d, "notify_failed" if not verified else "register_verify",
         {"notify_verified": bool(verified)})
    return {"notify_verified": bool(verified), "session_id": session}


@app.post("/domains/{d}/agents/{aid}/deregister")
async def deregister(d, aid, req: Request):
    domain_of(req, d)
    a = require_agent(d, aid)
    for t in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status NOT IN ('completed','failed','canceled')", (d, aid)):
        await fail_task(t, "agent_deregistered", to="initiator")
    await send_to_owner(d, a["owner_username"], "agent_deregistered", task_id=f"reg:{aid}",
                        ctx={"agent": aid})  # BEFORE deleting the binding, or the goodbye can never be delivered
    # the send above awaits, so a re-register may have landed meanwhile (deregister
    # → restart with new config): only OUR session's row may die, the fresh one survives
    if x("DELETE FROM agents WHERE domain_id=? AND agent_id=? AND session=?", (d, aid, a["session"])):
        if not q1("SELECT 1 FROM agents WHERE domain_id=? AND owner_username=?", (d, a["owner_username"])):
            x("DELETE FROM owner_bindings WHERE domain_id=? AND username=?", (d, a["owner_username"]))
        DB.commit()
        drop_waiter(d, aid)
        DEREGISTERED[(d, aid)] = now() + 60
    return Response(status_code=204)


@app.get("/domains/{d}/agents")
async def list_agents(d, req: Request):
    domain_of(req, d)
    out = []
    for a in q("SELECT * FROM agents WHERE domain_id=?", (d,)):
        b = owner_binding(d, a["owner_username"])  # same lookup the send path uses
        out.append({"agent_id": a["agent_id"], "owner_username": a["owner_username"], "description": a["description"],
                    "accept_policy": a["accept_policy"], "default_driver": a["default_driver"],
                    "default_driver_kind": a["default_driver_kind"],
                    "online": bool(a["last_seen_at"] and now() - a["last_seen_at"] < CFG["online_timeout"]),
                    "notify_verified": bool(b and b["verified"])})
    return out


# ---------- task lifecycle ----------

async def create_task_core(d, initiator, target_id, text, timeout=None):
    target = require_agent(d, target_id)
    if not isinstance(text, str) or not text.strip():
        raise ApiErr(400, "text must be a non-empty string")
    if timeout is not None and not (isinstance(timeout, int) and timeout > 0):
        raise ApiErr(400, "timeout must be a positive int")
    tid, ts = uuid.uuid4().hex, now()
    timeout = timeout or CFG["default_task_timeout"]
    convo = json.dumps([{"from": initiator, "text": text, "at": ts}], ensure_ascii=False)
    allow = target["accept_from"] == "all" or initiator in json.loads(target["accept_from"])
    if not allow:
        DB.execute("INSERT INTO tasks (id,domain_id,initiator,target,text,convo,timeout_seconds,"
                   "created_at,status,fail_reason,available_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (tid, d, initiator, target_id, text, convo, timeout, ts, "failed", "source_not_allowed", ts, ts))
        DB.commit()
        emit(tid, d, "state_change", {"to": "failed", "reason": "source_not_allowed"})
        await notify_task({"id": tid, "domain_id": d, "initiator": initiator, "target": target_id},
                          "task_failed", to="target", reason=notify.reason_desc("source_not_allowed"))
        return tid, "failed"
    if target["accept_policy"] == "manual":
        DB.execute("INSERT INTO tasks (id,domain_id,initiator,target,text,convo,timeout_seconds,"
                   "created_at,status) VALUES (?,?,?,?,?,?,?,?,?)",
                   (tid, d, initiator, target_id, text, convo, timeout, ts, "pending-approval"))
        DB.execute("INSERT INTO approvals (task_id,domain_id,state,expires_at,decided_at) "
                   "VALUES (?,?,'pending',?,NULL)", (tid, d, ts + CFG["approval_timeout"]))
        DB.commit()
        emit(tid, d, "state_change", {"to": "pending-approval"})
        await notify_task({"id": tid, "domain_id": d, "initiator": initiator, "target": target_id}, "manual_confirm",
                          to="target", agent=target_id, summary=text[:120])
        return tid, "pending-approval"
    DB.execute("INSERT INTO tasks (id,domain_id,initiator,target,text,convo,timeout_seconds,"
               "created_at,status,available_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (tid, d, initiator, target_id, text, convo, timeout, ts, "available", ts))
    DB.commit()
    emit(tid, d, "state_change", {"to": "available"})
    if target["accept_policy"] == "notify_run":
        await notify_task({"id": tid, "domain_id": d, "initiator": initiator, "target": target_id}, "task_received",
                          to="target", agent=target_id, summary=text[:120])
    wake(d, target_id)
    return tid, "available"


def followup_core(d, task, text):
    convo = json.loads(task["convo"])
    convo.append({"from": task["initiator"], "text": text, "at": now()})
    # guarded: only the first follow-up wins; a racing second one sees 0 rows and 409s
    if x("UPDATE tasks SET status='available', available_at=?, convo=?, ir_at=NULL WHERE id=? AND status='input-required'",
         (now(), json.dumps(convo, ensure_ascii=False), task["id"])) == 0:
        raise ApiErr(409, "task not input-required")
    DB.commit()
    emit(task["id"], d, "state_change", {"to": "available", "via": "followup"})
    wake(d, task["target"])
    return "available"


async def cancel_core(task, reason):
    was_pa = task["status"] == "pending-approval"
    if x("UPDATE tasks SET status='canceled', fail_reason=?, finished_at=? WHERE id=? AND status NOT IN ('completed','failed','canceled')",
         (reason, now(), task["id"])) == 0:
        raise ApiErr(409, "task already finished")
    if was_pa:
        x("UPDATE approvals SET state='canceled', decided_at=? WHERE task_id=?", (now(), task["id"]))
    DB.commit()
    emit(task["id"], task["domain_id"], "state_change", {"to": "canceled", "reason": reason})
    await notify_task(task, "task_canceled", reason=notify.reason_desc(reason))
    if task["status"] in INFLIGHT:
        wake(task["domain_id"], task["target"])
    return "canceled"


@app.post("/domains/{d}/tasks")
async def create_task(d, req: Request):
    domain_of(req, d)
    b = await json_body(req)  # parse BEFORE reading state: no stale snapshot after an await
    initiator = req.headers.get("x-agent-id")
    if not initiator:
        raise ApiErr(403, "missing X-Agent-Id")
    if not b.get("target") or not b.get("text"):
        raise ApiErr(400, "missing target/text")
    require_agent(d, initiator)
    tid, status = await create_task_core(d, initiator, b["target"], b["text"], b.get("timeout_seconds"))
    return JSONResponse({"task_id": tid, "status": status}, status_code=201)


@app.get("/domains/{d}/tasks")
async def list_tasks(d, req: Request, status=None, role=None):
    domain_of(req, d)
    me = req.headers.get("x-agent-id")
    sql, args = "SELECT * FROM tasks WHERE domain_id=?", [d]
    if status:
        sql += " AND status=?"
        args.append(status)
    if role:
        if role not in ("initiator", "target"):
            raise ApiErr(400, "bad role")
        if not me:
            raise ApiErr(403, "missing X-Agent-Id")  # otherwise the role filter silently matches nothing
        sql += f" AND {role}=?"
        args.append(me)
    return [task_json(t) for t in q(sql + " ORDER BY created_at DESC LIMIT 100", args)]


@app.get("/domains/{d}/tasks/{tid}")
async def task_detail(d, tid, req: Request):
    domain_of(req, d)
    return task_json(require_task(d, tid), convo=True)


@app.post("/domains/{d}/tasks/{tid}/cancel")
async def cancel_task(d, tid, req: Request):
    domain_of(req, d)
    task = require_task(d, tid)
    if req.headers.get("x-agent-id") != task["initiator"]:
        raise ApiErr(403, "not initiator")  # owners abort via /action
    return {"status": await cancel_core(task, "canceled_by_initiator")}


@app.post("/domains/{d}/tasks/{tid}/messages")
async def followup(d, tid, req: Request):
    domain_of(req, d)
    b = await json_body(req)  # parse before reading state
    task = require_task(d, tid)
    if req.headers.get("x-agent-id") != task["initiator"]:
        raise ApiErr(403, "not initiator")
    if not isinstance(b.get("text"), str) or not b["text"].strip():
        raise ApiErr(400, "text must be a non-empty string")
    followup_core(d, task, b["text"])  # guarded UPDATE; races surface as 409 from the core
    return {"status": "available"}


@app.post("/domains/{d}/tasks/{tid}/action")
async def owner_action(d, tid, req: Request):
    """Owner entry point (approve/reject/abort). IM card callbacks forward here too;
    until IM adapters exist, the owner calls this from any of their own agents."""
    domain_of(req, d)
    body = await json_body(req)  # parse BEFORE reading state: no stale snapshot after an await
    task = require_task(d, tid)
    me = req.headers.get("x-agent-id")
    if not me:
        raise ApiErr(403, "missing X-Agent-Id")
    target = require_agent(d, task["target"])
    actor = require_agent(d, me)
    if actor["owner_username"] != target["owner_username"]:
        raise ApiErr(403, "not the owner of the target agent")
    action = body.get("action")
    if action == "approve":
        if x("UPDATE tasks SET status='available', available_at=? WHERE id=? AND status='pending-approval'",
             (now(), tid)) == 0:  # approval expired between read and write -> no revival
            raise ApiErr(409, "not pending-approval")
        x("UPDATE approvals SET state='approved', decided_at=? WHERE task_id=?", (now(), tid))
        DB.commit()
        emit(tid, d, "state_change", {"to": "available", "via": "approve"})
        wake(d, task["target"])
        return {"status": "available"}
    if action == "reject":
        if task["status"] != "pending-approval":
            raise ApiErr(409, "not pending-approval")
        if not await fail_task(task, "rejected"):
            raise ApiErr(409, "not pending-approval")
        x("UPDATE approvals SET state='rejected', decided_at=? WHERE task_id=?", (now(), tid))
        DB.commit()
        return {"status": "failed"}
    if action == "abort":
        return {"status": await cancel_core(task, "canceled_by_owner")}
    raise ApiErr(400, "bad action")


# ---------- worker protocol ----------

@app.post("/domains/{d}/agents/{aid}/poll")
async def poll(d, aid, req: Request):
    domain_of(req, d)
    b = await json_body(req)  # parse BEFORE reading state
    if (exp := DEREGISTERED.get((d, aid), 0)) and now() < exp:
        raise ApiErr(410, "deregistered")  # every poll inside the window gets 410, not just the first
    agent = require_agent(d, aid)
    agent_session = agent["session"]
    st = b.get("exec_state") or {"tasks": []}
    seen = [t for t in st.get("tasks", []) if isinstance(t, dict) and t.get("task_id")]

    x("UPDATE agents SET last_seen_at=? WHERE domain_id=? AND agent_id=?", (now(), d, aid))
    DB.commit()
    for t in seen:
        if t.get("phase") != "running":
            continue
        row = q1("SELECT lease_id FROM tasks WHERE domain_id=? AND id=? AND target=? AND status='dispatched'",
                 (d, t["task_id"], aid))
        if row and row["lease_id"] and row["lease_id"] == t.get("lease_id"):
            exp = t.get("expected_seconds")  # client value: must be a positive int or nothing (sweep does math on it)
            x("UPDATE tasks SET status='working', started_at=?, expected_seconds=? WHERE id=? AND status='dispatched'",
              (now(), exp if isinstance(exp, int) and exp > 0 else None, t["task_id"]))
            emit(t["task_id"], d, "state_change", {"to": "working"})
    for t in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status IN ('dispatched','working')", (d, aid)):
        orphan = t["id"] not in {s["task_id"] for s in seen}
        # judged against the SERVER-RECORDED session (agents.session): a lease held by
        # any other session belongs to a dead/old poller and fails immediately
        foreign = t["lease_session"] and t["lease_session"] != agent_session
        if (orphan and (foreign or now() - t["dispatched_at"] > CFG["dispatch_grace"])):
            await fail_task(t, "worker_restart")

    async def deliver():
        # re-read inside deliver: the second call runs AFTER the long-poll await, during
        # which the agent may have re-registered (new session) — leasing under the stale
        # captured session would get the task killed as worker_restart on the next poll
        a = q1("SELECT session, default_driver_kind FROM agents WHERE domain_id=? AND agent_id=?", (d, aid))
        if not a:
            return None  # deregistered while we were suspended
        seen_ids = {s["task_id"] for s in seen}
        # control.cancel for ANY terminal task still sitting in the worker's
        # exec_state (canceled by owner OR failed by server-side timeouts):
        # the worker kills its driver, drops the result, clears the slot
        cancels = [t["id"] for t in q("SELECT id FROM tasks WHERE domain_id=? AND target=? AND status IN ('canceled','failed')", (d, aid))
                   if t["id"] in seen_ids]
        if cancels:
            return {"control": {"cancel": cancels}}
        # busy = the worker is actually running something (dispatched/working).
        # input-required does NOT block delivery: the driver already finished there,
        # the agent is idle waiting for the initiator's follow-up.
        if q1("SELECT 1 FROM tasks WHERE domain_id=? AND target=? AND status IN ('dispatched','working') LIMIT 1", (d, aid)):
            return None
        row = q1("SELECT id FROM tasks WHERE domain_id=? AND target=? AND status='available' ORDER BY COALESCE(available_at, created_at) LIMIT 1", (d, aid))
        if not row:
            return None
        lease = uuid.uuid4().hex
        if x("UPDATE tasks SET status='dispatched', lease_id=?, lease_session=?, dispatched_at=? WHERE id=? AND status='available'",
             (lease, a["session"], now(), row["id"])) != 1:
            return None
        DB.commit()
        emit(row["id"], d, "state_change", {"to": "dispatched"})
        t = q1("SELECT * FROM tasks WHERE id=?", (row["id"],))
        if a["default_driver_kind"] == "manual":  # kind, not the driver's name
            await notify_task(t, "manual_dispatch", to="target", summary=t["text"][:200])
        return {"task": {"id": t["id"], "lease_id": lease, "text": t["text"], "initiator": t["initiator"],
                         "timeout_seconds": t["timeout_seconds"], "convo": json.loads(t["convo"])}}

    if (r := await deliver()) is not None:
        return r
    if not b.get("block", True):
        return Response(status_code=204)
    ev = WAITERS.setdefault((d, aid), asyncio.Event())
    ev.clear()
    try:
        await asyncio.wait_for(ev.wait(), timeout=CFG["poll_wait"])
    except asyncio.TimeoutError:
        pass
    # NOTE: a suspended poller whose process died can still claim here (writing
    # to a dead socket). We cannot detect that after the body was consumed
    # (request.is_disconnected blocks). deliver() leases under the CURRENT
    # agents.session, so the orphan is recovered by the grace-window restart
    # detection (or receive_timeout at 2×grace) — bounded, visible, and
    # re-dispatchable by a human.
    return (r := await deliver()) or Response(status_code=204)


@app.post("/domains/{d}/agents/{aid}/results")
async def results(d, aid, req: Request):
    domain_of(req, d)
    b = await json_body(req)
    task = q1("SELECT * FROM tasks WHERE domain_id=? AND id=?", (d, b.get("task_id")))
    lease_ok = task and task["target"] == aid and b.get("lease_id") and task["lease_id"] == b["lease_id"]
    if lease_ok and task["status"] == b.get("status") and not (
            task["status"] == "failed" and b.get("fail_reason") != task["fail_reason"]):
        return {"accepted": True, "duplicate": True}  # retry after a lost response: already applied
    # (a failed task re-reported with a DIFFERENT reason falls through to late_result)
    if not (lease_ok and task["status"] in ("dispatched", "working")):
        if task and not q1("SELECT 1 FROM task_events WHERE domain_id=? AND task_id=? AND type='late_result'", (d, task["id"])):
            emit(task["id"], d, "late_result", {"from_agent": aid})
            await notify_task(task, "late_result")
        return {"accepted": False, "reason": "late_result"}
    status = b.get("status")
    out_text = str(b.get("output") or "")  # never let a non-str poison slicing downstream
    if status == "completed":
        convo = json.loads(task["convo"])
        convo.append({"from": aid, "text": out_text, "at": now()})  # result readable via task detail
        if x("UPDATE tasks SET status='completed', convo=?, finished_at=? WHERE id=? AND status IN ('dispatched','working')",
             (json.dumps(convo, ensure_ascii=False), now(), task["id"])) == 0:
            raise ApiErr(409, "task no longer running")
        emit(task["id"], d, "state_change", {"to": "completed"})
        await notify_task(task, "task_completed", output_head=out_text[:400])
    elif status == "failed":
        reason = b.get("fail_reason")  # client value: keep only a usable string
        await fail_task(task, reason if isinstance(reason, str) and reason else "driver_error", output_head=out_text)
    elif status == "input-required":
        convo = json.loads(task["convo"])
        convo.append({"from": aid, "text": b.get("question", ""), "at": now()})
        x("UPDATE tasks SET status='input-required', ir_at=?, convo=? WHERE id=? AND status IN ('dispatched','working')",
          (now(), json.dumps(convo, ensure_ascii=False), task["id"]))
        DB.commit()
        emit(task["id"], d, "state_change", {"to": "input-required"})
        await notify_task(task, "input_required", to="initiator", question=b.get("question", ""))
    else:
        raise ApiErr(400, "bad status")
    wake(d, aid)
    return {"accepted": True}


# ---------- sweeper ----------

async def sweep():
    while True:
        try:
            await asyncio.sleep(CFG["sweep_interval"])
            t = now()
            for k, exp in list(DEREGISTERED.items()):  # tombstones expire by time, not by being consumed
                if exp < t:
                    DEREGISTERED.pop(k, None)
            for a in q("SELECT * FROM agents"):
                try:  # one bad agent row never stops the whole sweep
                    offline = not a["last_seen_at"] or t - a["last_seen_at"] > CFG["online_timeout"]
                    for task in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status IN (?,?,?)",
                                  (a["domain_id"], a["agent_id"], *INFLIGHT)):
                        if offline:
                            await fail_task(task, "worker_offline")
                        elif task["status"] == "working" and task["started_at"] + (task["expected_seconds"] or 2 * task["timeout_seconds"]) + 120 < t:
                            await fail_task(task, "timeout_stale")
                        elif task["status"] == "dispatched" and task["dispatched_at"] + 2 * CFG["dispatch_grace"] < t:
                            await fail_task(task, "receive_timeout")
                        elif task["status"] == "input-required" and task["ir_at"] and task["ir_at"] + CFG["input_required_timeout"] < t:
                            await fail_task(task, "input_timeout")
                except Exception as e:
                    DB.rollback()
                    print(f"[sweep] skip agent {a['agent_id']}: {e}", flush=True)
            for ap in q("SELECT a.* FROM approvals a JOIN tasks t2 ON t2.id=a.task_id WHERE a.state='pending' AND a.expires_at < ?", (t,)):
                task = q1("SELECT * FROM tasks WHERE id=?", (ap["task_id"],))
                x("UPDATE approvals SET state='expired', decided_at=? WHERE task_id=?", (t, ap["task_id"]))
                if task and task["status"] == "pending-approval":
                    await fail_task(task, "approval_expired")
            for a in q("SELECT * FROM agents WHERE last_seen_at IS NULL OR last_seen_at < ?", (t - CFG["agent_retention"],)):
                for task in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status NOT IN ('completed','failed','canceled')",
                              (a["domain_id"], a["agent_id"])):
                    await fail_task(task, "agent_deregistered", to="initiator")
                await send_to_owner(a["domain_id"], a["owner_username"], "agent_cleaned",
                                    task_id=f"reg:{a['agent_id']}",
                                    ctx={"agent": a["agent_id"], "days": int(CFG["agent_retention"] // 86400)})
                x("DELETE FROM agents WHERE domain_id=? AND agent_id=?", (a["domain_id"], a["agent_id"]))
                if not q1("SELECT 1 FROM agents WHERE domain_id=? AND owner_username=?", (a["domain_id"], a["owner_username"])):
                    x("DELETE FROM owner_bindings WHERE domain_id=? AND username=?", (a["domain_id"], a["owner_username"]))
                drop_waiter(a["domain_id"], a["agent_id"])
            DB.execute("DELETE FROM tasks WHERE finished_at IS NOT NULL AND finished_at < ?", (t - CFG["task_retention"],))
            DB.execute("DELETE FROM task_events WHERE created_at < ?", (t - CFG["task_retention"],))
            DB.execute("DELETE FROM approvals WHERE task_id NOT IN (SELECT id FROM tasks)")
            DB.commit()
        except Exception as e:
            DB.rollback()
            print(f"[sweep] error: {e}", flush=True)


# ---------- read-only admin ----------

@app.get("/admin", response_class=HTMLResponse)
async def admin(token=""):
    if not CFG["admin_token"] or token != CFG["admin_token"]:
        return HTMLResponse("unauthorized", status_code=401)
    t = now()
    verified_owners = {}  # owner_binding semantics: the domain's channel wins, else any binding
    for b in q("SELECT domain_id, username, channel, verified FROM owner_bindings"):
        key = (b["domain_id"], b["username"])
        if key not in verified_owners or b["channel"] == domain_channel(b["domain_id"]):
            verified_owners[key] = b["verified"]
    rows_a = "".join(
        f"<tr><td>{escape(a['agent_id'])}</td><td>{escape(a['owner_username'])}</td><td>{escape(a['accept_policy'])}</td>"
        f"<td>{escape(a['default_driver'])}</td><td>{'online' if (a['last_seen_at'] and t - a['last_seen_at'] < CFG['online_timeout']) else 'offline'}</td>"
        f"<td>{'ok' if verified_owners.get((a['domain_id'], a['owner_username'])) else 'unverified'}</td>"
        f"<td>{escape(a['description'])}</td></tr>"
        for a in q("SELECT * FROM agents ORDER BY domain_id, agent_id"))
    rows_t = "".join(f"<tr><td>{t2['id'][:8]}</td><td>{escape(t2['domain_id'])}</td><td>{escape(t2['initiator'])}</td>"
                     f"<td>{escape(t2['target'])}</td><td>{escape(t2['status'])}</td><td>{escape(t2['fail_reason'] or '')}</td></tr>"
                     for t2 in q("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100"))
    rows_e = "".join(f"<tr><td>{escape(e['task_id'][:12])}</td><td>{escape(e['type'])}</td><td>{escape(e['payload'][:160])}</td></tr>"
                     for e in q("SELECT * FROM task_events ORDER BY created_at DESC LIMIT 50"))
    return f"<h1>A2A Co-Work</h1><h2>Agents</h2><table border=1><tr><th>agent</th><th>owner</th><th>policy</th><th>driver</th><th>state</th><th>notify</th><th>desc</th></tr>{rows_a}</table>" \
           f"<h2>Tasks</h2><table border=1><tr><th>id</th><th>domain</th><th>from</th><th>to</th><th>status</th><th>reason</th></tr>{rows_t}</table>" \
           f"<h2>Recent events</h2><table border=1><tr><th>task</th><th>type</th><th>payload</th></tr>{rows_e}</table>"


if __name__ == "__main__":
    import uvicorn
    for dm in CFG["domains"]:  # an unset env var silently disables a whole domain
        if not env_expand(dm.get("token", "")):
            print(f"[server] WARNING: domain {dm['id']} has an empty token ($VAR unset?); "
                  f"its requests will all get 401", flush=True)
    _f = CFG.get("feishu") or {}
    if (_f.get("app_id") or _f.get("app_secret")) and "feishu" not in notify.CHANNELS:
        print("[server] WARNING: feishu config incomplete (needs both app_id and app_secret); "
              "channel disabled", flush=True)
    if CFG["admin_token"] in ("", "change-me"):
        print("[server] WARNING: admin_token is default/empty — /admin exposes task texts; "
              "set a real token in server.yaml", flush=True)
    if not CFG["public_base"]:
        print("[server] WARNING: public_base is empty; links in notification texts "
              "will be relative — set it in server.yaml", flush=True)
    uvicorn.run(app, host=CFG["host"], port=int(CFG["port"]), log_level="warning")
