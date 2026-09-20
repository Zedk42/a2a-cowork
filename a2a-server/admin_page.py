"""Read-only admin console. One static HTML page (this file) + three JSON
endpoints in server.py: /admin/data (snapshot), /admin/task/{id} (full convo and
events). Vanilla JS, no build step, no external assets (works fully offline on
an intranet); everything user-supplied is rendered via textContent, so no HTML
escaping is needed. Visual language: warm paper, ink text, one coral accent,
white cards with hairline borders — no shadows, no dark-tech styling."""

ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>A2A Co-Work</title>
<style>
:root{
  --paper:#f5f5f5; --card:#ffffff; --ink:#2d3142; --muted:#4f5d75; --soft:#7a8399;
  --rule:rgba(45,49,66,.12); --rule-soft:rgba(45,49,66,.07);
  --accent:#eb6c36;
  --green:#3e7d4e; --green-tint:rgba(62,125,78,.09);
  --red:#b0423a; --red-tint:rgba(176,66,58,.08);
  --amber:#9c6b1f; --amber-tint:rgba(156,107,31,.10);
  --blue:#2e5aa8; --blue-tint:rgba(46,90,168,.08);
  --gray-tint:rgba(45,49,66,.06);
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
     font:14px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:1160px;margin:0 auto;padding:32px 32px 48px}
header{display:flex;justify-content:space-between;align-items:flex-end;
       border-bottom:1px solid var(--rule);padding-bottom:16px}
.eyebrow{font:600 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.14em;
         text-transform:uppercase;color:var(--soft);margin:0 0 8px}
h1{font:400 28px/1 "Iowan Old Style",Georgia,"Times New Roman",serif;margin:0}
.live{display:flex;align-items:center;gap:8px;font:11px ui-monospace,Menlo,monospace;color:var(--soft)}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--accent);animation:pulse 2.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}}
section{margin-top:28px}
.sec-head{display:flex;align-items:baseline;gap:12px;margin:0 0 8px}
h2{font-weight:600;font-size:15px;line-height:1.2;margin:0;font-family:inherit}
.note{font:11px ui-monospace,Menlo,monospace;color:var(--soft)}
.card{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:4px 20px 12px}
table{border-collapse:collapse;width:100%}
th{font:600 9px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;
   color:var(--soft);text-align:left;padding:12px 12px 7px;border-bottom:1px solid var(--rule)}
td{padding:9px 12px;border-bottom:1px solid var(--rule-soft);vertical-align:top;
   font-size:13px;color:var(--muted)}
tr:last-child td, tr:last-child th{border-bottom:none}
td .name{font-weight:600;color:var(--ink)}
tr.tk{cursor:pointer}
tr.tk:hover td{background:rgba(45,49,66,.03)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:0}
.dot.on{background:var(--green)} .dot.off{background:#c2c8d2}
.tag{display:inline-block;font:600 9px/1 ui-monospace,Menlo,monospace;letter-spacing:.06em;
     text-transform:uppercase;padding:3px 7px;border-radius:2px;border:1px solid}
.tag.green{color:var(--green);background:var(--green-tint);border-color:rgba(62,125,78,.35)}
.tag.red{color:var(--red);background:var(--red-tint);border-color:rgba(176,66,58,.35)}
.tag.amber{color:var(--amber);background:var(--amber-tint);border-color:rgba(156,107,31,.35)}
.tag.blue{color:var(--blue);background:var(--blue-tint);border-color:rgba(46,90,168,.35)}
.tag.gray{color:var(--soft);background:var(--gray-tint);border-color:rgba(45,49,66,.2)}
#detail{display:none;background:var(--card);border:1px solid var(--rule);border-left:2px solid var(--accent);
        border-radius:6px;margin-top:24px;padding:12px 16px;max-height:28vh;overflow-y:auto;
        font:12px/1.7 ui-monospace,Menlo,monospace;color:var(--muted);white-space:pre-wrap;word-break:break-all}
#log{height:44vh;overflow-y:auto;font:12px/1.75 ui-monospace,Menlo,monospace;color:var(--muted)}
#log .t{color:var(--soft)}
#log .et-ok{color:var(--green)} #log .et-bad{color:var(--red)}
#log .et-warn{color:var(--amber)} #log .et-mut{color:var(--soft)}
</style></head><body>
<div class="wrap">
<header>
  <div>
    <p class="eyebrow">A2A Co-Work &middot; read-only console</p>
    <h1>Team Console</h1>
  </div>
  <div class="live"><span class="pulse"></span><span id="log-note">loading&hellip;</span></div>
</header>

<section>
  <div class="sec-head"><h2>Agents</h2><span class="note" id="agen-note"></span></div>
  <div class="card"><table id="agents"></table></div>
</section>

<section>
  <div class="sec-head"><h2>Recent tasks</h2><span class="note">click a row for the full conversation &amp; events</span></div>
  <div class="card"><table id="tasks"></table></div>
</section>

<div id="detail"></div>

<section>
  <div class="sec-head"><h2>Event log</h2><span class="note">newest at bottom &middot; auto-refresh 3s</span></div>
  <div class="card"><div id="log"></div></div>
</section>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = s => document.querySelector(s);
const log = $("#log"), detail = $("#detail");
let maxN = 0, first = true;

function span(cls, text) {
  const s = document.createElement("span");
  if (cls) s.className = cls;
  if (text) s.textContent = text;
  return s;
}

function row(table, cells, header) {
  const tr = document.createElement("tr");
  for (const cell of cells) {
    const td = document.createElement(header ? "th" : "td");
    for (const item of [].concat(cell)) td.appendChild(typeof item === "string" ? document.createTextNode(item) : item);
    tr.appendChild(td);
  }
  table.appendChild(tr);
  return tr;
}

const TAG = {completed: "green", "input-required": "amber", "pending-approval": "amber",
             working: "blue", dispatched: "blue", failed: "red", canceled: "gray", available: "gray"};

function renderAgents(list) {
  const t = $("#agents"); t.innerHTML = "";
  row(t, ["domain","agent","owner","status","policy","driver","notify","description"], true);
  $("#agen-note").textContent = `(${list.filter(a => a.online).length}/${list.length} online)`;
  for (const a of list)
    row(t, [a.domain, span("name", a.agent_id), a.owner,
            [span(a.online ? "dot on" : "dot off"), a.online ? "online" : "offline"],
            a.accept_policy, a.default_driver,
            span(a.notify_verified ? "tag green" : "tag gray", a.notify_verified ? "ok" : "unverified"),
            a.description]);
}

function renderTasks(list) {
  const t = $("#tasks"); t.innerHTML = "";
  row(t, ["created","id","domain","from","to","status","reason"], true);
  for (const k of list) {
    const tr = row(t, [k.created_at, span("name", k.id.slice(0,8)), k.domain, k.initiator, k.target,
                       span("tag " + (TAG[k.status] || "gray"), k.status), k.fail_reason || ""]);
    tr.classList.add("tk");
    tr.onclick = () => showTask(k.id);
  }
}

async function showTask(id) {
  detail.style.display = "block";
  detail.textContent = `loading ${id} …`;
  try {
    const r = await fetch(`/admin/task/${id}?token=${encodeURIComponent(TOKEN)}`);
    const d = await r.json();
    if (!r.ok) { detail.textContent = JSON.stringify(d); return; }
    const lines = [`${d.id}  ${d.initiator} -> ${d.target}  ${d.status}${d.fail_reason ? " (" + d.fail_reason + ")" : ""}`];
    for (const m of d.convo) lines.push(`[${m.at}] ${m.from}: ${m.text}`);
    for (const e of d.events) lines.push(`[${e.created_at}] #${e.seq} ${e.type} ${e.payload}`);
    detail.textContent = lines.join("\\n");
  } catch (e) {
    detail.textContent = `failed to load ${id}: ${e}`;
  }
}

const EVT = {notify: "et-ok", register_verify: "et-ok", notify_failed: "et-bad",
             late_result: "et-warn", state_change: "et-mut"};

function appendLog(events) {
  if (!events.length) return;
  // pin to the newest line unless the reader scrolled up to inspect history
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  for (const e of events) {
    if (e.n <= maxN) continue;
    const div = document.createElement("div");
    div.append(span("t", `[${e.at}] `), `${e.domain} ${e.task_id.slice(0,12)} #${e.seq} `,
               span(EVT[e.type] || "", e.type), ` ${e.payload}`);
    log.appendChild(div);
    maxN = e.n;
  }
  while (log.childNodes.length > 1000) log.removeChild(log.firstChild);
  if (first || atBottom) log.scrollTop = log.scrollHeight;
  first = false;
}

async function poll() {
  try {
    const r = await fetch(`/admin/data?token=${encodeURIComponent(TOKEN)}`);
    if (!r.ok) throw new Error(r.status === 401 ? "unauthorized — wrong admin token" : `HTTP ${r.status}`);
    const d = await r.json();
    renderAgents(d.agents);
    renderTasks(d.tasks);
    appendLog(d.events);
    $("#log-note").textContent = "auto-refresh 3s";
  } catch (e) {
    $("#log-note").textContent = `problem: ${e.message || e}`;
  }
}
poll();
setInterval(poll, 3000);
</script></body></html>
"""
