import asyncio, hashlib, json, os, re, secrets, signal, time, uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import im_channels
import notify
from admin_page import ADMIN_HTML
from db import INFLIGHT, connect

# Concurrency contract: everything runs on ONE asyncio event loop with SYNCHRONOUS
# sqlite calls. Keep DB read-modify-write sections await-free. Notification sends
# run in a worker thread (asyncio.to_thread) AFTER committing the state transition.

DEFAULTS = {
    "host": "0.0.0.0", "port": 8100, "language": "zh", "db_path": "a2a.db",
    "default_channel": "log", "public_base": "", "online_timeout": 90,
    "poll_wait": 30, "sweep_interval": 5, "dispatch_grace": 60, "agent_retention": 259200,
    "task_retention": 2592000, "approval_timeout": 1800, "input_required_timeout": 86400,
    "default_task_timeout": 3600, "max_file_mb": 50, "max_files_per_task": 10, "max_body_mb": 2,
}

def env_expand(v):
    if isinstance(v, dict):
        return {k: env_expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [env_expand(x) for x in v]
    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", lambda m: os.environ.get(m.group(1)) or m.group(2) or "", v) if isinstance(v, str) else v


_cfg_path = Path(os.environ.get("A2A_SERVER_CONFIG", Path(__file__).parent / "server.yaml"))
if not _cfg_path.exists():
    raise SystemExit(f"config not found: {_cfg_path} (copy server.example.yaml to server.yaml)")
_raw_cfg = yaml.safe_load(_cfg_path.read_text()) or {}
if not isinstance(_raw_cfg, dict):
    raise SystemExit(f"config error: {_cfg_path} must be a YAML mapping")
CFG = env_expand({**DEFAULTS, **_raw_cfg})
for _k, _dflt in DEFAULTS.items():
    if isinstance(_dflt, (int, float)):
        _v = CFG[_k]
        if isinstance(_v, bool) or not isinstance(_v, (int, float)) or _v <= 0:
            raise SystemExit(f"config error: {_k} must be a positive number, got {_v!r}")
if CFG["poll_wait"] >= 60:
    raise SystemExit("config error: poll_wait must be < 60 — the worker's HTTP client gives up at 65s; "
                     "a longer suspend leaves dead-socket pollers that can lease tasks nobody receives")
if CFG["online_timeout"] <= CFG["poll_wait"] + CFG["sweep_interval"]:
    raise SystemExit("config error: online_timeout must exceed poll_wait + sweep_interval — "
                     "a long-polling worker's heartbeat gap is one poll cycle plus round-trip")
if not isinstance(CFG.get("domains"), list) or not CFG["domains"]:
    raise SystemExit("config error: domains must be a non-empty list")
for _d in CFG["domains"]:
    if not isinstance(_d, dict) or not _d.get("id") or not _d.get("token"):
        raise SystemExit(f"config error: every domain needs id and token, got {_d!r} "
                         "(an unset ${ENV_VAR} expands to empty)")

notify.CHANNELS.update(im_channels.build(CFG))
DOMAINS = {dm["id"]: dm for dm in CFG["domains"]}
for _dm in CFG["domains"]:
    _ch = _dm.get("channel") or CFG["default_channel"]
    if _ch not in notify.CHANNELS:
        raise SystemExit(f"config error: channel {_ch!r} of domain {_dm['id']!r} is not available "
                         f"(configured: {', '.join(notify.CHANNELS)})")
if CFG["language"] not in notify.MESSAGES:
    raise SystemExit(f"config error: language {CFG['language']!r} not in catalog {list(notify.MESSAGES)}")
CFG["public_base"] = str(CFG["public_base"] or "").rstrip("/")
notify.set_lang(CFG["language"])
DB = connect(CFG["db_path"])
FILE_DIR = Path(CFG["db_path"]).parent / "files"
WAITERS = {}  # (domain, agent_id) -> asyncio.Event, woken when work appears
DEREGISTERED = {}  # (domain, agent_id) -> expiry tombstone (410)
STOPPING = False

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    global STOPPING
    STOPPING = False
    def _stop():
        global STOPPING
        STOPPING = True
        for ev in WAITERS.values():
            ev.set()
    prev = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    for sig, old in prev.items():
        signal.signal(sig, lambda s, f, _old=old: (_stop(), _old(s, f) if callable(_old) else None))
    task = asyncio.create_task(sweep())
    try:
        yield
    finally:
        _stop()
        task.cancel()
        for sig, old in prev.items():
            signal.signal(sig, old)


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


now = time.time
AGENT_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # ids travel in URL paths and the
# x-agent-id header: a "/" matches no route (404 poll loop), non-ASCII breaks the header
SESSION_RE = re.compile(r"[0-9a-f]{16,64}")  # a poller's self-declared process identity


def declared_session(b):
    s = b.get("session")
    return s if isinstance(s, str) and SESSION_RE.fullmatch(s) else None
INVITE_ABC = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # unambiguous: no 0/O, 1/I/L


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def online(a, t):
    return bool(a["last_seen_at"] and t - a["last_seen_at"] < CFG["online_timeout"])


def domain_channel(d) -> str:
    """The one IM this domain uses; owners bind on exactly it (uniformity contract).
    .get(): DB rows may outlive a domain removed from the config — they must not
    crash the sweeper or /admin, they simply keep the default channel."""
    return DOMAINS.get(d, {}).get("channel") or CFG["default_channel"]


def domain_of(request, path_domain) -> str:
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    for d in CFG["domains"]:
        if token and token == str(d["token"]) and path_domain == d["id"]:  # str(): YAML bare-number tokens
            return d["id"]
    raise ApiErr(401, "bad token")


async def json_body(req):
    """Call BEFORE reading DB state: the awaits below invalidate any snapshot
    taken above. Bodies over max_body_mb are refused."""
    cap = CFG["max_body_mb"] * 1024 * 1024
    if (cl := req.headers.get("content-length") or "").isdigit() and int(cl) > cap:
        raise ApiErr(413, "body too large")  # pre-check; chunked bodies hit the hard cap below
    raw = await req.body()
    if len(raw) > cap:
        raise ApiErr(413, "body too large")
    try:
        b = json.loads(raw)
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
    if ev := WAITERS.pop((domain, agent_id), None):
        ev.set()


# ---------- notifications ----------

def owner_binding(domain, username):
    """The binding notifications actually go through: default channel first, else any.
    Single source of truth — directory health must reflect the real send path."""
    b = (q1("SELECT * FROM owner_bindings WHERE domain_id=? AND username=? AND channel=?",
            (domain, username, domain_channel(domain)))
         or q1("SELECT * FROM owner_bindings WHERE domain_id=? AND username=? LIMIT 1", (domain, username)))
    # a binding on a channel removed from the config can neither send nor claim
    # health: report it as missing so the directory shows the truth
    return b if b and b["channel"] in notify.CHANNELS else None


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


async def fail_task(task, reason, to="both", output_head=None, files=None):
    # guarded: a task already in a terminal state can never be re-failed (no revival, no double notify)
    if x("UPDATE tasks SET status='failed', fail_reason=?, finished_at=? WHERE id=? AND status NOT IN ('completed','failed','canceled')",
         (reason, now(), task["id"])) == 0:
        return False
    emit(task["id"], task["domain_id"], "state_change", {"to": "failed", "reason": reason,
                                                         **({"output": output_head[:2000]} if output_head else {}),
                                                         **({"files": files} if files else {})})
    await notify_task(task, "task_failed", to=to, reason=notify.reason_desc(reason))
    wake(task["domain_id"], task["target"])
    return True


def _safe_name(name):
    name = re.sub(r"[\\/:\x00-\x1f]", "_", str(name)).strip()
    return "file" if name in ("", ".", "..") else name[:200]


def file_meta(f):
    return {"id": f["id"], "name": f["name"], "size": f["size"], "sha256": f["sha256"]}


def _claim_files(d, ids, task_id, kind="in", enforce_cap=True):
    """Attach uploaded files to a task: domain + existence checked, one task per
    file; kind distinguishes initiator/followup inputs ('in') from worker
    deliverables ('out')."""
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ApiErr(400, "files must be a list of file ids")
    uniq = list(dict.fromkeys(ids))
    metas = []
    for fid in uniq:
        f = q1("SELECT * FROM files WHERE domain_id=? AND id=?", (d, fid))
        if not f:
            raise ApiErr(404, f"no file {fid[:8]}")
        if f["task_id"] and f["task_id"] != task_id:
            raise ApiErr(409, f"file {fid[:8]} already attached to another task")
        metas.append(file_meta(f))
    if enforce_cap:
        have = q1("SELECT COUNT(*) c FROM files WHERE domain_id=? AND task_id=? AND kind='in'", (d, task_id))["c"]
        if have + len(uniq) > CFG["max_files_per_task"]:
            raise ApiErr(400, f"too many files on one task (max {CFG['max_files_per_task']})")
    for fid in uniq:
        x("UPDATE files SET task_id=?, kind=? WHERE id=?", (task_id, kind, fid))
    return metas


# ---------- serialization ----------

def task_json(t, convo=False):
    out = {k: t[k] for k in ("id", "initiator", "target", "text", "status", "fail_reason", "timeout_seconds")}
    out["created_at"] = iso(t["created_at"])
    if convo:  # detail view only: the list endpoint must not pay a files query per row
        fs = q("SELECT * FROM files WHERE domain_id=? AND task_id=? ORDER BY created_at", (t["domain_id"], t["id"]))
        if fs:
            out["files"] = [file_meta(f) for f in fs]
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


def remove_agent(a):
    """Session-guarded agent deletion + owner-binding cleanup (deregister and the
    retention sweep share it). True if this call deleted the row; a re-registered
    row (new session) survives."""
    if not x("DELETE FROM agents WHERE domain_id=? AND agent_id=? AND session=?",
             (a["domain_id"], a["agent_id"], a["session"])):
        return False
    if not q1("SELECT 1 FROM agents WHERE domain_id=? AND owner_username=?", (a["domain_id"], a["owner_username"])):
        x("DELETE FROM owner_bindings WHERE domain_id=? AND username=?", (a["domain_id"], a["owner_username"]))
    WAITERS.pop((a["domain_id"], a["agent_id"]), None)
    return True


# ---------- agent lifecycle ----------

def guard_new(d, b):
    """join-only rule: a code must never take over a live identity —
    register_core REPLACEs the agent row and the owner's notify binding."""
    if q1("SELECT 1 FROM agents WHERE domain_id=? AND agent_id=?", (d, b["agent_id"])):
        raise ApiErr(409, "agent id already registered — kick it first or pick a new id")
    if q1("SELECT 1 FROM agents WHERE domain_id=? AND owner_username=?", (d, b["owner_username"])):
        raise ApiErr(409, "owner username already in use — pick another")


def check_registration(d, b):
    """Pure input/domain checks, no writes: a typo answers 400 and nothing
    changed server-side."""
    for k in ("agent_id", "owner_username", "notify"):
        if k not in b:
            raise ApiErr(400, f"missing {k}")
    if not isinstance(b["agent_id"], str) or b["agent_id"] in (".", "..") \
            or not AGENT_ID_RE.fullmatch(b["agent_id"]):
        raise ApiErr(400, "agent_id must be 1-64 chars of A-Z a-z 0-9 _ . -")
    if not isinstance(b["owner_username"], str) or not b["owner_username"].strip():
        raise ApiErr(400, "owner_username must be a non-empty string")
    if q1("SELECT 1 FROM disabled_agents WHERE domain_id=? AND agent_id=?", (d, b["agent_id"])):
        raise ApiErr(403, "agent disabled by the operator")
    if b.get("accept_policy", "notify_run") not in ("auto", "notify_run", "manual"):
        raise ApiErr(400, "accept_policy must be auto | notify_run | manual")
    af = b.get("accept_from", "all")
    if af != "all" and not (isinstance(af, list) and all(isinstance(i, str) for i in af)):
        raise ApiErr(400, "accept_from must be 'all' or a list of agent ids")
    nb = b["notify"] if isinstance(b["notify"], dict) else {}
    id_type = nb.get("id_type") or "auto"
    if not nb.get("channel") or not isinstance(nb.get("id"), str) or not nb["id"].strip() \
            or not isinstance(id_type, str):
        raise ApiErr(400, "notify must have channel, id_type and id")
    if b.get("driver_kind", "command") not in ("command", "manual"):
        raise ApiErr(400, "driver_kind must be command | manual")
    if not isinstance(b.get("description", ""), str):
        raise ApiErr(400, "description must be a string")
    if nb["channel"] not in notify.CHANNELS:
        raise ApiErr(400, "channel_unavailable")
    if nb["channel"] != domain_channel(d):
        raise ApiErr(400, f"notify.channel must be {domain_channel(d)} — the uniform channel of this domain")


async def register_core(d, b, fresh=False):
    """Shared by /agents/register and /join: validate, verify the notify
    binding, write agent + binding, emit. fresh=True (join): re-check the id
    AFTER the IM awaits — that window is the takeover race."""
    check_registration(d, b)
    af = b.get("accept_from", "all")
    nb = {**b["notify"], "id": b["notify"]["id"].strip(), "id_type": b["notify"].get("id_type") or "auto"}
    ch = notify.CHANNELS[nb["channel"]]
    why = ""
    try:  # both must succeed BEFORE any binding is written — a transient IM
        # failure must never downgrade an existing verified binding
        uid = await asyncio.to_thread(ch.normalize, nb)
        await asyncio.to_thread(ch.send_text, uid,
                                notify.t("register_verify", owner=b["owner_username"], agent=b["agent_id"]))
        verified = 1
    except Exception as e:
        uid, verified, why = "", 0, str(e)
        print(f"[notify] register {b['agent_id']}: {e}", flush=True)
    ts = now()
    sess = declared_session(b) or uuid.uuid4().hex
    prev = q1("SELECT owner_username o FROM agents WHERE domain_id=? AND agent_id=?", (d, b["agent_id"]))
    if fresh:
        guard_new(d, b)  # re-check: the IM-await window is the race
    if q1("SELECT 1 FROM disabled_agents WHERE domain_id=? AND agent_id=?", (d, b["agent_id"])):
        raise ApiErr(403, "agent disabled by the operator")  # re-check after the IM awaits
    DEREGISTERED.pop((d, b["agent_id"]), None)  # an explicit re-register revives the agent
    DB.execute("INSERT OR REPLACE INTO agents "
               "(domain_id, agent_id, owner_username, description, accept_policy, accept_from, "
               "driver_kind, session, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (d, b["agent_id"], b["owner_username"], b.get("description", ""),
                b.get("accept_policy", "notify_run"),
                "all" if af == "all" else json.dumps(af), b.get("driver_kind", "command"), sess, ts))
    old = q1("SELECT verified FROM owner_bindings WHERE domain_id=? AND username=? AND channel=?",
             (d, b["owner_username"], nb["channel"]))
    if verified or not (old and old["verified"]):  # a failed declaration never overwrites a verified binding
        DB.execute("INSERT OR REPLACE INTO owner_bindings "
                   "(domain_id, username, channel, id_type, id, platform_uid, verified) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (d, b["owner_username"], nb["channel"], nb["id_type"], nb["id"], uid, verified))
    if prev and prev["o"] != b["owner_username"] and \
            not q1("SELECT 1 FROM agents WHERE domain_id=? AND owner_username=?", (d, prev["o"])):
        x("DELETE FROM owner_bindings WHERE domain_id=? AND username=?", (d, prev["o"]))
    DB.commit()
    wake(d, b["agent_id"])  # wake any suspended long-poll from a superseded session so it exits 204
    emit(f"reg:{b['agent_id']}", d, "notify_failed" if not verified else "register_verify",
         {"notify_verified": bool(verified), "why": why})
    for t in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status IN ('dispatched','working') "
               "AND lease_session IS NOT NULL AND lease_session != ?", (d, b["agent_id"], sess)):
        await fail_task(t, "worker_restart")
    return {"notify_verified": bool(verified)}


@app.post("/domains/{d}/agents/register")
async def register(d, req: Request):
    domain_of(req, d)
    return await register_core(d, await json_body(req))


@app.post("/domains/{d}/join")
async def join(d, req: Request):
    """Invite-code onboarding for NEW members: the code is the credential
    (revocable); the domain token reaches members only through this call."""
    if d not in DOMAINS:
        raise ApiErr(404, "no domain")
    b = await json_body(req)
    code = b.get("code")
    if not isinstance(code, str) or not code:
        raise ApiErr(400, "missing code")
    if not q1("SELECT 1 FROM invites WHERE code=? AND domain_id=?", (code, d)):
        raise ApiErr(403, "invite invalid or revoked")
    check_registration(d, b)
    guard_new(d, b)
    r = await register_core(d, b, fresh=True)
    r["domain_token"] = str(DOMAINS[d]["token"])
    return r


async def deregister_core(d, aid):
    """Shared by the deregister endpoint (owner-initiated) and operator kick:
    tombstone immediately, fail in-flight, say goodbye, remove the row (session-guarded)."""
    a = require_agent(d, aid)
    DEREGISTERED[(d, aid)] = now() + 180
    wake(d, aid)
    for t in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status NOT IN ('completed','failed','canceled')", (d, aid)):
        await fail_task(t, "agent_deregistered", to="initiator")
    await send_to_owner(d, a["owner_username"], "agent_deregistered", task_id=f"reg:{aid}",
                        ctx={"agent": aid})  # BEFORE deleting the binding, or the goodbye can never be delivered
    if remove_agent(a):
        DB.commit()
        DEREGISTERED[(d, aid)] = now() + 180


@app.post("/domains/{d}/agents/{aid}/deregister")
async def deregister(d, aid, req: Request):
    domain_of(req, d)
    await deregister_core(d, aid)
    return Response(status_code=204)


@app.get("/domains/{d}/agents")
async def list_agents(d, req: Request):
    domain_of(req, d)
    out = []
    for a in q("SELECT * FROM agents WHERE domain_id=?", (d,)):
        b = owner_binding(d, a["owner_username"])
        out.append({"agent_id": a["agent_id"], "owner_username": a["owner_username"], "description": a["description"],
                    "accept_policy": a["accept_policy"], "driver_kind": a["driver_kind"],
                    "online": online(a, now()),
                    "notify_verified": bool(b and b["verified"])})
    return out


# ---------- task lifecycle ----------

async def create_task_core(d, initiator, target_id, text, timeout=None, files=None):
    target = require_agent(d, target_id)
    if not isinstance(text, str) or not text.strip():
        raise ApiErr(400, "text must be a non-empty string")
    if timeout is not None and not (isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0):
        raise ApiErr(400, "timeout must be a positive int")
    tid, ts = uuid.uuid4().hex, now()
    nt = {"id": tid, "domain_id": d, "initiator": initiator, "target": target_id}
    fmeta = _claim_files(d, files or [], tid, kind="in", enforce_cap=True)
    timeout = timeout or CFG["default_task_timeout"]
    convo = json.dumps([{"from": initiator, "text": text, "at": ts, **({"files": fmeta} if fmeta else {})}],
                       ensure_ascii=False)
    allow = target["accept_from"] == "all" or initiator in json.loads(target["accept_from"])
    if not allow:
        DB.execute("INSERT INTO tasks (id,domain_id,initiator,target,text,convo,timeout_seconds,"
                   "created_at,status,fail_reason,available_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (tid, d, initiator, target_id, text, convo, timeout, ts, "failed", "source_not_allowed", ts, ts))
        DB.commit()
        emit(tid, d, "state_change", {"to": "failed", "reason": "source_not_allowed"})
        await notify_task(nt, "task_failed", to="target", reason=notify.reason_desc("source_not_allowed"))
        return tid, "failed"
    if target["accept_policy"] == "manual":
        DB.execute("INSERT INTO tasks (id,domain_id,initiator,target,text,convo,timeout_seconds,"
                   "created_at,status,approval_expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (tid, d, initiator, target_id, text, convo, timeout, ts, "pending-approval",
                    ts + CFG["approval_timeout"]))
        DB.commit()
        emit(tid, d, "state_change", {"to": "pending-approval"})
        await notify_task(nt, "manual_confirm", to="target", agent=target_id, summary=text[:120])
        return tid, "pending-approval"
    DB.execute("INSERT INTO tasks (id,domain_id,initiator,target,text,convo,timeout_seconds,"
               "created_at,status,available_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (tid, d, initiator, target_id, text, convo, timeout, ts, "available", ts))
    DB.commit()
    emit(tid, d, "state_change", {"to": "available"})
    if target["accept_policy"] == "notify_run":
        await notify_task(nt, "task_received", to="target", agent=target_id, summary=text[:120])
    wake(d, target_id)
    return tid, "available"


def followup_core(d, task, text, fmeta=None):
    convo = json.loads(task["convo"])
    convo.append({"from": task["initiator"], "text": text, "at": now(), **({"files": fmeta} if fmeta else {})})
    if x("UPDATE tasks SET status='available', available_at=?, convo=?, ir_at=NULL WHERE id=? AND status='input-required'",
         (now(), json.dumps(convo, ensure_ascii=False), task["id"])) == 0:
        raise ApiErr(409, "task not input-required")
    DB.commit()
    emit(task["id"], d, "state_change", {"to": "available", "via": "followup"})
    wake(d, task["target"])


async def cancel_core(task, reason):
    if x("UPDATE tasks SET status='canceled', fail_reason=?, finished_at=? WHERE id=? AND status NOT IN ('completed','failed','canceled')",
         (reason, now(), task["id"])) == 0:
        raise ApiErr(409, "task already finished")
    DB.commit()
    emit(task["id"], task["domain_id"], "state_change", {"to": "canceled", "reason": reason})
    await notify_task(task, "task_canceled", reason=notify.reason_desc(reason))
    if task["status"] in INFLIGHT:
        wake(task["domain_id"], task["target"])
    return "canceled"


@app.post("/domains/{d}/tasks")
async def create_task(d, req: Request):
    domain_of(req, d)
    b = await json_body(req)
    initiator = req.headers.get("x-agent-id")
    if not initiator:
        raise ApiErr(403, "missing X-Agent-Id")
    if not b.get("target") or not b.get("text"):
        raise ApiErr(400, "missing target/text")
    require_agent(d, initiator)
    tid, status = await create_task_core(d, initiator, b["target"], b["text"], b.get("timeout_seconds"), b.get("files"))
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
            raise ApiErr(403, "missing X-Agent-Id")
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
        raise ApiErr(403, "not initiator")
    return {"status": await cancel_core(task, "canceled_by_initiator")}


@app.post("/domains/{d}/tasks/{tid}/messages")
async def followup(d, tid, req: Request):
    domain_of(req, d)
    b = await json_body(req)
    task = require_task(d, tid)
    if req.headers.get("x-agent-id") != task["initiator"]:
        raise ApiErr(403, "not initiator")
    if not isinstance(b.get("text"), str) or not b["text"].strip():
        raise ApiErr(400, "text must be a non-empty string")
    fmeta = _claim_files(d, b.get("files") or [], tid, kind="in", enforce_cap=False)
    followup_core(d, task, b["text"], fmeta)
    return {"status": "available"}


@app.post("/domains/{d}/tasks/{tid}/action")
async def owner_action(d, tid, req: Request):
    """Owner entry point (approve/reject/abort)."""
    domain_of(req, d)
    body = await json_body(req)
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
             (now(), tid)) == 0:
            raise ApiErr(409, "not pending-approval")
        DB.commit()
        emit(tid, d, "state_change", {"to": "available", "via": "approve"})
        wake(d, task["target"])
        return {"status": "available"}
    if action == "reject":
        if task["status"] != "pending-approval":
            raise ApiErr(409, "not pending-approval")
        await fail_task(task, "rejected")
        return {"status": "failed"}
    if action == "abort":
        return {"status": await cancel_core(task, "canceled_by_owner")}
    raise ApiErr(400, "bad action")


# ---------- worker protocol ----------

@app.post("/domains/{d}/agents/{aid}/poll")
async def poll(d, aid, req: Request):
    domain_of(req, d)
    b = await json_body(req)
    if (exp := DEREGISTERED.get((d, aid), 0)) and now() < exp:
        raise ApiErr(410, "deregistered")
    agent = require_agent(d, aid)
    reg_session = agent["session"]
    poller = declared_session(b) or reg_session
    if poller != reg_session:
        p8 = poller[:8]
        if not q1("SELECT 1 FROM task_events WHERE domain_id=? AND task_id=? AND type='dual_poller' "
                  "AND payload LIKE ?", (d, f"reg:{aid}", f'%"{p8}"%')):
            emit(f"reg:{aid}", d, "dual_poller", {"poller": p8})
    st = b.get("exec_state") if isinstance(b.get("exec_state"), dict) else {"tasks": []}
    seen = [t for t in st.get("tasks", []) if isinstance(t, dict)
            and isinstance(t.get("task_id"), str) and t.get("task_id")]

    x("UPDATE agents SET last_seen_at=? WHERE domain_id=? AND agent_id=?", (now(), d, aid))
    DB.commit()
    for t in seen:
        if t.get("phase") != "running":
            continue
        row = q1("SELECT lease_id FROM tasks WHERE domain_id=? AND id=? AND target=? AND status='dispatched'",
                 (d, t["task_id"], aid))
        if row and row["lease_id"] and row["lease_id"] == t.get("lease_id"):
            exp = t.get("expected_seconds")
            x("UPDATE tasks SET status='working', started_at=?, expected_seconds=? WHERE id=? AND status='dispatched'",
              (now(), exp if isinstance(exp, int) and not isinstance(exp, bool) and exp > 0 else None, t["task_id"]))
            emit(t["task_id"], d, "state_change", {"to": "working"})
    for t in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status IN ('dispatched','working')", (d, aid)):
        if t["id"] not in {s["task_id"] for s in seen} and t["lease_session"] == poller \
                and now() - t["dispatched_at"] > CFG["dispatch_grace"]:
            await fail_task(t, "worker_restart")

    async def deliver():
        if STOPPING:
            return None
        a = q1("SELECT session, driver_kind FROM agents WHERE domain_id=? AND agent_id=?", (d, aid))
        if not a or a["session"] != reg_session:
            return None  # deregistered or re-registered while this poll was suspended
        seen_ids = {s["task_id"] for s in seen}
        cancels = [t["id"] for t in q("SELECT id FROM tasks WHERE domain_id=? AND target=? AND status IN ('canceled','failed')", (d, aid))
                   if t["id"] in seen_ids]
        if cancels:
            return {"control": {"cancel": cancels}}
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
        if a["driver_kind"] == "manual":
            await notify_task(t, "manual_dispatch", to="target", summary=t["text"][:200])
        fs = q("SELECT * FROM files WHERE domain_id=? AND task_id=? AND kind='in' ORDER BY created_at", (d, row["id"]))
        return {"task": {"id": t["id"], "lease_id": lease, "text": t["text"], "initiator": t["initiator"],
                         "timeout_seconds": t["timeout_seconds"], "convo": json.loads(t["convo"]),
                         "files": [file_meta(f) for f in fs]}}

    if (r := await deliver()) is not None:
        return r
    if not b.get("block", True) or STOPPING:
        return Response(status_code=204)
    wake(d, aid)
    ev = WAITERS[(d, aid)] = asyncio.Event()
    try:
        await asyncio.wait_for(ev.wait(), timeout=CFG["poll_wait"])
    except asyncio.TimeoutError:
        return Response(status_code=204)
    if await req.is_disconnected():
        return Response(status_code=204)
    return (r := await deliver()) or Response(status_code=204)


@app.post("/domains/{d}/agents/{aid}/results")
async def results(d, aid, req: Request):
    domain_of(req, d)
    b = await json_body(req)
    tid = b.get("task_id")
    task = q1("SELECT * FROM tasks WHERE domain_id=? AND id=?", (d, tid)) if isinstance(tid, str) else None
    lease_ok = task and task["target"] == aid and b.get("lease_id") and task["lease_id"] == b["lease_id"]
    if lease_ok and b.get("status") == "input-required" and task["status"] == "available":
        return {"accepted": True, "duplicate": True}  # IR retry raced a follow-up: applied, task requeued
    if lease_ok and task["status"] == b.get("status") and not (
            task["status"] == "failed" and b.get("fail_reason") != task["fail_reason"]):
        return {"accepted": True, "duplicate": True}  # retry after a lost response: already applied
    if not (lease_ok and task["status"] in ("dispatched", "working")):
        if task and not q1("SELECT 1 FROM task_events WHERE domain_id=? AND task_id=? AND type='late_result'", (d, task["id"])):
            emit(task["id"], d, "late_result", {"from_agent": aid})
            await notify_task(task, "late_result")
        return {"accepted": False, "reason": "late_result"}
    status = b.get("status")
    out_text = str(b.get("output") or "")
    fmeta = _claim_files(d, b.get("files") or [], task["id"], kind="out", enforce_cap=False)
    if status == "completed":
        convo = json.loads(task["convo"])
        convo.append({"from": aid, "text": out_text, "at": now(),
                      **({"files": fmeta} if fmeta else {})})
        x("UPDATE tasks SET status='completed', convo=?, finished_at=? WHERE id=? AND status IN ('dispatched','working')",
          (json.dumps(convo, ensure_ascii=False), now(), task["id"]))
        emit(task["id"], d, "state_change", {"to": "completed"})
        await notify_task(task, "task_completed", output_head=out_text[:400])
    elif status == "failed":
        reason = b.get("fail_reason")
        await fail_task(task, reason if isinstance(reason, str) and reason else "driver_error",
                        output_head=out_text, files=fmeta)
    elif status == "input-required":
        question = str(b.get("question") or "")
        convo = json.loads(task["convo"])
        convo.append({"from": aid, "text": question, "at": now(),
                      **({"files": fmeta} if fmeta else {})})
        x("UPDATE tasks SET status='input-required', ir_at=?, convo=? WHERE id=? AND status IN ('dispatched','working')",
          (now(), json.dumps(convo, ensure_ascii=False), task["id"]))
        DB.commit()
        emit(task["id"], d, "state_change", {"to": "input-required"})
        await notify_task(task, "input_required", to="initiator", question=question)
    else:
        raise ApiErr(400, "bad status")
    wake(d, aid)
    return {"accepted": True}


# ---------- file staging ----------

@app.post("/domains/{d}/files")
async def upload_file(d, req: Request, name=""):
    """Raw-body upload; the response (and any task/result payload) carries the
    meta: id, name, size, sha256. Bytes never travel inside task messages."""
    domain_of(req, d)
    name = _safe_name(name)
    cap = CFG["max_file_mb"] * 1024 * 1024
    if (cl := req.headers.get("content-length") or "").isdigit() and int(cl) > cap:
        raise ApiErr(413, f"file larger than {CFG['max_file_mb']}MB")
    buf = bytearray()
    async for chunk in req.stream():  # chunked bodies bypass content-length: count bytes as they land
        buf += chunk
        if len(buf) > cap:
            raise ApiErr(413, f"file larger than {CFG['max_file_mb']}MB")
    if not buf:
        raise ApiErr(400, "empty file")
    fid, ts = uuid.uuid4().hex, now()
    path = FILE_DIR / d / fid

    def _store():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buf)
        return hashlib.sha256(buf).hexdigest()

    sha = await asyncio.to_thread(_store)  # big writes must not stall the loop
    DB.execute("INSERT INTO files (id,domain_id,task_id,name,size,sha256,created_at) "
               "VALUES (?,?,NULL,?,?,?,?)", (fid, d, name, len(buf), sha, ts))
    DB.commit()
    return {"id": fid, "name": name, "size": len(buf), "sha256": sha}


def _send_file(f):
    if not f:
        raise ApiErr(404, "no file")
    path = FILE_DIR / f["domain_id"] / f["id"]
    if not path.exists():
        raise ApiErr(404, "file data expired")
    return FileResponse(path, media_type="application/octet-stream", filename=f["name"],
                        headers={"X-File-SHA256": f["sha256"]})  # size rides on Content-Length


@app.get("/domains/{d}/files/{fid}")
async def download_file(d, fid, req: Request):
    domain_of(req, d)
    return _send_file(q1("SELECT * FROM files WHERE domain_id=? AND id=?", (d, fid)))


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
                    offline = not online(a, t)
                    for task in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status IN (?,?,?)",
                                  (a["domain_id"], a["agent_id"], *INFLIGHT)):
                        if task["status"] == "input-required":
                            # IR waits on the initiator's follow-up, not the worker: only
                            # input_timeout may kill it — worker silence (restart, sleep)
                            # during the question window is normal
                            if task["ir_at"] and task["ir_at"] + CFG["input_required_timeout"] < t:
                                await fail_task(task, "input_timeout")
                        elif offline:
                            await fail_task(task, "worker_offline")
                        elif task["status"] == "working" and task["started_at"] + (task["expected_seconds"] or 2 * task["timeout_seconds"]) + 120 < t:
                            await fail_task(task, "timeout_stale")
                        elif task["status"] == "dispatched" and task["dispatched_at"] + 2 * CFG["dispatch_grace"] < t:
                            await fail_task(task, "receive_timeout")
                except Exception as e:
                    DB.rollback()
                    print(f"[sweep] skip agent {a['agent_id']}: {e}", flush=True)
            for task in q("SELECT * FROM tasks WHERE status='pending-approval' AND approval_expires_at < ?", (t,)):
                await fail_task(task, "approval_expired")
            for a in q("SELECT * FROM agents WHERE last_seen_at IS NULL OR last_seen_at < ?", (t - CFG["agent_retention"],)):
                for task in q("SELECT * FROM tasks WHERE domain_id=? AND target=? AND status NOT IN ('completed','failed','canceled')",
                              (a["domain_id"], a["agent_id"])):
                    await fail_task(task, "agent_deregistered", to="initiator")
                await send_to_owner(a["domain_id"], a["owner_username"], "agent_cleaned",
                                    task_id=f"reg:{a['agent_id']}",
                                    ctx={"agent": a["agent_id"], "days": int(CFG["agent_retention"] // 86400)})
                # the notify above awaits: a worker that re-registered in that window
                # keeps its fresh row (remove_agent is session-guarded)
                remove_agent(a)
                DB.commit()  # before the file/task cleanup below: a failure there must
                # roll back only that cleanup, never resurrect this agent (and re-notify)
            cutoff = t - CFG["task_retention"]
            DB.execute("DELETE FROM tasks WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
            for f in q("DELETE FROM files WHERE created_at < ? AND (task_id IS NULL OR task_id NOT IN (SELECT id FROM tasks)) RETURNING id, domain_id", (cutoff,)):
                (FILE_DIR / f["domain_id"] / f["id"]).unlink(missing_ok=True)
            DB.execute("DELETE FROM task_events WHERE created_at < ? AND task_id NOT IN (SELECT id FROM tasks)", (cutoff,))
            DB.commit()
        except Exception as e:
            DB.rollback()
            print(f"[sweep] error: {e}", flush=True)


# ---------- admin console + operator actions ----------

@app.get("/admin", response_class=HTMLResponse)
async def admin():
    return ADMIN_HTML


@app.get("/admin/data")
async def admin_data():
    t = now()
    agents = []
    for a in q("SELECT * FROM agents ORDER BY domain_id, agent_id"):
        b = owner_binding(a["domain_id"], a["owner_username"])
        agents.append({"domain": a["domain_id"], "agent_id": a["agent_id"], "owner": a["owner_username"],
                       "description": a["description"], "accept_policy": a["accept_policy"],
                       "driver_kind": a["driver_kind"],
                       "online": online(a, t),
                       "notify_verified": bool(b and b["verified"])})
    tasks, domains = [], []
    for dm in CFG["domains"]:
        did = dm["id"]
        tasks.extend({"id": k["id"], "domain": k["domain_id"], "initiator": k["initiator"], "target": k["target"],
                      "status": k["status"], "fail_reason": k["fail_reason"], "created_at": iso(k["created_at"]),
                      "finished_at": iso(k["finished_at"]) if k["finished_at"] else None}
                     for k in q("SELECT * FROM tasks WHERE domain_id=? ORDER BY created_at DESC LIMIT 100", (did,)))
        ags = [a for a in agents if a["domain"] == did]
        last = q1("SELECT MAX(created_at) m FROM task_events WHERE domain_id=?", (did,))["m"]
        domains.append({"id": did, "channel": dm.get("channel") or CFG["default_channel"],
                        "agents": len(ags), "online": sum(1 for a in ags if a["online"]),
                        "inflight": q1("SELECT COUNT(*) c FROM tasks WHERE domain_id=? AND status IN (?,?,?)",
                                       (did, *INFLIGHT))["c"],
                        "last_event": iso(last) if last else None})
    invites = [{"code": i["code"], "domain": i["domain_id"], "note": i["note"]}
               for i in q("SELECT * FROM invites ORDER BY created_at DESC")]
    disabled = [{"domain": b["domain_id"], "agent_id": b["agent_id"]}
                for b in q("SELECT domain_id, agent_id FROM disabled_agents ORDER BY agent_id")]
    return {"public_base": CFG["public_base"], "agents": agents, "tasks": tasks,
            "domains": domains, "invites": invites, "disabled": disabled}


@app.post("/admin/op")
async def admin_op(req: Request):
    """Operator actions, one endpoint: invite | invite_revoke | kick | disable
    | enable | task_abort. kick = deregister; disable = kick + deny
    re-registration."""
    b = await json_body(req)
    op, d = b.get("op"), b.get("domain")
    if any(v is not None and not isinstance(v, (str, int, float, bool)) for v in b.values()):
        raise ApiErr(400, "values must be scalars")  # non-scalars break the SQL
    if op == "invite":
        if d not in DOMAINS:
            raise ApiErr(404, "no domain")
        code = "A2A-" + "".join(secrets.choice(INVITE_ABC) for _ in range(8))  # 31^8: grinding is hopeless
        DB.execute("INSERT INTO invites (code,domain_id,note,created_at) VALUES (?,?,?,?)",
                   (code, d, str(b.get("note") or "")[:200], now()))
        DB.commit()
        return {"code": code}
    if op == "invite_revoke":
        if not x("DELETE FROM invites WHERE code=?", (b.get("code"),)):
            raise ApiErr(404, "no invite")
        DB.commit()
        return {"ok": True}
    if op in ("kick", "disable"):
        aid = b.get("agent_id")
        if op == "disable":  # BEFORE the kick: re-registers in the kick's awaits must 403
            require_agent(d, aid)
            DB.execute("INSERT OR REPLACE INTO disabled_agents (domain_id,agent_id) VALUES (?,?)", (d, aid))
            DB.commit()
        await deregister_core(d, aid)  # kick: the 404 comes from deregister_core
        return {"ok": True}
    if op == "enable":
        x("DELETE FROM disabled_agents WHERE domain_id=? AND agent_id=?", (d, b.get("agent_id")))
        DB.commit()
        return {"ok": True}
    if op == "task_abort":
        return {"status": await cancel_core(require_task(d, b.get("task_id")), "canceled_by_operator")}
    raise ApiErr(400, "bad op")


@app.get("/admin/task/{tid}")
async def admin_task(tid):
    task = q1("SELECT * FROM tasks WHERE id=?", (tid,))
    if not task:
        raise ApiErr(404, "no task")
    return task_json(task, convo=True)  # full convo + events: the interaction record for debugging


@app.get("/admin/file/{fid}")
async def admin_file(fid):
    return _send_file(q1("SELECT * FROM files WHERE id=?", (fid,)))


if __name__ == "__main__":
    import uvicorn
    for _name in ("feishu", "dingtalk", "wecom"):
        if CFG.get(_name) and _name not in notify.CHANNELS:
            print(f"[server] WARNING: {_name} config incomplete; channel disabled "
                  f"(complete the credentials or drop the section)", flush=True)
    if not CFG["public_base"] or any(s in CFG["public_base"] for s in ("127.0.0.1", "localhost")):
        print("[server] WARNING: public_base is empty or loopback; links in notification "
              "texts will not open on other machines — set the server's LAN address", flush=True)
    uvicorn.run(app, host=CFG["host"], port=int(CFG["port"]), log_level="warning")
