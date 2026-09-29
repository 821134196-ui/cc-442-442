/* 社区雨水口清掏管理平台 —— 前端（原生 JS） */
"use strict";

// ------------------------------------------------------------- 常量/工具
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const api = async (path, opts = {}) => {
  const headers = Object.assign({ "X-Username": state.username }, opts.headers || {});
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || "请求失败"), { data });
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (iso) => iso ? iso.replace("T", " ").slice(0, 16) : "—";
const pct = (n) => (n == null ? "—" : Number(n).toFixed(1));

const STATUS_LABEL = {
  open: ["待派工", "orange"], dispatched: ["已派工", "blue"], working: ["作业中", "blue"],
  arrived: ["班组到场", "blue"], ready: ["待申请", "amber"],
  pending_accept: ["待验收", "amber"], rejected: ["验收退回", "red"],
  accepted: ["已完成", "green"], normal: ["正常", "green"], blocked: ["堵塞", "red"],
  cleaning: ["清掏中", "blue"], scheduled: ["已排程", "blue"],
  vehicle_missing: ["车辆未到", "red"], completed: ["已完成", "green"],
  cancelled: ["已取消", "gray"], waiting: ["等待到场", "amber"],
  arrived_h: ["已到场", "blue"], missing: ["车辆未到", "red"],
  replaced: ["已改派", "blue"], handed: ["已交接", "green"],
  pending: ["待验收", "amber"], pass: ["通过", "green"], reject: ["退回", "red"],
};
const LV_LABEL = ["正常", "轻度", "中度", "严重"];
const LV_COLOR = ["#8fa1ae", "#e6c043", "#ef7b24", "#d43d2f"];
const STEP_LABEL = { open: "开盖", clean: "清掏", flush: "冲洗", reset: "复位" };
const STEP_ORDER = ["open", "clean", "flush", "reset"];
const ISSUE_KINDS = {
  storm_urgent: "暴雨前加急", cover_damaged: "井盖破损", backflow: "积水反涌",
  silt_spill: "淤泥遗撒", vehicle_missing: "车辆未到", photo_missing: "照片缺失",
};

const state = {
  username: localStorage.getItem("sw_user") || "dispatch",
  user: null,
  dir: { users: [], crews: [], vehicles: [], contacts: [] },
  map: { segments: [], inlets: [] },
  orders: [],
  schedule: [],
  handovers: [],
  accepts: [],
  currentDispatchId: null,
};

const tag = (key) => {
  const [t, cls] = STATUS_LABEL[key] || [key, "gray"];
  return `<span class="tag ${cls}">${t}</span>`;
};
const lvlHtml = (l) => `<span class="lvl" data-l="${l}">` +
  [0, 1, 2].map(i => `<i></i>`).join("") +
  `</span> <span class="muted">${LV_LABEL[l] || "—"}</span>`;
const photoUrl = (p) => p ? `/uploads/${p}` : "";

// ------------------------------------------------------------- 弹窗
function modal(title, bodyHtml, buttons) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  const foot = $("#modalFoot");
  foot.innerHTML = "";
  (buttons || []).forEach(b => {
    const btn = document.createElement("button");
    btn.textContent = b.text;
    btn.className = b.cls || "";
    btn.onclick = () => b.onClick(btn);
    foot.appendChild(btn);
  });
  $("#modalMask").classList.add("open");
  $("#modalBox").classList.toggle("wide", !!arguments[3]);
}
const closeModal = () => $("#modalMask").classList.remove("open");
$("#modalClose").onclick = closeModal;
$("#modalMask").addEventListener("click", e => { if (e.target.id === "modalMask") closeModal(); });

function toast(msg, ok = false) {
  const bar = $("#reminderBar");
  bar.className = "reminder-bar show";
  bar.style.background = ok ? "var(--ok-soft)" : "var(--danger-soft)";
  bar.style.color = ok ? "#1d6e45" : "var(--danger)";
  bar.textContent = msg;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => bar.classList.remove("show"), 3600);
}
const wrap = async (fn, btn) => {
  if (btn) { btn.disabled = true; btn.dataset.txt = btn.textContent; btn.textContent = "处理中…"; }
  try { return await fn(); }
  catch (e) { toast(e.message); throw e; }
  finally { if (btn) { btn.disabled = false; btn.textContent = btn.dataset.txt; } }
};

// ------------------------------------------------------------- 身份 / 路由
async function loadDirectory() {
  state.dir = await api("/api/directory");
  const sel = $("#userSelect");
  sel.innerHTML = state.dir.users.map(u =>
    `<option value="${u.username}" ${u.username === state.username ? "selected" : ""}>` +
    `${u.display_name}（${({ inspector: "巡查员", dispatcher: "调度员", crew: "班组", supervisor: "主管", acceptor: "验收员" })[u.role]}）</option>`).join("");
  sel.onchange = () => {
    state.username = sel.value;
    localStorage.setItem("sw_user", sel.value);
    initUser(); refreshAll();
  };
  initUser();

  $("#fIssueKind").innerHTML =
    `<option value="">全部</option>` +
    Object.entries(ISSUE_KINDS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  $("#fCrew").innerHTML = `<option value="">全部班组</option>` +
    state.dir.crews.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
}
function initUser() {
  state.user = state.dir.users.find(u => u.username === state.username);
  $("#roleBadge").textContent = state.user
    ? ({ inspector: "巡查员", dispatcher: "调度员", crew: "班组", supervisor: "主管", acceptor: "验收员" })[state.user.role]
    : "—";
}
const role = () => state.user?.role;

function route() {
  const view = (location.hash || "#map").slice(1);
  $$("nav.tabs a").forEach(a => a.classList.toggle("active", a.dataset.view === view));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "map") renderMap();
  if (view === "patrol") renderOrders();
  if (view === "schedule") renderSchedule();
  if (view === "clean") renderCleanView();
  if (view === "vehicle") renderHandovers();
  if (view === "accept") renderAccepts();
}
window.addEventListener("hashchange", route);

async function refreshAll() {
  const [mp, orders, sched, ho, ac] = await Promise.all([
    api("/api/map"), api("/api/orders"), api("/api/schedule"),
    api("/api/handovers"), api("/api/acceptances"),
  ]);
  state.map = mp; state.orders = orders; state.schedule = sched;
  state.handovers = ho; state.accepts = ac;
  loadReminders();
  route();
}

// ------------------------------------------------------------- 地图
function renderMap() {
  const svg = $("#mapSvg");
  const { segments, inlets } = state.map;
  let html = "";

  // 浅灰路网装饰线
  for (let y = 70; y <= 470; y += 90)
    html += `<line class="road-h" x1="0" y1="${y}" x2="1000" y2="${y}"/>`;
  for (let x = 120; x <= 960; x += 170)
    html += `<line class="road-v" x1="${x}" y1="0" x2="${x - 30}" y2="520"/>`;

  // 片区多边形 + 名称 + 联系人
  segments.forEach(s => {
    const pts = JSON.parse(s.polygon).map(p => p.join(",")).join(" ");
    const cx = JSON.parse(s.polygon).reduce((a, p) => a + p[0], 0) / JSON.parse(s.polygon).length;
    const cy = 60;
    html += `<polygon class="seg-shape" points="${pts}"/>`;
    html += `<text class="seg-label" x="${cx - 70}" y="${cy}">${esc(s.name)}</text>`;
    html += `<text class="seg-label" style="font-size:11px;letter-spacing:0" x="${cx - 80}" y="${cy + 18}">联系人 ${esc(s.contact_name)} ${esc(s.contact_phone)}</text>`;
  });

  // 警戒范围（取自未闭环工单）
  const orderByInlet = {};
  state.orders.filter(o => o.status !== "accepted").forEach(o => { orderByInlet[o.inlet_id] = o; });
  inlets.forEach(i => {
    const o = orderByInlet[i.id];
    if (o && o.guard_radius_m) {
      html += `<circle class="guard" cx="${i.x}" cy="${i.y}" r="${Math.max(18, o.guard_radius_m * 1.4)}"/>`;
    }
  });

  // 点位：仅编号 + 堵塞等级颜色（不显示照片/工序，点击侧栏再展开）
  inlets.forEach(i => {
    const fill = LV_COLOR[i.status === "normal" ? 0 : Math.max(1, i.block_level)];
    const sel = state.selInlet === i.id ? " selected" : "";
    html += `<g class="inlet${sel}" data-id="${i.id}" transform="translate(${i.x},${i.y})">
      <circle class="pin" r="15" fill="${fill}"/>
      <text text-anchor="middle" y="4" style="font-size:10px;fill:#fff;stroke:none">${esc(i.code.slice(-4))}</text>
      <text class="lvlmark" text-anchor="middle" y="32">${LV_LABEL[i.status === "normal" ? 0 : Math.max(0, i.block_level)]}</text>
    </g>`;
  });
  svg.innerHTML = html;
  $$(".inlet", svg).forEach(g => g.onclick = () => openInletSidebar(Number(g.dataset.id)));

  $("#mapLegend").innerHTML = `
    <span><b>堵塞等级</b></span>
    <span><i class="dot" style="background:${LV_COLOR[0]}"></i>正常</span>
    <span><i class="dot" style="background:${LV_COLOR[1]}"></i>轻度</span>
    <span><i class="dot" style="background:${LV_COLOR[2]}"></i>中度</span>
    <span><i class="dot" style="background:${LV_COLOR[3]}"></i>严重</span>
    <span><svg width="26" height="14"><circle cx="8" cy="7" r="6" fill="none" stroke="#ef7b24" stroke-dasharray="4 3"/></svg>警戒范围</span>`;
}
$("#mapRefresh").onclick = () => wrap(refreshAll);

async function openInletSidebar(inletId) {
  state.selInlet = inletId;
  renderMap();
  const inlet = state.map.inlets.find(i => i.id === inletId);
  const orders = state.orders.filter(o => o.inlet_id === inletId)
    .sort((a, b) => b.id - a.id);
  const sb = $("#mapSidebar");
  sb.classList.add("open");
  sb.innerHTML = `<div class="sb-head">
      <div style="flex:1">
        <h3>${esc(inlet.code)} ${tag(inlet.status)} ${inlet.block_level ? lvlHtml(inlet.block_level) : ""}</div>
        <div class="sub">${esc(inlet.segment_name)} · 坐标(${inlet.x}, ${inlet.y})</div>
      </div>
      <button class="ghost small" id="sbClose">✕</button>
    </div><div class="sb-body" id="sbBody"><div class="muted">加载中…</div></div>`;
  $("#sbClose").onclick = () => { sb.classList.remove("open"); state.selInlet = null; renderMap(); };

  const body = $("#sbBody");
  if (!orders.length) {
    body.innerHTML = `<div class="sb-section"><h4>无未闭环/历史工单</h4>
      <p class="muted">巡查发现问题后可在「巡查列表」建单并划定警戒范围。</p></div>`;
    return;
  }
  let html = "";
  for (const o of orders) {
    const detail = await api(`/api/orders/${o.id}`);
    html += renderOrderDetailCard(detail, true);
  }
  body.innerHTML = html;
  bindOrderCardEvents(body);
}

function photoFrame(src, caption, small) {
  return `<div class="${small ? "" : "pf-wrap"}">
    ${caption ? `<div class="pf-cap">${caption}</div>` : ""}
    <div class="photo-frame"><img loading="lazy" src="${src}" alt=""></div>
  </div>`;
}
function photoFrameEmpty(caption) {
  return `<div class="pf-wrap"><div class="pf-cap">${caption}</div>
    <div class="photo-frame"><span class="empty">暂无照片</span></div></div>`;
}

function renderOrderDetailCard(o, compact) {
  const d = o.dispatches[o.dispatches.length - 1];
  const steps = new Set();
  const before = o.photos.filter(p => p.phase === "before");
  const after = o.dispatches.length
    ? o.photos.filter(p => p.dispatch_id === d.id && p.phase === "after" && p.kind === "clean") : [];
  const reset = o.dispatches.length
    ? o.photos.filter(p => p.dispatch_id === d.id && p.kind === "reset") : [];
  if (d) {
    // 需要 steps 明细：来自 dispatch 详情
  }
  const guard = o.guard_radius_m
    ? `<dt>警戒范围</dt><dd>半径 ${o.guard_radius_m}m ${esc(o.guard_note || "")}</dd>` : "";
  const kind = o.issue_kind ? `<span class="tag orange">${ISSUE_KINDS[o.issue_kind] || o.issue_kind}</span>` : "";
  const prio = o.priority ? `<span class="tag red">加急</span>` : "";
  let photosHtml = "";
  if (before.length || after.length || reset.length) {
    const cell = (p) => p
      ? `<img src="${photoUrl(p.path)}" style="width:100%;height:100%;object-fit:contain" alt="">`
      : `<span class="empty">暂无</span>`;
    photosHtml = `<div class="photo-grid">
      <div><div class="pf-cap">清掏前 ×${before.length}</div><div class="photo-frame">${cell(before[0])}</div></div>
      <div><div class="pf-cap">清掏后 ×${after.length}</div><div class="photo-frame">${cell(after[0])}</div></div>
      <div><div class="pf-cap">复位检查 ×${reset.length}</div><div class="photo-frame">${cell(reset[0])}</div></div>
    </div>`;
  }
  return `<div class="sb-section" data-order="${o.id}">
    <h4>${esc(o.code)} ${tag(o.status)} ${kind} ${prio}</h4>
    <dl class="kv">
      <dt>点位</dt><dd>${esc(o.inlet_code)}（${esc(o.segment_name)}）</dd>
      <dt>堵塞</dt><dd>${lvlHtml(o.block_level)}</dd>
      <dt>建单</dt><dd>${esc(o.created_by)} · ${fmt(o.created_at)}</dd>
      ${guard}
      <dt>说明</dt><dd>${esc(o.issue_note || "—")}</dd>
      ${o.reject_reason ? `<dt>退回原因</dt><dd style="color:var(--danger)">${esc(o.reject_reason)}</dd>` : ""}
    </dl>
    ${photosHtml ? `<div class="hr"></div>${photosHtml}` : ""}
    <div class="hr"></div>
    <div class="pill-row" id="cardActions-${o.id}"></div>
  </div>`;
}

function bindOrderCardEvents(scope) {
  $$(".sb-section[data-order]", scope).forEach(card => {
    const oid = Number(card.dataset.order);
    const row = state.orders.find(x => x.id === oid);
    const box = $(`#cardActions-${oid}`, card);
    if (!box) return;
    const btns = [];
    if (["open", "rejected"].includes(row.status))
      btns.push(["派工", "small", () => location.hash = "#schedule"]);
    if (d_canWork(row))
      btns.push(["现场记录", "small ghost", () => { state.currentDispatchId = lastDispatchId(oid); location.hash = "#clean"; }]);
    // 六个专项入口
    btns.push(["暴雨加急", "small orange", () => setStormUrgent(oid)]);
    btns.push(["井盖破损", "small danger", () => reportKind(oid, "cover_damaged")]);
    btns.push(["积水反涌", "small danger", () => reportKind(oid, "backflow")]);
    btns.push(["淤泥遗撒", "small danger", () => reportKind(oid, "silt_spill")]);
    btns.push(["车辆未到", "small danger", () => markMissing(lastDispatchId(oid))]);
    btns.push(["照片缺失", "small danger", () => markPhotoMissing(oid)]);
    btns.forEach(([t, c, fn]) => {
      const b = document.createElement("button");
      b.className = c; b.textContent = t; b.onclick = () => wrap(fn, b);
      box.appendChild(b);
    });
  });
}
const d_canWork = (o) => ["dispatched", "working", "arrived", "vehicle_missing"].includes(o.status);
const lastDispatchId = (oid) => {
  const o = state.orders.find(x => x.id === oid);
  return o ? state.schedule.filter(d => d.order_id === oid).sort((a, b) => b.id - a.id)[0]?.id : null;
};

async function setStormUrgent(oid) {
  const o = await api(`/api/orders/${oid}`);
  const due = new Date(Date.now() + 30 * 60000 - new Date().getTimezoneOffset() * 60000)
    .toISOString().slice(0, 16);
  modal("暴雨前加急", `
    <div class="notice info">工单 ${esc(o.code)} 标记为加急并生成「暴雨前复查提醒」。</div>
    <dl class="kv">
      <dt>点位</dt><dd>${esc(o.inlet_code)} ${esc(o.segment_name)}</dd>
      <dt>当前</dt><dd>${tag(o.status)} ${lvlHtml(o.block_level)}</dd>
    </dl>
    <label class="field">复查时间 <input type="datetime-local" id="mDue" value="${due}"></label>
    <label class="field">备注 <input id="mNote" value="暴雨橙色预警，优先加急"></label>`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "设为加急并提醒", cls: "orange", onClick: async (b) => {
       await wrap(async () => {
         await api(`/api/orders/${oid}/issues`, { method: "POST", body: { issue_kind: "storm_urgent", note: $("#mNote").value } });
         await api("/api/reminders", { method: "POST", body: {
           order_id: oid, inlet_id: o.inlet_id, due_at: $("#mDue").value,
           message: `暴雨前复查：${o.inlet_code} 须在降雨前再次开盖核查` } });
         closeModal(); toast("已设为暴雨前加急并生成复查提醒", true);
         await refreshAll();
       }, b);
     } }], true);
}
async function reportKind(oid, kind) {
  const needGuard = kind === "cover_damaged";
  modal(ISSUE_KINDS[kind], `
    <p class="muted">在原工单 ${esc(state.orders.find(o => o.id === oid).code)} 上追加专项问题，不另开新单。</p>
    <label class="field">情况说明 <textarea id="mNote" placeholder="${kind === "backflow" ? "如：井底反涌，水位距井箅 20cm" : kind === "silt_spill" ? "如：清掏淤泥约 2 袋遗撒路缘未清运" : "如：井盖裂纹约 15cm，已锥桶围护"}"></textarea></label>
    ${needGuard ? `<label class="field">警戒半径（米，必填）<input type="number" id="mGuard" value="10" min="1" step="0.5"></label>` : ""}`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "登记", cls: "danger", onClick: async (b) => {
       await wrap(async () => {
         await api(`/api/orders/${oid}/issues`, { method: "POST",
           body: { issue_kind: kind, note: $("#mNote").value } });
         if (needGuard && $("#mGuard")) {
           // 警戒范围通过建单校验：井盖破损由后端自动补默认，也在说明里带上半径
           const v = Number($("#mGuard").value);
           if (v > 0) await api(`/api/orders/${oid}/issues`, { method: "POST",
             body: { issue_kind: kind, note: `警戒半径 ${v}m` } });
         }
         closeModal(); toast("已登记：" + ISSUE_KINDS[kind], true);
         await refreshAll();
       }, b);
     } }]);
}
async function markPhotoMissing(oid) {
  modal("照片缺失登记", `<label class="field">缺失说明 <textarea id="mNote" placeholder="如：复位照片未拍，班组收车后补传"></textarea></label>
    <p class="muted">登记后仍须补齐前/后照片与复位检查，验收门禁才会放行。</p>`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "登记缺失", cls: "danger", onClick: async (b) => {
       await wrap(async () => {
         await api(`/api/orders/${oid}/photo-missing`, { method: "POST", body: { note: $("#mNote").value } });
         closeModal(); toast("已登记照片缺失，待补传", true); await refreshAll();
       }, b);
     } }]);
}
async function markMissing(did) {
  if (!did) return toast("该工单尚无派工，无法登记车辆未到");
  modal("车辆未到", `<label class="field">情况说明 <textarea id="mNote">车辆未按计划到场，已通知调度改派</textarea></label>`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "登记车辆未到", cls: "danger", onClick: async (b) => {
       await wrap(async () => {
         await api(`/api/dispatches/${did}/missing`, { method: "POST", body: { note: $("#mNote").value } });
         closeModal(); toast("已登记车辆未到，可在车辆交接页改派", true); await refreshAll();
       }, b);
     } }]);
}

// ------------------------------------------------------------- 巡查列表
function renderOrders() {
  const st = $("#fOrderStatus").value;
  const kd = $("#fIssueKind").value;
  const rows = state.orders.filter(o => (!st || o.status === st) && (!kd || o.issue_kind === kd));
  const t = $("#ordersTable");
  t.innerHTML = `<thead><tr>
    <th>工单号</th><th>雨水口</th><th>区段</th><th>堵塞等级</th><th>专项</th>
    <th>状态</th><th>警戒</th><th>建单时间</th><th>派工</th><th></th>
  </tr></thead><tbody>` +
    (rows.length ? rows.map(o => `<tr>
      <td class="mono">${esc(o.code)}${o.priority ? ' <span class="tag red">加急</span>' : ""}</td>
      <td><b>${esc(o.inlet_code)}</b></td>
      <td>${esc(o.segment_name)}</td>
      <td>${lvlHtml(o.block_level)}</td>
      <td>${o.issue_kind ? `<span class="tag orange">${ISSUE_KINDS[o.issue_kind] || esc(o.issue_kind)}</span>` : "—"}</td>
      <td>${tag(o.status)}</td>
      <td>${o.guard_radius_m ? `${o.guard_radius_m}m` : "—"}</td>
      <td>${fmt(o.created_at)}</td>
      <td>${o.dispatch_count}</td>
      <td><button class="ghost small" data-order="${o.id}">处理</button></td>
    </tr>`).join("") : `<tr><td colspan="10" class="muted" style="text-align:center;padding:24px">没有匹配工单</td></tr>`) +
    `</tbody>`;
  $$("button[data-order]", t).forEach(b => b.onclick = () => openOrderModal(Number(b.dataset.order)));
}
$("#fOrderStatus").onchange = renderOrders;
$("#fIssueKind").onchange = renderOrders;

async function openOrderModal(oid) {
  const o = await api(`/api/orders/${oid}`);
  let stepsHtml = "";
  const d = o.dispatches[o.dispatches.length - 1];
  if (d) {
    const dd = await api(`/api/dispatches/${d.id}`);
    const done = new Set(dd.steps.map(s => s.step));
    stepsHtml = `<ul class="steps">${STEP_ORDER.map(s =>
      `<li class="${done.has(s) ? "done" : "todo"}"><span class="box">✓</span>${STEP_LABELS[s]}
        <span class="meta">${done.has(s) ? fmt(dd.steps.find(x => x.step === s).done_at) : "未记录"}</span></li>`).join("")}</ul>
    <p class="muted">袋数：${dd.output?.bag_count ?? "—"} ｜ 过磅：${pct(dd.output?.measured_kg)}kg
       ${dd.output?.revised_kg != null ? `｜主管修订：${pct(dd.output.revised_kg)}kg` : ""}</p>`;
  }
  const photosHtml = `<div class="photo-pair">
      ${o.photos.find(p => p.phase === "before") ? photoFrame(photoUrl(o.photos.find(p => p.phase === "before").path), "清掏前") : photoFrameEmpty("清掏前")}
      ${o.photos.find(p => p.phase === "after" && p.kind === "clean") ? photoFrame(photoUrl(o.photos.find(p => p.phase === "after" && p.kind === "clean").path), "清掏后") : photoFrameEmpty("清掏后")}
      ${o.photos.find(p => p.kind === "reset") ? photoFrame(photoUrl(o.photos.find(p => p.kind === "reset").path), "复位检查") : photoFrameEmpty("复位检查")}
    </div>`;
  modal(`工单 ${o.code}`,
    `<dl class="kv">
       <dt>雨水口</dt><dd><b>${esc(o.inlet_code)}</b>（${esc(o.segment_name)}，联系人 ${esc(o.contact_name)} ${esc(o.contact_phone)}）</dd>
       <dt>状态</dt><dd>${tag(o.status)} ${lvlHtml(o.block_level)} ${o.priority ? '<span class="tag red">加急</span>' : ""}</dd>
       <dt>问题</dt><dd>${o.issue_kind ? `<span class="tag orange">${ISSUE_KINDS[o.issue_kind]}</span> ` : ""}${esc(o.issue_note || "—")}</dd>
       <dt>警戒</dt><dd>${o.guard_radius_m ? `半径 ${o.guard_radius_m}m，${esc(o.guard_note || "")}` : "未划定"}</dd>
       ${o.reject_reason ? `<dt>退回</dt><dd style="color:var(--danger)">${esc(o.reject_reason)}<br><span class="muted">沿用原工单继续处理，不另开新单</span></dd>` : ""}
     </div>
     <div class="sb-section"><h4>工序（最新派工）</h4>${stepsHtml || '<p class="muted">尚未派工</p>'}</div>
     <div class="sb-section"><h4>照片</h4>${photosHtml}</div>
     ${o.feedbacks.length ? `<div class="sb-section"><h4>居民反馈</h4>${o.feedbacks.map(f =>
       `<p class="muted">${esc(f.reporter)}：${esc(f.content)} ${f.handled ? "（已处理）" : ""}</p>`).join("")}</div>` : ""}
     <div class="pill-row" id="mOrderActions"></div>`,
    [{ text: "关闭", cls: "ghost", onClick: closeModal }], true);
  const ab = $("#mOrderActions");
  const add = (txt, cls, fn) => { const b = document.createElement("button"); b.className = cls; b.textContent = txt; b.onclick = () => wrap(fn, b); ab.appendChild(b); };
  add("暴雨加急", "orange", async () => { closeModal(); await setStormUrgent(oid); });
  add("井盖破损", "danger", async () => { closeModal(); await reportKind(oid, "cover_damaged"); });
  add("积水反涌", "danger", async () => { closeModal(); await reportKind(oid, "backflow"); });
  add("淤泥遗撒", "danger", async () => { closeModal(); await reportKind(oid, "silt_spill"); });
  add("车辆未到", "danger", async () => { closeModal(); await markMissing(d?.id); });
  add("照片缺失", "danger", async () => { closeModal(); await markPhotoMissing(oid); });
  if (d) add("去现场记录", "ghost", async () => { closeModal(); state.currentDispatchId = d.id; location.hash = "#clean"; });
}

// 建单
$("#btnCreateOrder").onclick = () => {
  const free = state.map.inlets;
  modal("巡查建单 / 划定警戒范围", `
    <div class="kind-grid" id="kindGrid">
      <button data-k="" class="ghost" style="border-style:dashed">常规巡查</button>
      ${Object.entries(ISSUE_KINDS).filter(([k]) => !["vehicle_missing", "photo_missing"].includes(k))
        .map(([k, v]) => `<button data-k="${k}" class="ghost">${v}</button>`).join("")}
    </div>
    <div class="inline-fields">
      <label class="field" style="flex:1">雨水口
        <select id="mInlet">${free.map(i => `<option value="${i.id}">${i.code}（${i.segment_name}）</option>`).join("")}</select>
      </label>
      <label class="field">堵塞等级
        <select id="mLevel"><option value="1">1 轻度</option><option value="2">2 中度</option><option value="3" selected>3 严重</option></select>
      </label>
    </div>
    <label class="field">问题说明 <textarea id="mIssueNote" placeholder="淤积情况、异味、积水表现…"></textarea></label>
    <div class="inline-fields">
      <label class="field">警戒半径（米，可空）<input type="number" id="mGuard" min="0" step="0.5" placeholder="如 12"></label>
      <label class="field">警戒措施 <input id="mGuardNote" placeholder="锥桶 / 警戒带 / 围挡"></label>
      <label class="field"><span>&nbsp;</span><label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="mPrio"> 暴雨前加急</label></label>
      <label class="field"><span>&nbsp;</span><label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="mRecheck" checked> 生成复查提醒</label></label>
    </div>`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "建单", cls: "orange", onClick: async (b) => {
       await wrap(async () => {
         const kind = $("#kindGrid .selected")?.dataset.k || "";
         await api("/api/orders", { method: "POST", body: {
           inlet_id: Number($("#mInlet").value),
           block_level: Number($("#mLevel").value),
           issue_kind: kind || null,
           issue_note: $("#mIssueNote").value,
           guard_radius_m: Number($("#mGuard").value) || null,
           guard_note: $("#mGuardNote").value || null,
           priority: $("#mPrio").checked || kind === "storm_urgent" ? 1 : 0,
           schedule_recheck: $("#mRecheck").checked,
         } });
         closeModal(); toast("已建单并在地图显示警戒范围", true); await refreshAll();
       }, b);
     } }]);
  $$("#kindGrid button").forEach(btn => btn.onclick = () => {
    $$("#kindGrid button").forEach(x => { x.classList.remove("orange", "selected"); x.classList.add("ghost"); });
    btn.classList.add("orange", "selected"); btn.classList.remove("ghost");
    if (btn.dataset.k === "storm_urgent") $("#mPrio").checked = true;
    if (btn.dataset.k === "cover_damaged" && !$("#mGuard").value) $("#mGuard").value = 10;
  });
};

// 居民反馈
$("#btnFeedback").onclick = () => {
  modal("居民反馈", `
    <div class="inline-fields">
      <label class="field" style="flex:1">联系人 <input id="fReporter" placeholder="如 刘阿姨"></label>
      <label class="field" style="flex:1">电话 <input id="fContact" placeholder="选填"></label>
    </div>
    <label class="field">关联雨水口（选填）
      <select id="fInlet"><option value="">不关联</option>
      ${state.map.inlets.map(i => `<option value="${i.id}">${i.code}</option>`).join("")}</select></label>
    <label class="field">分类
      <select id="fCat"><option>积水</option><option>异味</option><option>井盖异响</option><option>表扬</option><option>其他</option></select></label>
    <label class="field">内容 <textarea id="fContent"></textarea></label>
    <div id="fbList"></div>`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "提交反馈", onClick: async (b) => {
       await wrap(async () => {
         await api("/api/feedbacks", { method: "POST", body: {
           reporter: $("#fReporter").value || "匿名居民", contact: $("#fContact").value,
           category: $("#fCat").value, content: $("#fContent").value,
           inlet_id: Number($("#fInlet").value) || null } });
         closeModal(); toast("反馈已登记", true);
       }, b);
     } }], true);
  loadFeedbackList();
};
async function loadFeedbackList() {
  const list = await api("/api/feedbacks");
  $("#fbList").innerHTML = `<div class="hr"></div><h4 style="margin:6px 0">近期反馈</h4>` +
    list.slice(0, 6).map(f => `<div class="notice ${f.handled ? "ok" : "info"}" style="margin-bottom:6px">
      <b>${esc(f.reporter)}</b> ${f.inlet_code ? "·" + f.inlet_code : ""} · ${esc(f.category || "")}
      <br>${esc(f.content)} ${f.handled ? "✔ " + esc(f.handle_note || "") : ""}</div>`).join("");
}

// ------------------------------------------------------------- 班组日程
$("#fCrew").onchange = renderSchedule;
$("#btnDispatch").onclick = openDispatchModal;

function renderSchedule() {
  const crew = Number($("#fCrew").value) || 0;
  const rows = state.schedule.filter(d => !crew || d.crew_id === crew);

  // 甘特：时间窗
  const board = $("#scheduleBoard");
  if (!rows.length) { board.innerHTML = `<div class="muted" style="padding:18px">近期无派工</div>`; }
  else {
    const times = rows.flatMap(d => [new Date(d.start_at), new Date(d.end_at)]);
    let t0 = Math.min(...times), t1 = Math.max(...times);
    if (t1 <= t0) t1 = t0 + 3600000;
    const crews = state.dir.crews.filter(c => !crew || c.id === crew);
    board.innerHTML = crews.map(c => {
      const items = rows.filter(d => d.crew_id === c.id);
      const blocks = items.map(d => {
        const x = (new Date(d.start_at) - t0) / (t1 - t0) * 100;
        const w = Math.max(4, (new Date(d.end_at) - new Date(d.start_at)) / (t1 - t0) * 100);
        const cls = d.merged_group_id ? "merged" : d.status === "vehicle_missing" ? "missing" : "";
        return `<div class="sched-block ${cls}" style="left:${x}%;width:${w}%" data-d="${d.id}"
          title="${esc(d.order_code)} ${esc(d.inlet_code)} ${fmt(d.start_at)}~${fmt(d.end_at)}">
          ${d.merged_group_id ? "🔗合单 " : ""}${esc(d.inlet_code)} ${esc(d.vehicle_plate || "无车")}</div>`;
      }).join("");
      return `<div class="sched-row"><div class="crew-cell">${esc(c.name)}<br>
        <span class="muted" style="font-weight:400">${esc(c.leader)} ${esc(c.phone)}</span></div>
        <div class="sched-track">${blocks}</div></div>`;
    }).join("");
    $$(".sched-block", board).forEach(b => b.onclick = () => {
      state.currentDispatchId = Number(b.dataset.d); location.hash = "#clean";
    });
  }

  const t = $("#scheduleTable");
  t.innerHTML = `<thead><tr><th>派工号</th><th>工单</th><th>雨水口</th><th>班组</th><th>车辆</th>
    <th>计划时段</th><th>状态</th><th>合单</th><th></th></tr></thead><tbody>` +
    (rows.length ? rows.map(d => `<tr>
      <td class="mono">${esc(d.code)}</td>
      <td class="mono">${esc(d.order_code)}</td>
      <td>${esc(d.inlet_code)}</td>
      <td>${esc(d.crew_name)}</td>
      <td>${esc(d.vehicle_plate || "—")}</td>
      <td>${fmt(d.start_at)} ~ ${fmt(d.end_at)}</td>
      <td>${tag(d.status)}</td>
      <td>${d.merge_group_id ? `<span class="tag orange">合单同车</span>` : "—"}</td>
      <td><button class="ghost small" data-d="${d.id}">现场记录</button></td>
    </tr>`).join("") : `<tr><td colspan="9" class="muted" style="text-align:center;padding:20px">无派工</td></tr>`) +
    `</tbody>`;
  $$("button[data-d]", t).forEach(b => b.onclick = () => {
    state.currentDispatchId = Number(b.dataset.d); location.hash = "#clean";
  });

  // 清掏页下拉
  $("#cleanDispatch").innerHTML = state.schedule.map(d =>
    `<option value="${d.id}" ${d.id === state.currentDispatchId ? "selected" : ""}>${d.code} · ${d.inlet_code} · ${d.crew_name}</option>`).join("");
}

function openDispatchModal() {
  const openOrders = state.orders.filter(o => ["open", "rejected"].includes(o.status));
  modal("派工 / 相邻点位合并派车", `
    <div class="notice info">勾选 ≥2 个同一道路区段、相距 ≤150m 的点位可合并派同一辆车；
      <b>合单只合并派车，验收仍按每个雨水口/工单分别进行</b>。同一班组时间重叠会被阻止。</div>
    <table class="grid" style="font-size:13px"><thead><tr>
      <th></th><th>工单</th><th>雨水口</th><th>区段</th><th>堵塞</th><th>状态</th></tr></thead><tbody>
      ${openOrders.map(o => `<tr>
        <td><input type="checkbox" class="ord-chk" value="${o.id}" data-seg="${o.segment_id}" data-xy="${o.x},${o.y}"></td>
        <td class="mono">${esc(o.code)}</td><td>${esc(o.inlet_code)}</td>
        <td>${esc(o.segment_name)}</td><td>${LV_LABEL[o.block_level]}</td>${""}
        <td>${tag(o.status)}</td></tr>`).join("") ||
      `<tr><td colspan="6" class="muted" style="text-align:center">没有待派工单（退回工单可重新派工）</td></tr>`}</tbody></table>
    <div id="mergeWarn" class="notice err hidden"></div>
    <div class="inline-fields">
      <label class="field">班组 <select id="dCrew">${state.dir.crews.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select></label>
      <label class="field">车辆 <select id="dVeh"><option value="">暂不派车</option>${state.dir.vehicles.map(v => `<option value="${v.id}">${esc(v.plate)}（载重${v.capacity_kg}kg）</option>`).join("")}</select></label>
      <label class="field">开始 <input type="datetime-local" id="dStart"></label>
      <label class="field">结束 <input type="datetime-local" id="dEnd"></label>
    </div>`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "下达派工", cls: "orange", onClick: async (b) => {
       await wrap(async () => {
         const ids = $$(".ord-chk:checked").map(x => Number(x.value));
         if (!ids.length) throw new Error("请勾选至少一个工单");
         const r = await api("/api/dispatches", { method: "POST", body: {
           order_ids: ids, crew_id: Number($("#dCrew").value),
           vehicle_id: Number($("#dVeh").value) || null,
           start_at: $("#dStart").value, end_at: $("#dEnd").value } });
         closeModal();
         toast(r.merge_group_id ? `已合并派车：${ids.length} 个点位，${r.crew}` : `派工成功：${r.crew}`, true);
         await refreshAll();
       }, b);
     } }], true);

  const nowd = new Date();
  const loc = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  $("#dStart").value = loc(new Date(nowd.getTime() + 3600000));
  $("#dEnd").value = loc(new Date(nowd.getTime() + 3 * 3600000));

  $$(".ord-chk").forEach(chk => chk.onchange = () => {
    const picked = $$(".ord-chk:checked");
    const warn = $("#mergeWarn");
    if (picked.length < 2) { warn.classList.add("hidden"); return; }
    const segs = new Set(picked.map(x => x.dataset.seg));
    let msg = "";
    if (segs.size > 1) msg = "跨道路区段不能合并派车";
    else {
      const pts = picked.map(x => x.dataset.xy.split(",").map(Number));
      for (let i = 1; i < pts.length; i++) {
        const dist = Math.hypot(pts[0][0] - pts[i][0], pts[0][1] - pts[i][1]);
        if (dist > 150) msg = `与首个点位相距 ${dist.toFixed(0)}m，超过相邻合单上限 150m`;
      }
    }
    warn.textContent = msg;
    warn.classList.toggle("hidden", !msg);
  });
}

// ------------------------------------------------------------- 清掏记录
$("#cleanDispatch").onchange = () => { state.currentDispatchId = Number($("#cleanDispatch").value); renderCleanView(); };
$("#cleanRefresh").onclick = () => wrap(async () => { await refreshAll(); });

async function renderCleanView() {
  const body = $("#cleanBody");
  if (!$("#cleanDispatch").options.length) {
    $("#cleanDispatch").innerHTML = state.schedule.map(d =>
      `<option value="${d.id}">${d.code} · ${d.inlet_code} · ${d.crew_name}</option>`).join("");
  }
  const id = state.currentDispatchId || Number($("#cleanDispatch").value) ||
    state.schedule[0]?.id;
  if (!id) { body.innerHTML = `<div class="card" style="padding:20px" class="muted">暂无派工</div>`; return; }
  state.currentDispatchId = id;
  $("#cleanDispatch").value = String(id);
  const d = await api(`/api/dispatches/${id}`);
  const o = await api(`/api/orders/${d.order_id}`);
  const done = new Set(d.steps.map(s => s.step));
  const stepsHtml = STEP_ORDER.map((s, i) => {
    const rec = d.steps.find(x => x.step === s);
    const can = i === 0 || done.has(STEP_ORDER[i - 1]);
    return `<li class="${done.has(s) ? "done" : "todo"}">
      <span class="box">✓</span><b style="width:48px">${STEP_LABEL[s]}</b>
      <span class="meta" style="flex:1">${rec ? `${fmt(rec.done_at)} · ${esc(rec.operator)}${rec.note ? " · " + esc(rec.note) : ""}` : (can ? "可记录" : `需先完成${STEP_LABEL[STEP_ORDER[i - 1]]}`)}</span>
      ${!done.has(s) ? `<button class="small" data-step="${s}" ${can ? "" : "disabled"}>记录</button>` : ""}
    </li>`;
  }).join("");

  body.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px" id="cleanGrid">
      <div class="card" style="padding:14px 16px">
        <h3 style="margin:0 0 4px">${esc(d.order_code)} · ${esc(d.inlet_code)} ${tag(d.order_status)}</h3>
        <p class="muted" style="margin:4px 0 12px">${esc(d.segment_name)} ｜ ${esc(d.crew_name)} ｜ ${esc(d.vehicle_plate || "未派车")} ｜ ${fmt(d.start_at)} ~ ${fmt(d.end_at)}</p>
        <h4>工序（开盖 → 清掏 → 冲洗 → 复位，须依次记录）</h4>
        <ul class="steps">${stepsHtml}</ul>
        <div class="hr"></div>
        <div class="inline-fields">
          <label class="field">淤泥袋数
            <input type="number" id="bagInput" min="0" value="${d.output?.bag_count ?? 0}" style="width:110px"></label>
          <button class="ghost small" id="bagBtn">登记袋数</button>
          <span class="muted">中度及以上堵塞袋数为 0 将无法通过验收</span>
        </div>
      </div>
      <div class="card" style="padding:14px 16px">
        <h4 style="margin-top:0">现场照片（落到本地 uploads/）</h4>
        <div class="photo-grid" id="photoSlots">
          ${photoSlot("清掏前", "before", "clean", o.photos.find(p => p.phase === "before"))}
          ${photoSlot("清掏后", "after", "clean", d.photos.find(p => p.phase === "after" && p.kind === "clean"), d.id)}
          ${photoSlot("复位检查", "after", "reset", d.photos.find(p => p.kind === "reset"), d.id)}
        </div>
        <div class="hr"></div>
        <h4>车辆与过磅</h4>
        <div class="pill-row">
          <button class="ghost small" id="btnArrive">车辆到场</button>
          <button class="danger small" id="btnMissing">车辆未到</button>
          <button class="small" id="btnWeigh">模拟过磅</button>
        </div>
        <p class="muted" id="weighInfo">袋数 ${d.output?.bag_count ?? "—"} ｜ 过磅净重 ${pct(d.output?.measured_kg)}kg
          ${d.output?.revised_kg != null ? `｜主管修订 ${pct(d.output.revised_kg)}kg（${esc(d.output.revise_reason || "")}）` : ""}</p>
        ${role() === "supervisor" ? `
        <div class="inline-fields">
          <label class="field">主管修订重量（kg）<input type="number" id="revKg" step="0.1" value="${d.output?.revised_kg ?? d.output?.measured_kg ?? ""}"></label>
          <label class="field" style="flex:1">修订原因（必填）<input id="revReason" placeholder="如：含袋重与雨水，核减…"></label>
          <button class="orange small" id="btnRevise">提交修订</button>
        </div>` : `<p class="muted">清运重量修订仅主管可操作。</p>`}
      </div>
    </div>
    <div class="card" style="padding:14px 16px;margin-top:14px">
      <h4 style="margin-top:0">申请验收门禁检查</h4>
      <ul class="checklist" id="gateList"></ul>
      <div class="pill-row" style="margin-top:10px">
        <button class="orange" id="btnRequestAccept">申请验收</button>
        <a class="btn ghost small" href="#accept">前往验收台账</a>
      </div>
    </div>`;

  $$("button[data-step]", body).forEach(b => b.onclick = () => wrap(async () => {
    await api(`/api/dispatches/${id}/steps`, { method: "POST", body: { step: b.dataset.step } });
    toast(`${STEP_LABEL[b.dataset.step]}已记录`, true); await refreshAll();
  }, b));
  $("#bagBtn").onclick = () => wrap(async () => {
    await api(`/api/dispatches/${id}/bags`, { method: "POST", body: { bag_count: Number($("#bagInput").value) } });
    toast("袋数已登记", true); await refreshAll();
  }, $("#bagBtn"));
  $("#btnArrive").onclick = () => wrap(async () => {
    await api(`/api/dispatches/${id}/arrive`, { method: "POST" });
    toast("车辆到场已登记", true); await refreshAll();
  }, $("#btnArrive"));
  $("#btnMissing").onclick = () => wrap(async () => { closeModal(); await markMissing(id); await refreshAll(); });
  $("#btnWeigh").onclick = () => wrap(async () => {
    const r = await api(`/api/dispatches/${id}/weigh`, { method: "POST" });
    toast(`模拟地磅返回净重 ${r.net_kg}kg（毛重 ${r.gross_kg}kg / 皮重 ${r.tare_kg}kg）`, true);
    await refreshAll();
  }, $("#btnWeigh"));
  const revBtn = $("#btnRevise");
  if (revBtn) revBtn.onclick = () => wrap(async () => {
    const r = await api(`/api/dispatches/${id}/revise-weight`, { method: "POST",
      body: { new_kg: Number($("#revKg").value), reason: $("#revReason").value } });
    toast(`重量已修订为 ${r.final_kg}kg，已留审计记录`, true); await refreshAll();
  }, revBtn);

  bindPhotoUploads(o.id, id);
  renderGate(o, d);
  $("#btnRequestAccept").onclick = () => wrap(async () => {
    await api(`/api/orders/${o.id}/acceptance/request`, { method: "POST" });
    toast("验收申请已提交，等待验收员结论", true); await refreshAll();
  }, $("#btnRequestAccept"));
}

function photoSlot(caption, phase, kind, photo, dispatchId) {
  return `<div>
    <div class="pf-cap">${caption}</div>
    <div class="photo-frame">${photo ? `<img src="${photoUrl(photo.path)}">` : `<span class="empty">未拍摄</span>`}</div>
    <label class="small" style="display:block;margin-top:5px">
      <input type="file" accept="image/*" data-phase="${phase}" data-kind="${kind}"
        data-dispatch="${dispatchId || ""}" style="font-size:12px">
    </label></div>`;
}
function bindPhotoUploads(orderId, dispatchId) {
  $$("#photoSlots input[type=file]").forEach(inp => {
    inp.onchange = async () => {
      const f = inp.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("phase", inp.dataset.phase);
      fd.append("kind", inp.dataset.kind);
      if (inp.dataset.dispatch) fd.append("dispatch_id", inp.dataset.dispatch);
      fd.append("file", f);
      await wrap(async () => {
        await api(`/api/orders/${orderId}/photos`, { method: "POST", body: fd });
        toast("照片已上传到本地附件目录", true); await refreshAll();
      });
    };
  });
}

function renderGate(o, d) {
  const done = new Set(d.steps.map(s => s.step));
  const before = o.photos.some(p => p.phase === "before" && p.kind === "clean");
  const after = d.photos.some(p => p.phase === "after" && p.kind === "clean");
  const reset = d.photos.some(p => p.kind === "reset");
  const bags = d.output?.bag_count != null;
  const checks = [
    ["工序：开盖", done.has("open")],
    ["工序：清掏", done.has("clean")],
    ["工序：冲洗", done.has("flush")],
    ["工序：复位检查", done.has("reset")],
    ["清掏前照片", before],
    ["清掏后照片", after],
    ["复位检查照片", reset],
    ["淤泥袋数已登记", bags && d.output.bag_count >= 0],
  ];
  $("#gateList").innerHTML = checks.map(([t, ok]) =>
    `<li class="${ok ? "good" : "bad"}">${ok ? "✔" : "✘"} ${t}</li>`).join("") +
    `<li class="muted">全部齐全才能申请验收；验收退回后沿用本工单继续处理。</li>`;
}

// ------------------------------------------------------------- 车辆交接
$("#vehicleRefresh").onclick = () => wrap(refreshAll);
function renderHandovers() {
  const t = $("#handoverTable");
  t.innerHTML = `<thead><tr><th>派工/工单</th><th>雨水口</th><th>班组</th><th>车辆</th>
    <th>计划到场</th><th>实际到场</th><th>状态</th><th>袋数</th><th>计量(kg)</th><th></th></tr></thead><tbody>` +
    (state.handovers.length ? state.handovers.map(h => `<tr>
      <td class="mono">${esc(h.dispatch_code)}<br><span class="muted">${esc(h.order_code)}</span></td>
      <td>${esc(h.inlet_code)}</td><td>${esc(h.crew_name)}</td>
      <td>${esc(h.vehicle_plate || "—")}</td>
      <td>${fmt(h.planned_arrival)}</td><td>${fmt(h.arrived_at)}</td>
      <td>${tag(h.status)}</td>
      <td>${h.bags_loaded ?? h.bag_count ?? "—"}</td>
      <td>${h.revised_kg != null ? `<b>${pct(h.revised_kg)}</b> <span class="muted">(原${pct(h.measured_kg)})</span>` : pct(h.measured_kg)}</td>
      <td><div class="pill-row">
        <button class="ghost small" data-act="arrive" data-d="${h.dispatch_id}">到场</button>
        <button class="danger small" data-act="missing" data-d="${h.dispatch_id}">未到</button>
        <button class="ghost small" data-act="reassign" data-d="${h.dispatch_id}">改派</button>
        <button class="small" data-act="weigh" data-d="${h.dispatch_id}">过磅</button>
        <button class="orange small" data-act="revise" data-d="${h.dispatch_id}" ${role() === "supervisor" ? "" : "title='仅主管'"}>修订</button>
      </div></td></tr>`).join("") : `<tr><td colspan="10" class="muted" style="text-align:center;padding:20px">暂无交接记录</td></tr>`) +
    `</tbody>`;
  $$("button[data-act]", t).forEach(b => b.onclick = () => wrap(async () => {
    const did = Number(b.dataset.d);
    if (b.dataset.act === "arrive") { await api(`/api/dispatches/${did}/arrive`, { method: "POST" }); toast("到场已登记", true); }
    if (b.dataset.act === "missing") { await markMissing(did); }
    if (b.dataset.act === "weigh") {
      const r = await api(`/api/dispatches/${did}/weigh`, { method: "POST" });
      toast(`模拟地磅：净重 ${r.net_kg}kg`, true);
    }
    if (b.dataset.act === "reassign") openReassign(did);
    if (b.dataset.act === "revise") openRevise(did, b);
    await refreshAll();
  }, b));
}
function openReassign(did) {
  modal("改派清运车辆（车辆未到）", `
    <label class="field">车辆 <select id="rsVeh">${state.dir.vehicles.map(v =>
      `<option value="${v.id}">${esc(v.plate)} · ${esc(v.driver)} · 载重${v.capacity_kg}kg</option>`).join("")}</select></label>
    <p class="muted">合单内各点位派工将一并换车；合单仍不合并验收。</p>`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "确认改派", cls: "orange", onClick: async (b) => {
       await wrap(async () => {
         await api(`/api/dispatches/${did}/vehicle`, { method: "POST", body: { vehicle_id: Number($("#rsVeh").value) } });
         closeModal(); toast("已改派车辆", true); await refreshAll();
       }, b);
     } }]);
}
function openRevise(did, btn) {
  if (role() !== "supervisor") return toast("清运重量修订仅主管可执行");
  modal("主管修订清运重量", `
    <label class="field">修订后重量（kg）<input type="number" step="0.1" id="rvKg"></label>
    <label class="field">修订原因（必填，将写入审计）<textarea id="rvReason" placeholder="如：过磅混有空袋/雨水；联单与现场袋数不符…"></textarea></label>
    <p class="muted">修订过程记录于 weight_revisions，保留原值、新值、原因与操作人。</p>`,
    [{ text: "取消", cls: "ghost", onClick: closeModal },
     { text: "提交修订", cls: "orange", onClick: async (b) => {
       await wrap(async () => {
         const r = await api(`/api/dispatches/${did}/revise-weight`, { method: "POST",
           body: { new_kg: Number($("#rvKg").value), reason: $("#rvReason").value } });
         closeModal(); toast(`已修订为 ${r.final_kg}kg`, true); await refreshAll();
       }, b);
     } }]);
}

// ------------------------------------------------------------- 验收台账
$("#fAcceptResult").onchange = renderAccepts;
$("#acceptRefresh").onclick = () => wrap(refreshAll);
function renderAccepts() {
  const rf = $("#fAcceptResult").value;
  const rows = state.accepts.filter(a => !rf || a.result === rf);
  const t = $("#acceptTable");
  t.innerHTML = `<thead><tr><th>验收号</th><th>工单</th><th>雨水口</th><th>区段</th><th>班组</th>
    <th>申请时间</th><th>结论</th><th>验收人</th><th>意见</th><th></th></tr></thead><tbody>` +
    (rows.length ? rows.map(a => `<tr>
      <td class="mono">#${a.id}</td>
      <td class="mono">${esc(a.order_code)}</td>
      <td><b>${esc(a.inlet_code)}</b></td>
      <td>${esc(a.segment_name)}</td><td>${esc(a.crew_name)}</td>
      <td>${fmt(a.created_at)}</td>
      <td>${tag(a.result)}</td>
      <td>${esc(a.reviewer)}${a.decided_at ? `<br><span class="muted">${fmt(a.decided_at)}</span>` : ""}</td>
      <td style="max-width:220px">${esc(a.comment || "—")}</td>
      <td>${a.result === "pending" ? `<button class="orange small" data-a="${a.id}" data-o="${a.order_id}" data-d="${a.dispatch_id}">验收</button>` : `<button class="ghost small" data-a="${a.id}" data-o="${a.order_id}" data-d="${a.dispatch_id}">查看</button>`}</td>
    </tr>`).join("") : `<tr><td colspan="10" class="muted" style="text-align:center;padding:20px">暂无记录</td></tr>`) +
    `</tbody>`;
  $$("button[data-a]", t).forEach(b => b.onclick = () => openAcceptance(Number(b.dataset.o), Number(b.dataset.d)));
}

async function openAcceptance(orderId, dispatchId) {
  const [o, d] = await Promise.all([api(`/api/orders/${orderId}`), api(`/api/dispatches/${dispatchId}`)]);
  const before = o.photos.filter(p => p.phase === "before" && p.kind === "clean");
  const after = d.photos.filter(p => p.phase === "after" && p.kind === "clean");
  const reset = d.photos.filter(p => p.kind === "reset");
  const done = new Set(d.steps.map(s => s.step));
  modal(`验收 · ${o.code}（${o.inlet_code}）`, `
    <div class="notice info">前后照片采用固定比例 4:3 画框并排对照，完整保留井口边缘；窄屏自动改为上下对照。</div>
    <div class="photo-pair">
      ${before[0] ? photoFrame(photoUrl(before[0].path), "清掏前") : photoFrameEmpty("清掏前（缺失）")}
      ${after[0] ? photoFrame(photoUrl(after[0].path), "清掏后") : photoFrameEmpty("清掏后（缺失）")}
    </div>
    <div class="photo-pair" style="margin-top:10px">
      ${reset[0] ? photoFrame(photoUrl(reset[0].path), "复位检查") : photoFrameEmpty("复位检查（缺失）")}
      <div class="pf-wrap"><div class="pf-cap">工序与计量</div>
        <div class="photo-frame" style="display:block;padding:10px;background:var(--panel-2)">
          ${STEP_ORDER.map(s => `<div>${done.has(s) ? "✔" : "✘"} ${STEP_LABEL[s]}</div>`).join("")}
          <hr style="border:none;border-top:1px solid var(--line)">
          <div>淤泥袋数：<b>${d.output?.bag_count ?? "—"}</b></div>
          <div>过磅净重：<b>${pct(d.output?.measured_kg)}kg</b></div>
          ${d.output?.revised_kg != null ? `<div>主管修订：<b>${pct(d.output.revised_kg)}kg</b><br><span class="muted">${esc(d.output.revise_reason)}</span></div>` : ""}
        </div></div>
    </div>
    ${o.merge_groups?.length ? `<div class="notice info">本单属于合单 ${esc(o.merge_groups[0].code)}（${o.merge_groups[0].orders.map(x => x.inlet_code).join("、")}），合单仅合并派车，本单独立验收。</div>` : ""}
    <label class="field">验收意见 <textarea id="acComment" placeholder="退回时必须填写原因">${esc("")}</textarea></label>`,
    [{ text: "关闭", cls: "ghost", onClick: closeModal },
     { text: "退回（沿用原工单）", cls: "danger", onClick: async (b) => {
       await wrap(async () => {
         await api(`/api/orders/${orderId}/acceptance/decide`, { method: "POST",
           body: { result: "reject", comment: $("#acComment").value } });
         closeModal(); toast("已退回，继续沿用原工单处理", true); await refreshAll();
       }, b);
     } },
     { text: "验收通过（恢复正常）", cls: "orange", onClick: async (b) => {
       await wrap(async () => {
         await api(`/api/orders/${orderId}/acceptance/decide`, { method: "POST",
           body: { result: "pass", comment: $("#acComment").value || "井室畅通，复位牢固" } });
         closeModal(); toast("验收通过，雨水口恢复正常状态", true); await refreshAll();
       }, b);
     } }], true);
}

// ------------------------------------------------------------- 暴雨复查提醒
async function loadReminders() {
  const list = await api("/api/reminders");
  const due = list.filter(r => new Date(r.due_at) <= new Date());
  const bar = $("#reminderBar");
  if (!list.length) { bar.classList.remove("show"); return; }
  const next = list[0];
  bar.className = "reminder-bar show";
  bar.style.background = ""; bar.style.color = "";
  bar.innerHTML = `🌧️ <b>暴雨前复查提醒 ${list.length}</b>：${esc(next.message)}（${fmt(next.due_at)}）
    <button class="small ${due.length ? "orange" : "ghost"}" id="fireReminder" style="margin-left:8px">
      ${due.length ? `触发到期提醒(${due.length})` : "检查到期"}</button>`;
  $("#fireReminder").onclick = () => wrap(async () => {
    const r = await api("/api/reminders/fire", { method: "POST" });
    toast(r.fired.length ? `已触发 ${r.fired.length} 条复查提醒` : "暂无到期提醒", true);
    await refreshAll();
  });
}

// ------------------------------------------------------------- 启动
(async function boot() {
  await loadDirectory();
  await refreshAll();
  if (!location.hash) location.hash = "#map";
  route();
})();
