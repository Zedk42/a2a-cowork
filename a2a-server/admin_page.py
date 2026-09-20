"""Read-only admin console. One static HTML page (this file) + three JSON
endpoints in server.py: /admin/data (snapshot), /admin/task/{id} (full convo and
events). Vanilla JS, no build step, no dependencies; everything user-supplied is
rendered via textContent, so no HTML escaping is needed."""

ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>A2A Co-Work</title>
<style>
 body{font:13px/1.5 -apple-system,"Segoe UI",sans-serif;margin:16px;color:#111;background:#fff}
 table{border-collapse:collapse;margin:6px 0 14px}
 td,th{border:1px solid #ccc;padding:2px 8px;text-align:left;vertical-align:top}
 th{background:#f3f3f3}
 .on{color:#080}.off{color:#c00}
 h1,h2{margin:14px 0 4px}.small{color:#777;font-weight:normal}
 #log,#detail{border:1px solid #ccc;padding:6px;background:#fafafa;
   font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;overflow-y:auto}
 #log{height:42vh}#detail{display:none;max-height:26vh;margin-bottom:12px}
 tr.tk{cursor:pointer}tr.tk:hover{background:#eef}
</style></head><body>
<h1>A2A Co-Work</h1>
<h2>Agents <span class="small" id="agen-note"></span></h2><table id="agents"></table>
<h2>Recent tasks <span class="small">(click a row for full convo &amp; events)</span></h2><table id="tasks"></table>
<div id="detail"></div>
<h2>Event log <span class="small" id="log-note">loading…</span></h2>
<div id="log"></div>
<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = s => document.querySelector(s);
const log = $("#log"), detail = $("#detail");
let maxN = 0, first = true;

function row(table, cells) {
  const tr = document.createElement("tr");
  for (const [text, cls] of cells) {
    const td = document.createElement("td");
    td.textContent = text == null ? "" : text;
    if (cls) td.className = cls;
    tr.appendChild(td);
  }
  table.appendChild(tr);
  return tr;
}

function renderAgents(list) {
  const t = $("#agents"); t.innerHTML = "";
  row(t, ["domain","agent","owner","online","policy","driver","notify","description"].map(h => [h]));
  $("#agen-note").textContent = `(${list.filter(a => a.online).length}/${list.length} online)`;
  for (const a of list)
    row(t, [[a.domain],[a.agent_id],[a.owner],
            [a.online ? "online" : "offline", a.online ? "on" : "off"],
            [a.accept_policy],[a.default_driver],
            [a.notify_verified ? "ok" : "unverified", a.notify_verified ? "on" : "off"],
            [a.description]]);
}

function renderTasks(list) {
  const t = $("#tasks"); t.innerHTML = "";
  row(t, ["created","id","domain","from","to","status","reason"].map(h => [h]));
  for (const k of list) {
    const tr = row(t, [[k.created_at],[k.id.slice(0,8)],[k.domain],[k.initiator],[k.target],
                       [k.status],[k.fail_reason]]);
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

function appendLog(events) {
  if (!events.length) return;
  // pin to the newest line unless the reader scrolled up to inspect history
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  for (const e of events) {
    if (e.n <= maxN) continue;
    const div = document.createElement("div");
    div.textContent = `[${e.at}] ${e.domain} ${e.task_id.slice(0,12)} #${e.seq} ${e.type} ${e.payload}`;
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
