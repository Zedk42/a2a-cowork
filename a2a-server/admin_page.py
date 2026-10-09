r"""Admin console. One static HTML page (this file) + the JSON endpoints in
server.py: /admin/data (snapshot), /admin/task/{id} (full convo and events),
and /admin/op (operator actions: invite / invite_revoke / kick / disable /
enable / task_abort). Vanilla JS + hand-drawn SVG, no build step, no external assets
(works fully offline on an intranet); everything user-supplied is rendered via
textContent, so no HTML escaping is needed. Unauthenticated by design: it
serves a trusted LAN. UI is bilingual (EN/中文, toggled in the header)."""

ADMIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A2A Co-Work</title>
<style>
:root{
  --bg:#f2f4f7; --card:#ffffff; --ink:#101828; --muted:#475467; --soft:#667085;
  --line:#eaecf0; --line-strong:#d0d5dd;
  --accent:#155eef; --accent-dk:#1149d6; --accent-tint:rgba(21,94,239,.12);
  --green:#067647; --green-tint:#ecfdf3; --green-bd:#abefc6;
  --red:#d92d20; --red-tint:#fef3f2; --red-bd:#fecdca;
  --amber:#dc6803; --amber-tint:#fffaeb; --amber-bd:#fef0c7;
  --blue:#175cd3; --blue-tint:#eff4ff; --blue-bd:#d1e0ff;
  --gray-tint:#f2f4f7; --gray-bd:#e4e7ec;
  --mono:ui-monospace,"SF Mono","Cascadia Code",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",system-ui,"Helvetica Neue",sans-serif;
  --shadow:0 1px 2px rgba(16,24,40,.05);
  --shadow-lg:0 12px 32px rgba(16,24,40,.16);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:1160px;margin:0 auto;padding:24px 32px 48px}
header{position:sticky;top:0;z-index:20;background:var(--card);height:56px;padding:0 32px;
       display:flex;justify-content:space-between;align-items:center;
       border-bottom:1px solid var(--line)}
.brand{display:inline-flex;align-items:center;gap:8px}
.brand .bname{font:500 15px/1 var(--sans);color:var(--soft)}
.brand .mark{font:700 16px/1 var(--sans);letter-spacing:-.03em;color:var(--ink)}
.brand .mark i{font-style:normal;color:var(--accent)}
.live{display:flex;align-items:center;gap:8px;font:12px var(--sans);color:var(--soft)}
.hright{display:flex;align-items:center;gap:16px}
.btn.lang{padding:5px 11px;font-size:12px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--accent);animation:pulse 2.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}}
section{margin-top:28px}
.sec-head{display:flex;align-items:center;gap:12px;margin:0 0 10px}
h2{font:600 16px/1.3 var(--sans);margin:0}
.note{font:12px var(--sans);color:var(--soft)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
      padding:4px 20px 12px;box-shadow:var(--shadow)}
.card.tbl{overflow-x:auto}  /* tables scroll, never spill; must not clip the topo panel */
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th{font:500 12px/1.4 var(--sans);color:var(--soft);text-align:left;
   padding:12px 12px 8px;border-bottom:1px solid var(--line)}
.card.tbl th:last-child,.card.tbl td:last-child{width:1%;white-space:nowrap}  /* compact actions */
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top;
   font-size:13px;color:var(--muted)}
tr:last-child td,tr:last-child th{border-bottom:none}
th,td .name,td .mono,.tag{white-space:nowrap}
td .name{font-weight:500;color:var(--ink);font-family:var(--mono);font-size:12.5px}
td .mono{font-family:var(--mono);font-size:12.5px}
tr.tk,tr.arow{cursor:pointer}
tr.tk:hover td,tr.arow:hover td{background:#f9fafb}
tr.brow td{background:var(--red-tint)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}
.dot.on{background:#12b76a} .dot.off{background:#d0d5dd}
.tag{display:inline-flex;align-items:center;font:500 11px/1 var(--sans);
     padding:3px 9px;border-radius:999px;border:1px solid}
.tag.green{color:var(--green);background:var(--green-tint);border-color:var(--green-bd)}
.tag.red{color:var(--red);background:var(--red-tint);border-color:var(--red-bd)}
.tag.amber{color:var(--amber);background:var(--amber-tint);border-color:var(--amber-bd)}
.tag.blue{color:var(--blue);background:var(--blue-tint);border-color:var(--blue-bd)}
.tag.gray{color:var(--muted);background:var(--gray-tint);border-color:var(--gray-bd)}
/* overview */
.domains{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:20px;margin-top:20px}
.dcard{background:var(--card);border:1px solid var(--line);border-radius:12px;
       padding:20px 22px 18px;cursor:pointer;box-shadow:var(--shadow);
       transition:border-color .15s ease,box-shadow .15s ease}
.dcard:hover{border-color:var(--line-strong);box-shadow:0 4px 12px rgba(16,24,40,.08)}
.dmeta{display:flex;justify-content:space-between;align-items:center;gap:10px}
.dname{font:600 20px/1.2 var(--sans);letter-spacing:-.01em}
.dstats{display:flex;gap:28px}
.dcard .dstats{border-top:1px solid var(--line);padding-top:14px;margin-top:16px}
.dstat .n{font:600 20px/1 var(--sans);font-variant-numeric:tabular-nums;color:var(--ink)}
.dstat .n.hot{color:var(--accent)}
.dstat .l{font:400 12px/1 var(--sans);color:var(--soft);margin-top:6px}
/* domain detail */
.back{display:inline-flex;align-items:center;gap:6px;font:13px var(--sans);
      color:var(--soft);text-decoration:none}
.back:hover{color:var(--ink)}
.domhead{display:flex;align-items:center;gap:14px;margin-top:8px;flex-wrap:wrap}
.dtitle{font:600 24px/1 var(--sans);letter-spacing:-.01em}
.domhead .dstats{margin-left:auto}
/* topology */
.topo > svg{display:block;width:100%;height:auto}  /* child only, or the panel icons inherit it */
.orbit{fill:none;stroke:var(--line-strong);stroke-dasharray:2 7;opacity:.55}
.slink{stroke:#d0d5dd;stroke-width:1.5}
.slink.on{stroke:var(--accent);stroke-opacity:.45}
.tnode{cursor:pointer}
.tnode rect.body{fill:var(--card);stroke:var(--line-strong);stroke-width:1.25}
.tnode:hover rect.body,.tnode.sel rect.body{stroke:var(--accent);stroke-width:1.5}
.tava{fill:var(--blue-tint);stroke-width:1.5}
.tava.on{stroke:#12b76a}
.tava.off{stroke:#d0d5dd}
.tinit{font:600 10px var(--sans);fill:var(--accent)}
.tname{font:500 11px var(--mono);fill:var(--ink)}
.towner{font:10px var(--mono);fill:var(--soft)}
.tsrv{font:500 11px var(--mono);fill:#fff}
.tbadge{font:600 10px var(--mono);fill:#fff}
.srv rect{fill:var(--accent)}
.tpanel{margin-top:22px;border-top:1px solid var(--line);padding-top:16px;
        display:flex;gap:30px;flex-wrap:wrap;align-items:flex-start}
.tpanel[hidden]{display:none}
.tp-name{font:500 15px/1.2 var(--mono);margin-right:10px}
.tp-meta{display:grid;grid-template-columns:auto auto;gap:3px 16px;font:12px var(--sans);color:var(--muted)}
.tp-meta .k{color:var(--soft)}
.tp-desc{flex-basis:100%;margin:0;font-size:13px;color:var(--muted)}
.tp-tasks{margin:6px 0 0;width:auto}
.tp-tasks td{padding:5px 10px}
.tp-acts{margin-left:auto;display:flex;gap:8px}
/* controls */
.btn{display:inline-flex;align-items:center;gap:7px;font:500 13px/1 var(--sans);
     padding:7px 13px;border-radius:8px;border:1px solid var(--line-strong);
     background:var(--card);color:#344054;cursor:pointer;transition:.12s}
.btn:hover{background:#f9fafb}
.btn:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.btn.pri{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn.pri:hover{background:var(--accent-dk);border-color:var(--accent-dk)}
.btn.danger:hover{border-color:var(--red);color:var(--red)}
.btn svg{flex:none}
td .btn + .btn{margin-left:8px}  /* DOM-built buttons have no whitespace between them */
/* task drill-down */
#detail{display:none;background:var(--card);border:1px solid var(--line);border-left:2px solid var(--accent);
        border-radius:8px;margin-top:24px;padding:12px 16px;max-height:28vh;overflow-y:auto;
        font:12px/1.7 var(--mono);color:var(--muted);white-space:pre-wrap;word-break:break-all}
/* dialog + toast */
dialog{border:1px solid var(--line);border-radius:12px;padding:22px 24px 18px;
       background:var(--card);color:var(--ink);max-width:460px;width:92%;box-shadow:var(--shadow-lg)}
dialog::backdrop{background:rgba(16,24,40,.4)}
dialog h3{font:600 16px/1.3 var(--sans);margin:0 0 6px}
.dlg-acts{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}
.fld{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:11px 0}
.fld-l{font:500 13px var(--sans);color:var(--muted)}
.fld input{border:1px solid var(--line-strong);border-radius:8px;padding:7px 10px;font:13px var(--mono);
           width:190px;background:#fff;color:var(--ink)}
.fld input:focus{outline:none;border-color:var(--accent);
           box-shadow:0 0 0 3px var(--accent-tint)}
.code{font:500 12.5px var(--mono);padding:13px 14px;border-radius:8px;text-align:left;
      margin:16px 0 4px;cursor:pointer;user-select:all;word-break:break-all;
      background:#101828;color:#eaecf0;border:1px solid #344054}
.code:hover{border-color:var(--accent)}
.dlg-sub{font:12px var(--sans);color:var(--soft);margin:6px 0 0;text-align:center}
#toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,8px);background:#101828;color:#fff;
       font:13px var(--sans);padding:9px 16px;border-radius:8px;opacity:0;pointer-events:none;transition:.2s;
       box-shadow:var(--shadow-lg)}
#toast.on{opacity:1;transform:translate(-50%,0)}
@media (max-width:640px){
  .wrap{padding:16px 16px 40px}
  header{padding:0 16px}
}
</style></head><body>
<header>
  <div class="brand"><span class="mark">a<i>2</i>a</span><span class="bname">cowork</span></div>
  <div class="hright">
    <div class="live"><span class="pulse"></span><span id="live-note">loading&hellip;</span></div>
    <button class="btn lang" id="lang">中文</button>
  </div>
</header>
<div class="wrap">
  <main id="view"></main>
  <div id="detail"></div>
</div>
<dialog id="dlg">
  <h3 id="dlg-title"></h3>
  <div id="dlg-body"></div>
  <form class="dlg-acts" method="dialog"><button value="close" class="btn" id="dlg-close">close</button></form>
</dialog>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const dlg = $("#dlg");
let DATA = null, view = {d: null, a: null};  // a: selected agent id (survives re-renders)

/* i18n: t(englishString) — zh table maps to Chinese, everything else falls
through unchanged; one table beats a key scheme when English is the source */
let LANG = localStorage.getItem("lang") === "zh" ? "zh" : "en";
const ZH = {
  "live · 3s": "实时 · 3s", "problem": "异常", "loading…": "加载中…",
  "Domains": "域列表", "agents online": "agent 在线", "in-flight": "进行中", "last event": "最近事件",
  "invite": "邀请", "Topology": "拓扑", "Agents": "Agent 列表", "Invites": "邀请码",
  "Recent tasks": "最近任务", "server": "服务器", "no agents registered": "尚无 agent 注册",
  "online": "在线", "offline": "离线", "owner": "属主", "policy": "策略", "driver": "驱动",
  "notify": "通知", "verified": "已验证", "unverified": "未验证",
  "status": "状态", "code": "邀请码", "note": "备注", "uses left": "剩余次数", "expires": "有效期",
  "agent": "Agent", "id": "ID",
  "created": "创建时间", "from": "发起方", "to": "接收方", "reason": "失败原因",
  "kick": "踢出", "disable": "停用", "enable": "启用", "revoke": "吊销", "abort": "中止",
  "create": "创建", "close": "关闭", "disabled": "已停用",
  "block lifted — the agent returns when its worker restarts": "已解除停用——该 agent 的 worker 重启后自动回归",
  "none": "暂无", "no tasks yet": "暂无任务", "New invite": "新邀请码",
  "uses": "次数", "valid for (hours)": "有效时长（小时）",
  "copied": "已复制", "copy failed": "复制失败", "failed": "失败",
  "just now": "刚刚", "expired": "已过期",
  "{n}m ago": "{n} 分钟前", "{n}h ago": "{n} 小时前", "{n}d ago": "{n} 天前",
  "{n}m left": "剩 {n} 分钟", "{n}h left": "剩 {n} 小时", "{n}d left": "剩 {n} 天",
  "agent topology": "agent 拓扑", "loading": "加载中", "failed to load": "加载失败",
};
const t = s => LANG === "zh" ? (ZH[s] || s) : s;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const span = (cls, text) => el("span", cls, text);

/* Feather-style line icons — no icon font, no assets */
const ICONS = {
  plus:    '<path d="M12 5v14M5 12h14"/>',
  left:    '<path d="M19 12H5M12 19l-7-7 7-7"/>',
  kick:    '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><path d="M23 11h-6"/>',
  disable: '<circle cx="12" cy="12" r="10"/><path d="M4.9 4.9l14.2 14.2"/>',
  enable:  '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><path d="M17 11l2 2 4-4"/>',
  abort:   '<rect x="4" y="4" width="16" height="16" rx="3"/>',
  x:       '<path d="M18 6 6 18M6 6l12 12"/>',
};
function icon(name) {
  const svg = svgEl("svg", {viewBox: "0 0 24 24", width: "14", height: "14", fill: "none",
                            stroke: "currentColor", "stroke-width": "2",
                            "stroke-linecap": "round", "stroke-linejoin": "round"});
  svg.innerHTML = ICONS[name];
  return svg;
}
function copyText(text) {  // clipboard API is absent on plain-HTTP LAN origins
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => toast(t("copied")), () => toast(t("copy failed")));
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.append(ta);
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  toast(ok ? t("copied") : t("copy failed"));
}

function row(table, cells, header) {
  const tr = document.createElement("tr");
  for (const cell of cells) {
    const td = document.createElement(header ? "th" : "td");
    for (const item of [].concat(cell)) td.append(typeof item === "string" ? document.createTextNode(item) : item);
    tr.append(td);
  }
  table.append(tr);
  return tr;
}

const TAG = {completed: "green", "input-required": "amber", "pending-approval": "amber",
             working: "blue", dispatched: "blue", failed: "red", canceled: "gray", available: "gray"};
const TERMINAL = new Set(["completed", "failed", "canceled"]);
const LIVE = new Set(["dispatched", "working"]);

function ago(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return t("just now");
  const n = s < 3600 ? Math.floor(s / 60) : s < 86400 ? Math.floor(s / 3600) : Math.floor(s / 86400);
  return t(s < 3600 ? "{n}m ago" : s < 86400 ? "{n}h ago" : "{n}d ago").replace("{n}", n);
}
function till(iso) {
  if (!iso) return "—";
  const s = (Date.parse(iso) - Date.now()) / 1000;
  if (s <= 0) return t("expired");
  const n = s < 3600 ? Math.floor(s / 60) : s < 86400 ? Math.floor(s / 3600) : Math.floor(s / 86400);
  return t(s < 3600 ? "{n}m left" : s < 86400 ? "{n}h left" : "{n}d left").replace("{n}", n);
}

/* ---------- api ---------- */

async function api(path, opts = {}) {
  return fetch(path, Object.assign({}, opts,
      {headers: Object.assign({"Content-Type": "application/json"}, opts.headers || {})}));
}
async function op(body) {
  const r = await api("/admin/op", {method: "POST", body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && j.error.code) || ("HTTP " + r.status));
  return j;
}
function run(fn) {  // do it, refresh, surface failure
  return fn().then(poll, e => toast(t("failed") + ": " + (e.message || e)));
}

let toastT;
function toast(msg) {
  const box = $("#toast");
  box.textContent = msg;
  box.classList.add("on");
  clearTimeout(toastT);
  toastT = setTimeout(() => box.classList.remove("on"), 2600);
}

/* ---------- routing + refresh ---------- */

function route() {
  let h = location.hash;  // browsers percent-encode non-ASCII ids (团队一) in the hash
  try { h = decodeURIComponent(h); } catch {}
  const m = h.match(/^#\/d\/(.+)$/);  // \w is ASCII-only; unknown ids fall back to the overview
  view = {d: m ? m[1] : null, a: null};
  $("#detail").style.display = "none";
  render();
}
window.addEventListener("hashchange", route);

function render() {
  if (!DATA) return;
  if (dlg.open) return;  // don't yank controls mid-dialog
  // a real selection survives the rebuild; a plain click's collapsed caret
  // must not block navigation (it once froze detail-view entry until reload)
  const sel = getSelection();
  if (!sel.isCollapsed && sel.rangeCount && $("#view").contains(sel.anchorNode)) return;
  const v = $("#view");
  v.innerHTML = "";
  if (view.d) {
    if (DATA.domains.some(d => d.id === view.d)) v.append(detailView(DATA, view.d));
    else location.hash = "#/";  // domain removed from config
    const open = DATA.agents.find(x => x.domain === view.d && x.agent_id === view.a);
    if (open) showAgent(DATA, view.d, open);  // after append: #tpanel must be in the document
  } else {
    v.append(overview(DATA));
  }
}

async function poll() {
  if (document.hidden) return;
  try {
    const r = await api("/admin/data");
    if (!r.ok) throw new Error("HTTP " + r.status);
    DATA = await r.json();
    render();
    $("#live-note").textContent = t("live · 3s");
  } catch (e) {
    $("#live-note").textContent = t("problem") + ": " + (e.message || e);
  }
}

/* ---------- overview ---------- */

function stat(n, l, hot) {
  const s = el("div", "dstat");
  s.append(el("div", "n" + (hot ? " hot" : ""), n), el("div", "l", l));
  return s;
}
function dstats(dm) {
  const st = el("div", "dstats");
  st.append(stat(`${dm.online}/${dm.agents}`, t("agents online")),
            stat(String(dm.inflight), t("in-flight"), dm.inflight > 0),
            stat(ago(dm.last_event), t("last event")));
  return st;
}

function overview(d) {
  const frag = document.createDocumentFragment();
  const head = el("div", "sec-head");
  head.append(el("h2", null, t("Domains")));
  const grid = el("div", "domains");
  for (const dm of d.domains) {
    const c = el("div", "dcard");
    c.onclick = () => { location.hash = "#/d/" + dm.id; };
    const top = el("div", "dmeta");
    top.append(el("div", "dname", dm.id), span("tag", dm.channel));
    c.append(top, dstats(dm));
    grid.append(c);
  }
  frag.append(head, grid);
  return frag;
}

/* ---------- domain detail ---------- */

function detailView(d, did) {
  const dm = d.domains.find(x => x.id === did);
  const frag = document.createDocumentFragment();
  const back = el("a", "back");
  back.append(icon("left"), el("span", null, t("Domains")));
  back.setAttribute("href", "#/");
  const head = el("div", "domhead");
  const invite = el("button", "btn pri");
  invite.append(icon("plus"), el("span", null, t("invite")));
  invite.onclick = () => inviteDialog(did);
  head.append(el("h2", "dtitle", did), dstats(dm), invite);
  frag.append(back, head, topoSection(d, did));
  frag.append(agentsSection(d, did), invitesSection(d, did), tasksSection(d, did));
  return frag;
}

/* ---------- topology ---------- */

function svgEl(tag, attrs, parent) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
  if (parent) parent.append(e);
  return e;
}

function topoSVG(d, did) {
  // hub-and-spoke: agents long-poll the server, never each other
  const W = 940, H = 470, cx = W / 2, cy = H / 2 + 4, BW = 132, BH = 40;
  const svg = svgEl("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": t("agent topology")});
  const defs = svgEl("defs", null, svg);
  const lift = svgEl("filter", {id: "lift", x: "-40%", y: "-40%", width: "180%", height: "180%"}, defs);
  svgEl("feDropShadow", {dx: 0, dy: 1.5, stdDeviation: 2, "flood-color": "#101828", "flood-opacity": .13}, lift);
  const ags = d.agents.filter(a => a.domain === did);
  const pos = new Map();
  const step = 2 * Math.PI / Math.max(ags.length, 1);
  const rx = Math.min(370, 190 + ags.length * 20), ry = 150;
  ags.forEach((a, i) => {
    const ang = -Math.PI / 2 + i * step;
    pos.set(a.agent_id, {x: cx + rx * Math.cos(ang), y: cy + ry * Math.sin(ang)});
  });
  svgEl("ellipse", {cx, cy, rx, ry, class: "orbit"}, svg);
  const rim = (ux, uy) => Math.min(BW / 2 / (Math.abs(ux) || 1e-9), BH / 2 / (Math.abs(uy) || 1e-9));
  for (const a of ags) {  // spokes first: cards paint over them
    const p = pos.get(a.agent_id);
    const L = Math.hypot(cx - p.x, cy - p.y) || 1, ux = (cx - p.x) / L, uy = (cy - p.y) / L;
    svgEl("line", {x1: p.x + ux * rim(ux, uy), y1: p.y + uy * rim(ux, uy),
                   x2: cx - ux * 44, y2: cy - uy * 24,  // ends just off the 76x34 chip
                   class: "slink" + (a.online ? " on" : "")}, svg);
  }
  const srv = svgEl("g", {class: "srv"}, svg);
  svgEl("rect", {x: cx - 38, y: cy - 17, width: 76, height: 34, rx: 8, filter: "url(#lift)"}, srv);
  svgEl("text", {x: cx, y: cy + 4, "text-anchor": "middle", class: "tsrv"}, srv).textContent = t("server");
  for (const a of ags) {
    const p = pos.get(a.agent_id);
    const g = svgEl("g", {class: "tnode" + (view.a === a.agent_id ? " sel" : ""), transform: `translate(${p.x},${p.y})`}, svg);
    g.onclick = () => showAgent(d, did, a);
    svgEl("rect", {class: "body", x: -BW / 2, y: -BH / 2, width: BW, height: BH, rx: 10, filter: "url(#lift)"}, g);
    svgEl("circle", {class: "tava " + (a.online ? "on" : "off"), cx: -BW / 2 + 20, r: 10}, g);
    svgEl("text", {x: -BW / 2 + 20, y: 3.5, "text-anchor": "middle", class: "tinit"}, g).textContent =
        (a.owner[0] || "?").toUpperCase();
    svgEl("text", {x: -BW / 2 + 38, y: 4, class: "tname"}, g).textContent =
        a.agent_id.length > 13 ? a.agent_id.slice(0, 12) + "…" : a.agent_id;  // full id in the panel
    const live = d.tasks.filter(k => k.domain === did && k.target === a.agent_id && LIVE.has(k.status)).length;
    if (live) {  // in-flight count
      svgEl("circle", {cx: BW / 2 + 1, cy: -BH / 2 - 1, r: 8.5, fill: "var(--accent)"}, g);
      svgEl("text", {x: BW / 2 + 1, y: -BH / 2 + 2.4, "text-anchor": "middle", class: "tbadge"}, g).textContent = String(live);
    }
  }
  if (!ags.length)
    svgEl("text", {x: cx, y: cy, "text-anchor": "middle", class: "towner"}, svg).textContent = t("no agents registered");
  return svg;
}

/* ---------- sections ---------- */

function section(title, cardCls) {
  const sec = el("section");
  const head = el("div", "sec-head");
  head.append(el("h2", null, title));
  const card = el("div", "card" + (cardCls ? " " + cardCls : ""));
  sec.append(head, card);
  return [sec, card];
}

function topoSection(d, did) {
  const [sec, card] = section(t("Topology"), "topo");
  card.append(topoSVG(d, did));
  const panel = el("div", "tpanel");
  panel.id = "tpanel";
  panel.hidden = true;
  card.append(panel);
  return sec;
}

function showAgent(d, did, a) {
  view.a = a.agent_id;
  const p = $("#tpanel");
  p.hidden = false;
  p.innerHTML = "";
  const head = el("div");
  head.append(el("span", "tp-name", a.agent_id),
              span(a.online ? "tag green" : "tag gray", a.online ? t("online") : t("offline")));
  p.append(head);
  const meta = el("div", "tp-meta");
  for (const [k, v] of [[t("owner"), a.owner], [t("policy"), a.accept_policy],
                        [t("driver"), a.driver_kind], [t("notify"), a.notify_verified ? t("verified") : t("unverified")]]) {
    meta.append(el("span", "k", k), el("span", "v", v));
  }
  p.append(meta);
  if (a.description) p.append(el("p", "tp-desc", a.description));
  const mine = d.tasks.filter(k => k.domain === did && (k.initiator === a.agent_id || k.target === a.agent_id)).slice(0, 6);
  if (mine.length) {
    const tb = el("table", "tp-tasks");
    for (const k of mine)
      row(tb, [k.created_at.slice(5, 16).replace("T", " "),
               (k.initiator === a.agent_id ? "→ " : "← ") + (k.initiator === a.agent_id ? k.target : k.initiator),
               span("tag " + (TAG[k.status] || "gray"), k.status)]);
    p.append(tb);
  }
  const acts = el("div", "tp-acts");
  const close = el("button", "btn");
  close.append(icon("x"));
  close.setAttribute("aria-label", t("close"));
  close.onclick = () => { view.a = null; p.hidden = true; };
  acts.append(close, ...agentActs(d, did, a.agent_id));
  p.append(acts);
}

/* ---------- agents / invites / tasks ---------- */

function mkBtn(label, cls, fn, ico) {
  const b = el("button", "btn " + (cls || ""));
  if (ico) b.append(icon(ico));
  if (label) b.append(el("span", null, label));
  b.onclick = e => {  // hold the button until the op settles; a second click would re-fire it
    e.stopPropagation();
    b.disabled = true;
    Promise.resolve(fn()).finally(() => { b.disabled = false; });
  };
  return b;
}
function agentActs(d, did, id) {
  return [mkBtn(t("kick"), "", () => run(() => op({op: "kick", domain: did, agent_id: id})), "kick"),
          mkBtn(t("disable"), "danger", () => run(() => op({op: "disable", domain: did, agent_id: id})), "disable")];
}

function agentsSection(d, did) {
  const [sec, card] = section(t("Agents"), "tbl");
  const tb = el("table");
  row(tb, [t("agent"), t("owner"), t("status"), t("policy"), t("driver"), ""], true);
  for (const a of d.agents.filter(a => a.domain === did)) {
    const tr = row(tb, [span("name", a.agent_id), a.owner,
                       [span(a.online ? "dot on" : "dot off"), a.online ? t("online") : t("offline")],
                       a.accept_policy, a.driver_kind, agentActs(d, did, a.agent_id)]);
    tr.classList.add("arow");
    tr.onclick = e => { if (!e.target.closest("button")) showAgent(d, did, a); };
  }
  for (const b of d.disabled.filter(b => b.domain === did))
    row(tb, [span("name", b.agent_id), "", span("tag red", t("disabled")), "", "",
             mkBtn(t("enable"), "", () => run(() => op({op: "enable", domain: did, agent_id: b.agent_id})
                 .then(() => toast(t("block lifted — the agent returns when its worker restarts")))), "enable")])
        .classList.add("brow");
  if (!d.agents.some(a => a.domain === did) && !d.disabled.some(b => b.domain === did)) noteRow(tb, t("none"), 6);
  card.append(tb);
  return sec;
}

function noteRow(tb, text, n) {
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = n;
  td.append(el("span", "note", text));
  tr.append(td);
  tb.append(tr);
}

function invitesSection(d, did) {
  const [sec, card] = section(t("Invites"), "tbl");
  const tb = el("table");
  row(tb, [t("code"), t("note"), t("uses left"), t("expires"), ""], true);
  const invs = d.invites.filter(i => i.domain === did);
  for (const i of invs)
    row(tb, [span("mono", i.code), i.note || "", String(i.uses_left), till(i.expires_at),
             mkBtn(t("revoke"), "", () => run(() => op({op: "invite_revoke", code: i.code})), "x")]);
  if (!invs.length) noteRow(tb, t("none"), 5);
  card.append(tb);
  return sec;
}

function tasksSection(d, did) {
  const [sec, card] = section(t("Recent tasks"), "tbl");
  const tb = el("table");
  const tks = d.tasks.filter(k => k.domain === did);
  const showReason = tks.some(k => k.fail_reason);
  const cols = [t("created"), t("id"), t("from"), t("to"), t("status")]
      .concat(showReason ? [t("reason")] : [], [""]);
  row(tb, cols, true);
  for (const k of tks) {
    const cells = [k.created_at.slice(5, 16).replace("T", " "), span("name", k.id.slice(0, 8)), k.initiator, k.target,
                   span("tag " + (TAG[k.status] || "gray"), k.status)];
    if (showReason) cells.push(k.fail_reason || "");
    cells.push(TERMINAL.has(k.status) ? ""
              : mkBtn(t("abort"), "", () => run(() => op({op: "task_abort", domain: did, task_id: k.id})), "abort"));
    const tr = row(tb, cells);
    tr.classList.add("tk");
    tr.onclick = e => { if (!e.target.closest("button")) showTask(k.id); };
  }
  if (!tks.length) noteRow(tb, t("no tasks yet"), cols.length);
  card.append(tb);
  return sec;
}

/* ---------- invite dialog ---------- */

function fld(label, val, parent) {
  const w = el("label", "fld");
  w.append(el("span", "fld-l", label));
  const i = el("input");
  i.value = val;
  w.append(i);
  parent.append(w);
  return i;  // the input, not the wrapping label
}

function inviteDialog(did) {
  const body = $("#dlg-body");
  body.innerHTML = "";
  $("#dlg-title").textContent = t("New invite") + " — " + did;
  const uses = fld(t("uses"), "1", body), hours = fld(t("valid for (hours)"), "24", body), note = fld(t("note"), "", body);
  const create = mkBtn(t("create"), "pri", null, "plus");
  create.style.marginTop = "12px";
  create.onclick = async () => {
    create.disabled = true;  // a double click would mint two codes, one orphaned
    try {
      const r = await op({op: "invite", domain: did, note: note.value,
                          uses: Number(uses.value) || 1,
                          hours: Number(hours.value) || 24});
      body.innerHTML = "";
      const ch = DATA.domains.find(x => x.id === did).channel;
      const cmd = (py, cd) => el("div", "code",
          `${cd} && ${py} api.py join --server ${location.origin} --domain ${did} --code ${r.code} `
          + `--agent-id <id> --owner <name> --notify-channel ${ch} --notify-id-type <t> --notify-id <im-id>`);
      const c1 = cmd("~/a2a-cowork/a2a-worker/.venv/bin/python", "cd ~/a2a-cowork/a2a-skill");
      const c2 = cmd("%USERPROFILE%\\a2a-cowork\\a2a-worker\\.venv\\Scripts\\python.exe",
                     "cd %USERPROFILE%\\a2a-cowork\\a2a-skill");
      c1.onclick = () => copyText(c1.textContent);
      c2.onclick = () => copyText(c2.textContent);
      body.append(c1, c2);
      body.append(el("p", "dlg-sub", `${r.uses}× · ${till(r.expires_at)}`));
    } catch (e) {
      create.disabled = false;
      toast(t("failed") + ": " + (e.message || e));
    }
  };
  body.append(create);
  dlg.showModal();
}

/* ---------- task drill-down + event log ---------- */

async function showTask(id) {
  const detail = $("#detail");
  detail.style.display = "block";
  detail.textContent = t("loading") + " " + id + " …";
  try {
    const r = await api(`/admin/task/${id}`);
    const d = await r.json();
    if (!r.ok) { detail.textContent = JSON.stringify(d); return; }
    const lines = [`${d.id}  ${d.initiator} -> ${d.target}  ${d.status}${d.fail_reason ? " (" + d.fail_reason + ")" : ""}`];
    for (const m of d.convo) lines.push(`[${m.at}] ${m.from}: ${m.text}`);
    for (const e of d.events) lines.push(`[${e.created_at}] #${e.seq} ${e.type} ${e.payload}`);
    detail.textContent = lines.join("\n");
    detail.scrollIntoView({block: "nearest", behavior: "smooth"});
  } catch (e) {
    detail.textContent = t("failed to load") + " " + id + ": " + e;
  }
}


/* ---------- boot ---------- */

function applyLang() {  // header + dialog shell are static HTML; the rest re-renders
  $("#lang").textContent = LANG === "zh" ? "EN" : "中文";
  document.documentElement.lang = LANG;
  $("#dlg-close").textContent = t("close");
  if (!DATA) $("#live-note").textContent = t("loading…");
}
$("#lang").onclick = () => {
  LANG = LANG === "zh" ? "en" : "zh";
  try { localStorage.setItem("lang", LANG); } catch {}  // storage may be blocked
  applyLang();
  render();
};
applyLang();
route();
poll();
window.__pollId = setInterval(poll, 3000);  // exposed so console captures can pause auto-refresh
</script></body></html>
"""
