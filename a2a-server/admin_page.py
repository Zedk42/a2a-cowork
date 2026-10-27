r"""Admin console. One static HTML page (this file) + the JSON endpoints in
server.py: /admin/data (snapshot), /admin/task/{id} (full convo and events),
/admin/file/{id} (staged file download), and /admin/op (operator actions:
invite / invite_revoke / kick / disable / enable / task_abort). Vanilla JS +
inline SVG, no build step, no external assets (works fully offline on an
intranet); everything user-supplied is rendered via textContent, so no HTML
escaping is needed. Unauthenticated by design: it serves a trusted LAN. UI is
bilingual (EN/中文, toggled in the header).

Rendering is section-based: each section (topology, agents, invites, tasks,
the overview cards) keeps a signature of its data slice and is rebuilt only
when that slice changes — the 3s snapshot never touches a steady DOM, so
clicks and text selection are not interrupted. Filter inputs and the dialog
live outside the rebuilt areas and keep their state."""

ADMIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A2A Co-Work</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230f172a'/%3E%3Ctext x='16' y='21' font-family='ui-monospace,Menlo,monospace' font-size='12' font-weight='700' fill='%23fff' text-anchor='middle'%3Ea2a%3C/text%3E%3C/svg%3E">
<style>
:root{
  --bg:#f8f9fa; --card:#ffffff; --subtle-bg:#f3f4f6;
  --ink:#0f172a; --muted:#334155; --soft:#64748b; --faint:#94a3b8;
  --line:#e5e7eb; --line-strong:#d1d5db;
  --accent:#2563eb; --accent-dk:#1d4ed8; --accent-tint:rgba(37,99,235,.1);
  --green:#047857; --green-tint:#ecfdf5; --green-bd:#a7f3d0;
  --red:#b91c1c; --red-tint:#fef2f2; --red-bd:#fecaca;
  --amber:#b45309; --amber-tint:#fffbeb; --amber-bd:#fde68a;
  --blue:#1d4ed8; --blue-tint:#eff6ff; --blue-bd:#bfdbfe;
  --gray-tint:#f3f4f6; --gray-bd:#e5e7eb;
  --mono:ui-monospace,"SF Mono","Cascadia Code",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",system-ui,"Helvetica Neue",sans-serif;
  --shadow:0 1px 2px rgba(15,23,42,.03),0 1px 3px rgba(15,23,42,.02);
  --shadow-md:0 4px 12px rgba(15,23,42,.06);
  --shadow-lg:0 16px 36px rgba(15,23,42,.14);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:13.5px/1.55 var(--sans);-webkit-font-smoothing:antialiased;
     text-rendering:optimizeLegibility}
.wrap{max-width:1180px;margin:0 auto;padding:28px 32px 56px}
header{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.92);
       backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
       height:54px;padding:0 32px;display:flex;justify-content:space-between;
       align-items:center;border-bottom:1px solid var(--line)}
.brand{display:inline-flex;align-items:center;gap:9px;text-decoration:none;color:inherit}
.brand .mark{display:inline-flex;align-items:center;justify-content:center;
             background:var(--ink);color:#fff;font:700 12px/1 var(--mono);
             padding:5px 7px;border-radius:6px;letter-spacing:-.02em}
.brand .bname{font:600 14.5px/1 var(--sans);letter-spacing:-.01em;color:var(--ink)}
.live{display:inline-flex;align-items:center;gap:7px;font:12px/1 var(--sans);
      color:var(--soft);background:var(--subtle-bg);padding:5px 10px;
      border-radius:999px;border:1px solid var(--line)}
.hright{display:flex;align-items:center;gap:12px}
.btn.lang{padding:5px 10px;font-size:12px;color:var(--muted)}
.pulse{width:6px;height:6px;border-radius:50%;background:#10b981;animation:pulse 2.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.pulse{animation:none}.fadein{animation:none}}
section{margin-top:28px}
.sec-head{display:flex;align-items:center;gap:9px;margin:0 0 10px;min-height:30px}
h2{font:600 15px/1.3 var(--sans);letter-spacing:-.01em;margin:0;color:var(--ink)}
.cnt{font:500 11px/1 var(--mono);color:var(--soft);background:var(--gray-tint);
     border:1px solid var(--gray-bd);border-radius:999px;padding:3px 8px}
.note{font:12.5px var(--sans);color:var(--soft)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:4px 18px 10px;box-shadow:var(--shadow)}
.card.tbl{overflow-x:auto;padding:2px 14px 6px}
.fadein{animation:fadein .16s ease-out}
@keyframes fadein{from{opacity:.5}to{opacity:1}}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th{font:600 11px/1.4 var(--sans);color:var(--soft);text-align:left;text-transform:uppercase;
   letter-spacing:.04em;padding:11px 12px 8px;border-bottom:1px solid var(--line)}
.card.tbl th:last-child,.card.tbl td:last-child{width:1%;white-space:nowrap;text-align:right}
td{padding:9.5px 12px;border-bottom:1px solid var(--line);vertical-align:middle;
   font-size:13px;color:var(--muted)}
tr:last-child td,tr:last-child th{border-bottom:none}
tr.empty td{text-align:center;padding:24px 12px}
th,td .name,td .mono,.tag{white-space:nowrap}
td .name{font-weight:500;color:var(--ink);font-family:var(--mono);font-size:12.5px}
td .mono{font-family:var(--mono);font-size:12.5px;color:var(--ink)}
tr.tk,tr.arow,tr.irow{cursor:pointer;transition:background .1s}
tr.tk:hover td,tr.arow:hover td,tr.irow:hover td{background:#f8fafc}
tr.brow td{background:var(--red-tint)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.on{background:#10b981} .dot.off{background:#cbd5e1}
.tag{display:inline-flex;align-items:center;font:500 11.5px/1 var(--sans);
     padding:3.5px 8.5px;border-radius:6px;border:1px solid var(--gray-bd);
     color:var(--muted);background:var(--gray-tint)}
.tag.green{color:var(--green);background:var(--green-tint);border-color:var(--green-bd)}
.tag.red{color:var(--red);background:var(--red-tint);border-color:var(--red-bd)}
.tag.amber{color:var(--amber);background:var(--amber-tint);border-color:var(--amber-bd)}
.tag.blue{color:var(--blue);background:var(--blue-tint);border-color:var(--blue-bd)}
/* overview */
.domains{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:18px;margin-top:16px}
.dcard{background:var(--card);border:1px solid var(--line);border-radius:10px;
       padding:20px 22px 18px;cursor:pointer;box-shadow:var(--shadow);
       transition:border-color .15s ease,box-shadow .15s ease,transform .15s ease}
.dcard:hover{border-color:var(--line-strong);box-shadow:var(--shadow-md);transform:translateY(-1px)}
.dmeta{display:flex;justify-content:space-between;align-items:center;gap:10px}
.dname{font:600 18px/1.2 var(--sans);letter-spacing:-.015em;color:var(--ink)}
.dstats{display:flex;gap:28px}
.dcard .dstats{border-top:1px solid var(--line);padding-top:14px;margin-top:16px}
.dstat .n{font:600 19px/1 var(--sans);font-variant-numeric:tabular-nums;color:var(--ink);letter-spacing:-.02em}
.dstat .n.hot{color:var(--accent)}
.dstat .n.fresh{color:var(--green)}
.dstat .l{font:400 11.5px/1 var(--sans);color:var(--soft);margin-top:5px}
/* domain detail */
.back{display:inline-flex;align-items:center;gap:5px;font:500 12.5px var(--sans);
      color:var(--soft);text-decoration:none;padding:4px 0;transition:color .12s}
.back:hover{color:var(--ink)}
.domhead{display:flex;align-items:center;gap:14px;margin-top:8px;flex-wrap:wrap;
         padding-bottom:4px}
.dtitle{font:600 22px/1.1 var(--sans);letter-spacing:-.02em;margin:0}
.domhead .dstats{margin-left:auto;margin-right:8px}
/* topology */
.card.topo{padding:12px 20px 14px}
.topo > svg{display:block;width:100%;height:auto}
.orbit{fill:none;stroke:var(--line);stroke-dasharray:3 6}
.slink{stroke:#cbd5e1;stroke-width:1.4}
.slink:not(.on){stroke-dasharray:4 4}
.slink.on{stroke:var(--accent);stroke-opacity:.4}
.tnode{cursor:pointer}
.tnode rect.body{fill:var(--card);stroke:var(--line-strong);stroke-width:1.2;transition:stroke .12s}
.tnode.off rect.body{fill:#f8fafc;stroke:var(--line)}
.tnode.off .tname{fill:var(--soft)}
.tnode:hover rect.body,.tnode.sel rect.body{stroke:var(--accent);stroke-width:1.6}
.tava{stroke-width:1.5}
.tava.on{stroke:#10b981} .tava.off{stroke:#cbd5e1}
.tinit{font:600 10px var(--sans)}
.tname{font:500 11px var(--mono);fill:var(--ink)}
.towner{font:11px var(--sans);fill:var(--soft)}
.tsrv{font:600 11px var(--mono);fill:#fff;letter-spacing:.02em}
.tbadge{font:600 10px var(--mono);fill:#fff}
.srv rect{fill:var(--ink)}
.legend{display:flex;gap:22px;align-items:center;justify-content:center;
        padding:4px 0 6px;font:12px var(--sans);color:var(--soft)}
.legend svg{margin-right:6px;vertical-align:-2px}
.tpanel{margin-top:16px;border-top:1px solid var(--line);padding-top:16px;
        display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start}
.tpanel[hidden]{display:none}
.tp-name{font:600 14px/1.2 var(--mono);margin-right:10px;color:var(--ink)}
.tp-meta{display:grid;grid-template-columns:auto auto;gap:4px 16px;font:12px var(--sans);color:var(--muted)}
.tp-meta .k{color:var(--soft)}
.tp-meta .v{font-family:var(--mono);font-size:11.5px;color:var(--ink)}
.tp-desc{flex-basis:100%;margin:0;font-size:12.5px;color:var(--muted);
         background:var(--subtle-bg);padding:8px 12px;border-radius:6px}
.tp-tasks{margin:4px 0 0;width:auto}
.tp-tasks td{padding:5px 10px;font-size:12px}
.tp-acts{margin-left:auto;display:flex;gap:8px;align-items:center}
/* controls */
.btn{display:inline-flex;align-items:center;gap:6px;font:500 12.5px/1 var(--sans);
     padding:7px 12px;border-radius:7px;border:1px solid var(--line-strong);
     background:var(--card);color:var(--muted);cursor:pointer;transition:all .12s}
.btn:hover{background:var(--subtle-bg);color:var(--ink)}
.btn:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.btn.pri{background:var(--ink);color:#fff;border-color:var(--ink)}
.btn.pri:hover{background:#1e293b;border-color:#1e293b}
.btn.danger:hover{border-color:var(--red);color:var(--red);background:var(--red-tint)}
.btn.arm{background:var(--red);border-color:var(--red);color:#fff}
.btn svg{flex:none}
.btn.icon-btn{padding:6px 8px}
.tbl .btn{padding:4.5px 9px;font-size:12px;gap:5px}
.tbl .btn svg{width:12px;height:12px}
td .btn + .btn{margin-left:6px}
.in{border:1px solid var(--line-strong);border-radius:7px;padding:5.5px 10px;font:12.5px var(--mono);
    width:170px;background:#fff;color:var(--ink);transition:border-color .12s,box-shadow .12s}
.in:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-tint)}
select.in{width:auto;font-family:var(--sans);cursor:pointer}
.tbar{display:flex;gap:8px;margin-left:auto}
/* task drill-down */
#detail{display:none;background:var(--card);border:1px solid var(--line);
        border-radius:10px;margin-top:24px;padding:14px 18px 18px;box-shadow:var(--shadow-md)}
.dt-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding-bottom:12px;
         border-bottom:1px solid var(--line)}
.dt-id{font:600 13px var(--mono);color:var(--ink)}
.dt-flow{font:12px var(--mono);color:var(--soft)}
.dt-acts{margin-left:auto;display:flex;gap:6px;align-items:center}
.dt-files{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 0 2px}
.dt-file{display:inline-flex;align-items:center;gap:6px;font:500 12px/1 var(--mono);
         padding:5px 10px;border-radius:6px;border:1px solid var(--line-strong);
         background:var(--subtle-bg);color:var(--ink);text-decoration:none;transition:border-color .12s}
.dt-file:hover{border-color:var(--accent);color:var(--accent)}
.dt-file span{color:var(--soft);font-size:11px}
.dt-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px;margin-top:12px}
.dt-pane{min-width:0}
.dt-label{font:600 11px var(--sans);color:var(--soft);text-transform:uppercase;
          letter-spacing:.04em;margin-bottom:8px}
.dt-body{font:12px/1.65 var(--mono);color:var(--muted);white-space:pre-wrap;word-break:break-word;
         max-height:36vh;overflow-y:auto;background:var(--subtle-bg);border:1px solid var(--line);
         border-radius:8px;padding:10px 12px}
.dt-body .who{color:var(--soft);font-weight:500}
/* dialog + toast */
dialog{border:1px solid var(--line);border-radius:12px;padding:22px 24px 18px;
       background:var(--card);color:var(--ink);max-width:580px;width:92%;box-shadow:var(--shadow-lg)}
dialog::backdrop{background:rgba(15,23,42,.35);backdrop-filter:blur(2px)}
dialog h3{font:600 16px/1.3 var(--sans);letter-spacing:-.01em;margin:0 0 8px}
.dlg-acts{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}
.fld{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:12px 0}
.fld-l{font:500 13px var(--sans);color:var(--muted)}
.fld input{border:1px solid var(--line-strong);border-radius:7px;padding:7px 10px;font:13px var(--mono);
           width:220px;background:#fff;color:var(--ink)}
.fld input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-tint)}
.code-l{font:600 11px/1 var(--sans);letter-spacing:.04em;color:var(--soft);margin:14px 0 0}
.code{font:500 12px/1.55 var(--mono);padding:12px 14px;border-radius:8px;text-align:left;
      margin:6px 0 4px;cursor:pointer;user-select:all;word-break:break-all;
      background:#0f172a;color:#e2e8f0;border:1px solid #1e293b;transition:border-color .12s}
.code:hover{border-color:var(--accent)}
#toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,8px);background:var(--ink);color:#fff;
       font:500 12.5px var(--sans);padding:8px 16px;border-radius:8px;opacity:0;pointer-events:none;
       transition:.18s;box-shadow:var(--shadow-lg);z-index:50}
#toast.on{opacity:1;transform:translate(-50%,0)}
@media (max-width:640px){
  .wrap{padding:16px 16px 40px}
  header{padding:0 16px}
  .tbar{margin-left:0}
}
</style></head><body>
<header>
  <a class="brand" href="#/"><span class="mark">a2a</span><span class="bname">cowork</span></a>
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
let FILTER = {status: "", q: ""};            // task table; inputs live outside rebuilds
let SIG = {}, MOUNTED = null;                // section key -> data signature; mounted route key
let DETAIL = {id: null, status: null};       // open drill-down, for auto-refresh

/* i18n: t(englishString) — zh table maps to Chinese, everything else falls
through unchanged; one table beats a key scheme when English is the source */
let LANG = localStorage.getItem("lang") === "zh" ? "zh" : "en";
const ZH = {  // server / agent / worker / driver / token stay English
  "live · 3s": "实时 · 3s", "problem": "异常", "loading…": "加载中…",
  "Domains": "全部域", "agents online": "agent 在线", "in-flight": "进行中", "last event": "最近事件",
  "invite": "邀请成员", "Topology": "拓扑", "Agents": "Agent 列表", "Invites": "邀请码",
  "Recent tasks": "最近任务", "no agents registered": "还没有 agent 注册",
  "online": "在线", "offline": "离线", "owner": "属主", "policy": "策略",
  "notify": "通知", "verified": "已验证", "unverified": "未验证",
  "status": "状态", "code": "邀请码", "note": "备注", "id": "ID",
  "created": "创建时间", "from": "发起方", "to": "接收方", "reason": "失败原因",
  "kick": "踢出", "disable": "停用", "enable": "启用", "revoke": "吊销", "abort": "中止",
  "create": "创建", "close": "关闭", "disabled": "已停用", "command": "命令",
  "block lifted — the agent returns when its worker restarts": "已解除停用，该 agent 的 worker 重启后会自动回归",
  "none": "暂无", "no tasks yet": "暂无任务", "New invite": "新建邀请码", "Invite": "邀请码",
  "copied": "已复制", "copy failed": "复制失败", "failed": "失败",
  "just now": "刚刚",
  "{n}m ago": "{n} 分钟前", "{n}h ago": "{n} 小时前", "{n}d ago": "{n} 天前",
  "agent topology": "agent 拓扑", "loading": "加载中", "failed to load": "加载失败",
  "all statuses": "全部状态", "filter…": "筛选…", "sure?": "确认？",
  "Conversation": "对话", "Events": "事件",
  "Click a command block below to copy:": "点击下方命令块即可复制：",
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
  copy:    '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  file:    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
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
const fresh = iso => iso && (Date.now() - Date.parse(iso)) / 1000 < 300;
function fmtSize(n) {
  return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
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

/* ---------- routing + section rendering ---------- */

function closeDetail() {
  DETAIL = {id: null, status: null};
  $("#detail").style.display = "none";
}

function route() {
  let h = location.hash;  // browsers percent-encode non-ASCII ids (团队一) in the hash
  try { h = decodeURIComponent(h); } catch {}
  const m = h.match(/^#\/d\/(.+)$/);  // \w is ASCII-only; unknown ids fall back to the overview
  view = {d: m ? m[1] : null, a: null};
  FILTER = {status: "", q: ""};
  SIG = {};
  closeDetail();
  render();
}
window.addEventListener("hashchange", route);
window.addEventListener("keydown", e => {
  if (e.key === "Escape" && !dlg.open && DETAIL.id) closeDetail();
});

/* Sections are rebuilt only when their data slice changed (SIG). Filter inputs
   and the dialog sit outside the rebuilt nodes, so focus and open state survive
   every refresh; a section holding a text selection is left alone for a tick. */
function section(key, sig, card, build) {
  if (SIG[key] === sig) return;
  const sel = getSelection();
  if (!sel.isCollapsed && sel.rangeCount && sel.getRangeAt(0).intersectsNode(card)) return;
  card.innerHTML = "";
  build(card);
  SIG[key] = sig;  // set only after a clean build: a throwing build retries next tick
  card.classList.remove("fadein");
  void card.offsetWidth;  // restart the fade so a refresh tick is perceptible, not jarring
  card.classList.add("fadein");
}

function render() {
  if (!DATA) return;
  if (dlg.open) return;  // don't yank controls mid-dialog
  if (!DATA.domains.some(d => d.id === view.d)) view.d = null;  // domain removed from config
  const key = view.d || "overview";
  if (MOUNTED !== key) mount(key);
  const did = view.d;
  if (!did) {
    const tick = Math.floor(Date.now() / 60000);  // relative times ("3m ago") advance on quiet sections
    section("overview", JSON.stringify([DATA.domains, tick]), $("[data-k=overview]"), buildOverview);
    return;
  }
  const dm = DATA.domains.find(x => x.id === did);
  const ags = DATA.agents.filter(a => a.domain === did);
  const dts = DATA.tasks.filter(k => k.domain === did);
  const tick = Math.floor(Date.now() / 60000);
  section("head", JSON.stringify([dm, tick]), $("[data-k=head]"), c => buildHead(c, dm));
  section("topo", JSON.stringify({a: ags,  // whole objects: the panel renders policy/driver/notify/description too
                                  t: dts.filter(k => LIVE.has(k.status)).map(k => [k.target, k.id]),
                                  sel: view.a, selTasks: agentTaskSlice(dts, view.a)}),
          $("[data-k=topo]"), c => buildTopo(c, did, ags, dts));
  section("agents", JSON.stringify({a: ags, dis: DATA.disabled.filter(b => b.domain === did)}),
          $("[data-k=agents]"), c => buildAgents(c, did, ags));
  section("invites", JSON.stringify(DATA.invites.filter(i => i.domain === did)),
          $("[data-k=invites]"), c => buildInvites(c, did));
  section("tasks", JSON.stringify({t: dts, f: FILTER}), $("[data-k=tasks]"), c => buildTasks(c, did, dts));
}

function agentTaskSlice(dts, aid) {
  if (!aid) return null;
  return dts.filter(k => k.initiator === aid || k.target === aid).slice(0, 6)
            .map(k => [k.id, k.created_at, k.initiator, k.target, k.status]);
}

function mount(key) {  // full rebuild: navigation or language switch only
  MOUNTED = key;
  SIG = {};
  const v = $("#view");
  v.innerHTML = "";
  if (!view.d) {
    const sec = el("section");
    const h = el("div", "sec-head");
    h.append(el("h2", null, t("Domains")));
    sec.append(h);
    const grid = el("div", "domains");
    grid.setAttribute("data-k", "overview");
    sec.append(grid);
    v.append(sec);
    return;
  }
  const back = el("a", "back");
  back.append(icon("left"), el("span", null, t("Domains")));
  back.setAttribute("href", "#/");
  const head = el("div");
  head.setAttribute("data-k", "head");
  v.append(back, head, shell("topo", "topo", t("Topology"), null, false), shell("agents", "tbl", t("Agents"), null, true),
           shell("invites", "tbl", t("Invites"), null, true), tasksShell());
}

function shell(key, cls, title, toolbar, badge) {
  const sec = el("section");
  const head = el("div", "sec-head");
  head.append(el("h2", null, title));
  if (badge) {
    const cnt = el("span", "cnt", "");
    cnt.setAttribute("data-cnt", key);
    head.append(cnt);
  }
  if (toolbar) head.append(toolbar);
  const card = el("div", "card" + (cls ? " " + cls : ""));
  card.setAttribute("data-k", key);
  sec.append(head, card);
  return sec;
}

function tasksShell() {
  const sel = el("select", "in");
  sel.setAttribute("data-k", "status");
  sel.append(new Option(t("all statuses"), ""));
  for (const s of ["pending-approval", "available", "dispatched", "working", "input-required",
                   "completed", "failed", "canceled"]) sel.append(new Option(s, s));
  sel.value = FILTER.status;
  sel.onchange = () => { FILTER.status = sel.value; render(); };
  const q = el("input", "in");
  q.setAttribute("data-k", "q");
  q.placeholder = t("filter…");
  q.value = FILTER.q;
  q.oninput = () => { FILTER.q = q.value; render(); };
  const bar = el("div", "tbar");
  bar.append(sel, q);
  return shell("tasks", "tbl", t("Recent tasks"), bar, true);
}

/* ---------- overview ---------- */

function stat(n, l, hot) {
  const s = el("div", "dstat");
  s.append(el("div", "n" + (hot === "hot" ? " hot" : hot === "fresh" ? " fresh" : ""), n), el("div", "l", l));
  return s;
}
function dstats(dm) {
  const st = el("div", "dstats");
  st.append(stat(`${dm.online}/${dm.agents}`, t("agents online")),
            stat(String(dm.inflight), t("in-flight"), dm.inflight > 0 && "hot"),
            stat(ago(dm.last_event), t("last event"), dm.last_event && fresh(dm.last_event) && "fresh"));
  return st;
}

function buildOverview(grid) {
  for (const dm of DATA.domains) {
    const c = el("div", "dcard");
    c.onclick = () => { location.hash = "#/d/" + dm.id; };
    const top = el("div", "dmeta");
    top.append(el("div", "dname", dm.id), span("tag", dm.channel));
    c.append(top, dstats(dm));
    grid.append(c);
  }
}

/* ---------- domain detail ---------- */

function buildHead(card, dm) {
  const head = el("div", "domhead");
  const invite = el("button", "btn pri");
  invite.append(icon("plus"), el("span", null, t("invite")));
  invite.onclick = () => inviteDialog(dm.id);
  head.append(el("h2", "dtitle", dm.id), span("tag", dm.channel), dstats(dm), invite);
  card.append(head);
}

/* ---------- topology ---------- */

function svgEl(tag, attrs, parent) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
  if (parent) parent.append(e);
  return e;
}
const hue = s => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % 360;

function topoSVG(ags, dts) {
  // hub-and-spoke: agents long-poll the server, never each other
  const W = 940, H = 450, cx = W / 2, cy = H / 2 + 2, BW = 136, BH = 40;
  const svg = svgEl("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": t("agent topology")});
  const defs = svgEl("defs", null, svg);
  const lift = svgEl("filter", {id: "lift", x: "-40%", y: "-40%", width: "180%", height: "180%"}, defs);
  svgEl("feDropShadow", {dx: 0, dy: 1, stdDeviation: 1.5, "flood-color": "#0f172a", "flood-opacity": .08}, lift);
  const pos = new Map();
  const step = 2 * Math.PI / Math.max(ags.length, 1);
  const rx = Math.min(365, 185 + ags.length * 20), ry = 145;
  ags.forEach((a, i) => {
    const ang = -Math.PI / 2 + i * step;
    pos.set(a.agent_id, {x: cx + rx * Math.cos(ang), y: cy + ry * Math.sin(ang)});
  });
  svgEl("ellipse", {cx, cy, rx, ry, class: "orbit"}, svg);
  const rim = (ux, uy) => Math.min(BW / 2 / (Math.abs(ux) || 1e-9), BH / 2 / (Math.abs(uy) || 1e-9));
  for (const a of ags) {
    const p = pos.get(a.agent_id);
    const L = Math.hypot(cx - p.x, cy - p.y) || 1, ux = (cx - p.x) / L, uy = (cy - p.y) / L;
    svgEl("line", {x1: p.x + ux * rim(ux, uy), y1: p.y + uy * rim(ux, uy),
                   x2: cx - ux * 42, y2: cy - uy * 22,
                   class: "slink" + (a.online ? " on" : "")}, svg);
  }
  if (ags.length) {
    const srv = svgEl("g", {class: "srv"}, svg);
    svgEl("rect", {x: cx - 38, y: cy - 17, width: 76, height: 34, rx: 8, filter: "url(#lift)"}, srv);
    svgEl("text", {x: cx, y: cy + 4, "text-anchor": "middle", class: "tsrv"}, srv).textContent = "server";
  }
  for (const a of ags) {
    const p = pos.get(a.agent_id);
    const g = svgEl("g", {class: "tnode" + (a.online ? "" : " off") + (view.a === a.agent_id ? " sel" : ""),
                          transform: `translate(${p.x},${p.y})`}, svg);
    const tip = svgEl("title", null, g);
    tip.textContent = `${a.agent_id} · ${a.owner}`;
    g.onclick = () => { view.a = a.agent_id; render(); };
    svgEl("rect", {class: "body", x: -BW / 2, y: -BH / 2, width: BW, height: BH, rx: 9, filter: "url(#lift)"}, g);
    const h = hue(a.owner);
    svgEl("circle", {class: "tava " + (a.online ? "on" : "off"), cx: -BW / 2 + 20, r: 10,
                     fill: `hsl(${h} 60% 95%)`}, g);
    const init = svgEl("text", {x: -BW / 2 + 20, y: 3.5, "text-anchor": "middle",
                                class: "tinit", fill: `hsl(${h} 45% 36%)`}, g);
    init.textContent = (a.owner[0] || "?").toUpperCase();
    svgEl("text", {x: -BW / 2 + 38, y: 4, class: "tname"}, g).textContent =
        a.agent_id.length > 13 ? a.agent_id.slice(0, 12) + "…" : a.agent_id;
    const live = dts.filter(k => k.target === a.agent_id && LIVE.has(k.status)).length;
    if (live) {
      svgEl("circle", {cx: BW / 2 + 1, cy: -BH / 2 - 1, r: 8.5, fill: "var(--accent)"}, g);
      svgEl("text", {x: BW / 2 + 1, y: -BH / 2 + 2.4, "text-anchor": "middle", class: "tbadge"}, g).textContent = String(live);
    }
  }
  if (!ags.length)
    svgEl("text", {x: cx, y: cy, "text-anchor": "middle", class: "towner"}, svg).textContent = t("no agents registered");
  return svg;
}

const LG_LINE = cls =>
  `<svg width="20" height="8" viewBox="0 0 20 8"><line x1="1" y1="4" x2="19" y2="4" ${cls}/></svg>`;

function buildTopo(card, did, ags, dts) {
  card.append(topoSVG(ags, dts));
  const lg = el("div", "legend");
  lg.innerHTML =
    `<span>${LG_LINE('stroke="var(--accent)" stroke-opacity=".4" stroke-width="1.5"')}${t("online")}</span>` +
    `<span>${LG_LINE('stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="4 4"')}${t("offline")}</span>` +
    `<span><svg width="12" height="12" viewBox="0 0 12 12"><circle cx="6" cy="6" r="5.5" fill="var(--accent)"/>` +
    `<text x="6" y="9" text-anchor="middle" font-size="8" font-weight="600" fill="#fff">n</text></svg>${t("in-flight")}</span>`;
  const open = ags.find(a => a.agent_id === view.a);
  const panel = el("div", "tpanel");
  panel.id = "tpanel";
  panel.hidden = !open;
  card.append(lg, panel);
  if (open) showAgent(panel, open, dts, did);
}

function showAgent(p, a, dts, did) {
  p.hidden = false;
  p.innerHTML = "";
  const head = el("div");
  head.append(el("span", "tp-name", a.agent_id),
              span(a.online ? "tag green" : "tag gray", a.online ? t("online") : t("offline")));
  p.append(head);
  const meta = el("div", "tp-meta");
  for (const [k, v] of [[t("owner"), a.owner], [t("policy"), a.accept_policy],
                        ["driver", a.driver_kind], [t("notify"), a.notify_verified ? t("verified") : t("unverified")]]) {
    meta.append(el("span", "k", k), el("span", "v", v));
  }
  p.append(meta);
  if (a.description) p.append(el("p", "tp-desc", a.description));
  const mine = dts.filter(k => k.initiator === a.agent_id || k.target === a.agent_id).slice(0, 6);
  if (mine.length) {
    const tb = el("table", "tp-tasks");
    for (const k of mine) {
      const tr = row(tb, [k.created_at.slice(5, 16).replace("T", " "),
                          (k.initiator === a.agent_id ? "→ " : "← ") + (k.initiator === a.agent_id ? k.target : k.initiator),
                          span("tag " + (TAG[k.status] || "gray"), k.status)]);
      tr.classList.add("tk");
      tr.onclick = () => showTask(k.id);
    }
    p.append(tb);
  }
  const acts = el("div", "tp-acts");
  const close = el("button", "btn icon-btn");
  close.append(icon("x"));
  close.setAttribute("aria-label", t("close"));
  close.onclick = () => { view.a = null; render(); };
  acts.append(close, ...agentActs(did, a.agent_id));
  p.append(acts);
}

/* ---------- agents / invites / tasks ---------- */

function mkBtn(label, cls, fn, ico, confirmable) {
  const b = el("button", "btn " + (cls || ""));
  if (ico) b.append(icon(ico));
  const lab = el("span", null, label);
  b.append(lab);
  let armed = null;
  b.onclick = e => {
    e.stopPropagation();
    if (confirmable && !armed) {
      b.classList.add("arm");
      lab.textContent = t("sure?");
      armed = setTimeout(() => { armed = null; b.classList.remove("arm"); lab.textContent = label; }, 2500);
      return;
    }
    clearTimeout(armed);
    armed = null;
    b.disabled = true;
    Promise.resolve(fn()).finally(() => { b.disabled = false; b.classList.remove("arm"); lab.textContent = label; });
  };
  return b;
}
function agentActs(did, id) {
  return [mkBtn(t("kick"), "", () => run(() => op({op: "kick", domain: did, agent_id: id})), "kick", true),
          mkBtn(t("disable"), "danger", () => run(() => op({op: "disable", domain: did, agent_id: id})), "disable", true)];
}

function buildAgents(card, did, ags) {
  const tb = el("table");
  row(tb, ["agent", t("owner"), t("status"), t("policy"), "driver", ""], true);
  for (const a of ags) {
    const tr = row(tb, [span("name", a.agent_id), a.owner,
                       [span(a.online ? "dot on" : "dot off"), a.online ? t("online") : t("offline")],
                       a.accept_policy, a.driver_kind, agentActs(did, a.agent_id)]);
    tr.classList.add("arow");
    tr.onclick = e => { if (!e.target.closest("button")) { view.a = a.agent_id; render(); } };
  }
  for (const b of DATA.disabled.filter(b => b.domain === did))
    row(tb, [span("name", b.agent_id), "", span("tag red", t("disabled")), "", "",
             mkBtn(t("enable"), "", () => run(() => op({op: "enable", domain: did, agent_id: b.agent_id})
                 .then(() => toast(t("block lifted — the agent returns when its worker restarts")))), "enable")])
        .classList.add("brow");
  if (!ags.length && !DATA.disabled.some(b => b.domain === did)) noteRow(tb, t("no agents registered"), 6);
  card.append(tb);
  setCnt("agents", ags.length);
}

function noteRow(tb, text, n) {
  const tr = document.createElement("tr");
  tr.className = "empty";
  const td = document.createElement("td");
  td.colSpan = n;
  td.append(el("span", "note", text));
  tr.append(td);
  tb.append(tr);
}

function buildInvites(card, did) {
  const tb = el("table");
  row(tb, [t("code"), t("note"), ""], true);
  const invs = DATA.invites.filter(i => i.domain === did);
  for (const i of invs) {
    const tr = row(tb, [span("mono", i.code), i.note || "",
                       [mkBtn(t("command"), "", () => openInviteCode(did, i.code, i.note), "copy"),
                        mkBtn(t("revoke"), "danger", () => run(() => op({op: "invite_revoke", code: i.code})), "x", true)]]);
    tr.classList.add("irow");
    tr.onclick = e => { if (!e.target.closest("button")) openInviteCode(did, i.code, i.note); };
  }
  if (!invs.length) noteRow(tb, t("none"), 3);
  card.append(tb);
  setCnt("invites", invs.length);
}

function buildTasks(card, did, dts) {
  const q = FILTER.q.toLowerCase();
  const tks = dts.filter(k => (!FILTER.status || k.status === FILTER.status) &&
      (!q || k.id.startsWith(q) || k.initiator.toLowerCase().includes(q) ||
       k.target.toLowerCase().includes(q) || (k.fail_reason || "").toLowerCase().includes(q)));
  const tb = el("table");
  const showReason = tks.some(k => k.fail_reason);
  const cols = [t("created"), t("id"), t("from"), t("to"), t("status")]
      .concat(showReason ? [t("reason")] : [], [""]);
  row(tb, cols, true);
  for (const k of tks) {
    const cells = [k.created_at.slice(5, 16).replace("T", " "), span("name", k.id.slice(0, 8)), k.initiator, k.target,
                   span("tag " + (TAG[k.status] || "gray"), k.status)];
    if (showReason) cells.push(k.fail_reason || "");
    cells.push(TERMINAL.has(k.status) ? ""
              : mkBtn(t("abort"), "", () => run(() => op({op: "task_abort", domain: did, task_id: k.id})), "abort", true));
    const tr = row(tb, cells);
    tr.classList.add("tk");
    tr.onclick = e => { if (!e.target.closest("button")) showTask(k.id); };
  }
  if (!tks.length) noteRow(tb, t("no tasks yet"), cols.length);
  card.append(tb);
  setCnt("tasks", tks.length);
}

function setCnt(key, n) {
  const c = document.querySelector(`[data-cnt="${key}"]`);
  if (c) c.textContent = n;
}

/* ---------- invite dialog ---------- */

function fld(label, val, parent) {
  const w = el("label", "fld");
  w.append(el("span", "fld-l", label));
  const i = el("input");
  i.value = val;
  w.append(i);
  parent.append(w);
  return i;
}

function serverBase() {
  const b = DATA && DATA.public_base;
  return (b && !/\/\/(127\.0\.0\.1|localhost)(:|\/|$)/.test(b)) ? b : location.origin;
}

function renderJoinCmds(body, did, code) {
  body.innerHTML = "";
  body.append(el("div", "note", t("Click a command block below to copy:")));
  const ch = (DATA.domains.find(x => x.id === did) || {}).channel || "log";
  const srv = serverBase();
  const flags = `join --server ${srv} --domain ${did} --code ${code} --agent-id <id> --owner <name> --notify-channel ${ch} --notify-id <im-id>`;
  const mk = (lbl, cmd) => {
    body.append(el("div", "code-l", lbl));
    const c = el("div", "code", cmd);
    c.onclick = () => copyText(c.textContent);
    body.append(c);
  };
  mk("macOS · Linux",
     `([ -d ~/a2a-cowork ] || git clone --depth 1 https://github.com/Zedk42/a2a-cowork.git ~/a2a-cowork) && python3 ~/a2a-cowork/a2a-skill/api.py ${flags} && nohup ~/a2a-cowork/a2a-worker/start.sh > ~/a2a-worker.log 2>&1 &`);
  mk("Windows",
     `(if not exist "%USERPROFILE%\\a2a-cowork" git clone --depth 1 https://github.com/Zedk42/a2a-cowork.git "%USERPROFILE%\\a2a-cowork") && py -3 "%USERPROFILE%\\a2a-cowork\\a2a-skill\\api.py" ${flags} && start /b cmd /c ""%USERPROFILE%\\a2a-cowork\\a2a-worker\\start.cmd" > "%USERPROFILE%\\a2a-worker.log" 2>&1"`);
}

function openInviteCode(did, code, noteText) {
  $("#dlg-title").textContent = t("Invite") + " · " + code + (noteText ? " (" + noteText + ")" : "");
  renderJoinCmds($("#dlg-body"), did, code);
  dlg.showModal();
}

function inviteDialog(did) {
  const body = $("#dlg-body");
  body.innerHTML = "";
  $("#dlg-title").textContent = t("New invite") + " — " + did;
  const note = fld(t("note"), "", body);
  const create = mkBtn(t("create"), "pri", null, "plus");
  create.style.marginTop = "12px";
  create.onclick = async () => {
    create.disabled = true;
    try {
      const r = await op({op: "invite", domain: did, note: note.value});
      renderJoinCmds(body, did, r.code);
      poll();
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
  detail.textContent = t("loading") + " " + id.slice(0, 8) + " …";
  DETAIL = {id, status: DETAIL.id === id ? DETAIL.status : null};
  try {
    const r = await api(`/admin/task/${id}`);
    const d = await r.json();
    if (DETAIL.id !== id) return;
    if (!r.ok) { detail.textContent = JSON.stringify(d); return; }
    DETAIL = {id, status: d.status};
    detail.innerHTML = "";
    const head = el("div", "dt-head");
    head.append(span("tag " + (TAG[d.status] || "gray"), d.status),
                el("span", "dt-id", d.id),
                el("span", "dt-flow", `${d.initiator} → ${d.target}`));
    if (d.fail_reason) head.append(span("tag red", d.fail_reason));
    const acts = el("div", "dt-acts");
    const copy = el("button", "btn icon-btn");
    copy.append(icon("copy"));
    copy.setAttribute("aria-label", "copy");
    copy.onclick = () => copyText(JSON.stringify(d, null, 2));
    const close = el("button", "btn icon-btn");
    close.append(icon("x"));
    close.setAttribute("aria-label", t("close"));
    close.onclick = closeDetail;
    acts.append(copy, close);
    head.append(acts);
    detail.append(head);
    if (d.files && d.files.length) {
      const fbar = el("div", "dt-files");
      for (const f of d.files) {
        const a = el("a", "dt-file");
        a.href = `/admin/file/${f.id}`;
        a.setAttribute("download", f.name);
        a.append(icon("file"), document.createTextNode(f.name), el("span", null, fmtSize(f.size)));
        fbar.append(a);
      }
      detail.append(fbar);
    }
    const cols = el("div", "dt-cols");
    const pane = (label, lines) => {
      const p = el("div", "dt-pane");
      p.append(el("div", "dt-label", label));
      const b = el("div", "dt-body");
      for (const [who, text] of lines) {
        if (who) b.append(el("span", "who", who));
        b.append(document.createTextNode(text + "\n"));
      }
      p.append(b);
      return p;
    };
    cols.append(pane(t("Conversation"), d.convo.map(m => [`[${m.at.slice(5, 19).replace("T", " ")}] ${m.from}: `, m.text])),
                pane(t("Events"), d.events.map(e => [`#${e.seq} ${e.type} `, e.payload])));
    detail.append(cols);
    detail.scrollIntoView({block: "nearest", behavior: "smooth"});
  } catch (e) {
    detail.textContent = t("failed to load") + " " + id.slice(0, 8) + ": " + e;
  }
}

/* ---------- refresh ---------- */

async function poll() {
  if (document.hidden) return;
  try {
    const r = await api("/admin/data");
    if (!r.ok) throw new Error("HTTP " + r.status);
    DATA = await r.json();
    render();
    $("#live-note").textContent = t("live · 3s");
    const k = DATA.tasks.find(k => k.id === DETAIL.id);
    if (k && DETAIL.status !== k.status) showTask(DETAIL.id);
  } catch (e) {
    $("#live-note").textContent = t("problem") + ": " + (e.message || e);
  }
}

/* ---------- boot ---------- */

function applyLang() {
  $("#lang").textContent = LANG === "zh" ? "EN" : "中文";
  document.documentElement.lang = LANG;
  $("#dlg-close").textContent = t("close");
  if (!DATA) $("#live-note").textContent = t("loading…");
}
$("#lang").onclick = () => {
  LANG = LANG === "zh" ? "en" : "zh";
  try { localStorage.setItem("lang", LANG); } catch {}
  applyLang();
  MOUNTED = null;
  render();
  if (DETAIL.id) showTask(DETAIL.id);
};
applyLang();
route();
poll();
window.__pollId = setInterval(poll, 3000);
</script></body></html>
"""
