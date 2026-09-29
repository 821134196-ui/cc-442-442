/* 社区雨水口清掏管理平台前端（无框架，原生 JS） */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATE = { user: 3, meta: {}, overview: null };
const ROLE_CN = { patrol: "巡查员", dispatcher: "调度员", crew: "班组", inspector: "验收员", supervisor: "主管" };
const STEP_CN = { open: "开盖", clean: "清掏", flush: "冲洗", reset: "复位" };
const STEPS = ["open", "clean", "flush", "reset"];
const STATUS_CN = {
  pending: "待派工", dispatched: "已派工", working: "作业中", submitted: "待验收",
  accepted: "已通过", rejected: "待整改", cancelled: "已取消"
};
const STATUS_TAG = {
  pending: "gray", dispatched: "", working: "orange", submitted: "orange",
  accepted: "green", rejected: "red", cancelled: "gray"
};
const LVL_CN = ["畅通", "轻微堵塞", "中度堵塞", "严重堵塞"];
const LVL_COLOR = ["#8b95a1", "#f3b26b", "#f28123", "#d9480f"];
const TYPE_CN = { normal: "常规", rush: "暴雨加急", cover_damage: "井盖破损", backflow: "积水反涌", spill: "淤泥遗撒" };

/* ---------- API ---------- */
async function api(path, body) {
  const opt = { headers: { "Content-Type": "application/json", "X-User-Id": STATE.user } };
  if (body) { opt.method = "POST"; opt.body = JSON.stringify({ ...body, user_id: STATE.user }); }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || "请求失败");
    err.data = data;
    throw err;
  }
  return data;
}

function toast(msg, kind = "info") {
  const box = $("#toast");
  const el = document.createElement("div");
  el.className = `toast-item ${kind}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function modal(title, bodyHtml, footerHtml = "") {
  closeModal();
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.id = "modalMask";
  mask.innerHTML = `<div class="modal">
    <div class="m-hd">${esc(title)}</div>
    <div class="m-bd">${bodyHtml}</div>
    <div class="m-ft">${footerHtml || `<button class="btn" onclick="closeModal()">关闭</button>`}</div>
  </div>`;
  mask.addEventListener("click", e => { if (e.target === mask) closeModal(); });
  document.body.appendChild(mask);
}
function closeModal() { $("#modalMask")?.remove(); }

function fmt(ts, withDate = true) {
  if (!ts) return "—";
  const d = new Date(ts);
  const p = n => String(n).padStart(2, "0");
  return withDate
    ? `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`
    : `${p(d.getHours())}:${p(d.getMinutes())}`;
}
function nowLocal() {
  const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}
function plusHours(h) {
  const d = new Date(Date.now() + h * 3600000);
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

/* ---------- 通用加载 ---------- */
async function loadMeta() {
  STATE.meta = await api("/api/meta");
  STATE.user = Number(localStorage.getItem("uid")) || STATE.meta.users[2]?.id || 3;
  renderRoleBox();
}
function renderRoleBox() {
  const u = STATE.meta.users.find(x => x.id === STATE.user) || STATE.meta.users[0];
  $("#roleSelect").value = u.id;
}
async function refreshMeta() {
  STATE.meta = await api("/api/meta");
  renderBell();
}
function renderBell() {
  const unread = STATE.meta.notifications.filter(n => !n.read).length;
  const dot = $("#bellDot");
  dot.textContent = unread; dot.style.display = unread ? "block" : "none";
  $("#notifPanel").innerHTML = STATE.meta.notifications.length
    ? STATE.meta.notifications.map(n =>
        `<div class="notif-item">${esc(n.message)}<div class="t">${esc(fmt(n.created_at))}</div></div>`).join("")
    : `<div class="notif-item muted">暂无通知</div>`;
}

/* ---------- 路由 ---------- */
const PAGES = ["map", "patrol", "schedule", "records", "handover", "acceptance"];
const PAGE_TITLES = { map: "片区点位图", patrol: "巡查列表", schedule: "班组日程", records: "清掏记录", handover: "车辆交接", acceptance: "验收台账" };
async function route() {
  const page = (location.hash.replace("#/", "") || "map").split("?")[0];
  $$("nav.tabs a").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  $("#pageTitle").textContent = PAGE_TITLES[page] || "";
  const view = $("#view");
  view.innerHTML = "";
  await refreshMeta();
  try {
    await ({ map: pageMap, patrol: pagePatrol, schedule: pageSchedule,
            records: pageRecords, handover: pageHandover, acceptance: pageAcceptance }[page] || pageMap)(view);
  } catch (e) {
    view.innerHTML = `<div class="card"><div class="card-bd">加载失败：${esc(e.message)}</div></div>`;
  }
}
window.addEventListener("hashchange", route);

/* ================= 点位图 ================= */
async function pageMap(view) {
  STATE.overview = await api("/api/overview");
  const ov = STATE.overview;
  const sec = ov.sectors[0];
  view.innerHTML = `
    <div class="map-layout">
      <div class="map-wrap">
        <div class="card">
          <div class="card-hd">片区点位图
            <span class="muted small">点击点位查看照片与工序</span>
            <span class="spacer"></span>
            <button class="btn warn sm" id="btnSos">⚑ 应急处理</button>
          </div>
          <div class="card-bd" style="padding:10px">
            <svg id="mapSvg" viewBox="0 0 ${sec.map_w} ${sec.map_h}" role="img" aria-label="片区点位图">
              ${ov.segments.map(s => {
                const pts = s.points.map(p => p.join(",")).join(" ");
                const mid = s.points[Math.floor(s.points.length / 2)];
                return `<polyline class="road-edge" points="${pts}"/>
                  <polyline class="road" points="${pts}"/>
                  <polyline class="road-dash" points="${pts}"/>
                  <text class="road-label" x="${mid[0] + 14}" y="${mid[1] - 22}">${esc(s.name)}</text>`;
              }).join("")}
              <g id="alertLayer"></g>
              <g id="inletLayer"></g>
            </svg>
            <div class="legend">
              <span><i style="background:#8b95a1"></i>畅通</span>
              <span><i style="background:#f3b26b"></i>轻微</span>
              <span><i style="background:#f28123"></i>中度</span>
              <span><i style="background:#d9480f"></i>严重</span>
              <span><i style="background:#fff;border-color:#d9480f"></i>作业中(白圈)</span>
            </div>
          </div>
        </div>
      </div>
      <div class="map-side" id="mapSide">
        <div class="card"><div class="card-bd muted small">← 选择地图上的雨水口编号，查看联系人、照片与清掏工序；巡查发现问题可直接建单并划定警戒范围。</div></div>
      </div>
    </div>`;
  $("#btnSos").onclick = sosModal;
  drawInlets(ov);
}

function drawInlets(ov) {
  const layer = $("#inletLayer");
  layer.innerHTML = ov.inlets.map(i => {
    const color = i.status === "cleaning" ? "#ffffff" : LVL_COLOR[i.block_level];
    const stroke = i.status === "cleaning" ? LVL_COLOR[Math.max(2, i.block_level)] : "#fff";
    const txt = i.block_level > 0 ? LVL_CN[i.block_level].replace("堵塞", "") : "畅";
    const cover = i.cover_ok ? "" : `<use href="#"/>`;
    return `<g class="inlet-g" data-id="${i.id}" transform="translate(${i.x},${i.y})">
      ${!i.cover_ok ? '<circle r="17" fill="none" stroke="#d64545" stroke-width="2" stroke-dasharray="3 3"/>' : ""}
      <circle class="inlet-dot" r="13" fill="${color}" stroke="${stroke}"/>
      <text class="inlet-badge" y="3.5">${esc(i.block_level > 0 ? i.block_level : "✓")}</text>
      <text class="inlet-code" y="30">${esc(i.code)}</text>
    </g>`;
  }).join("");
  $$(".inlet-g", layer).forEach(g => g.onclick = () => selectInlet(+g.dataset.id));
  drawAlertZones();
}

async function drawAlertZones() {
  // 作业/待派工工单的警戒范围
  const orders = await api("/api/orders");
  const scale = STATE.overview.sectors[0];
  const g = $("#alertLayer");
  if (!g) return;
  g.innerHTML = "";
  for (const o of orders) {
    if (!o.alert_radius || !["pending", "dispatched", "working", "rejected"].includes(o.status)) continue;
    for (const code of (o.inlet_codes || "").split(",")) {
      const inlet = STATE.overview.inlets.find(i => i.code === code);
      if (!inlet) continue;
      g.innerHTML += `<circle class="alert-ring" cx="${inlet.x}" cy="${inlet.y}" r="${o.alert_radius}"/>`;
    }
  }
}

async function selectInlet(id) {
  $$(".inlet-g").forEach(g => g.classList.toggle("selected", +g.dataset.id === id));
  const [detail, ov] = [await api(`/api/inlet?id=${id}`), STATE.overview];
  const activeOrder = detail.orders.find(o => ["pending", "dispatched", "working", "submitted", "rejected"].includes(o.status));
  let orderPanel = "";
  if (activeOrder) {
    const full = await api(`/api/order?id=${activeOrder.id}`);
    orderPanel = renderOrderWorkPanel(full, detail);
  }
  $("#mapSide").innerHTML = `
    <div class="card">
      <div class="card-hd">${esc(detail.code)}
        <span class="tag ${detail.block_level ? "orange" : "green"}">${esc(LVL_CN[detail.block_level])}</span>
        ${detail.cover_ok ? "" : '<span class="tag red">井盖破损</span>'}
        <span class="spacer"></span>
      </div>
      <div class="card-bd">
        <div class="small muted">状态：${esc(STATUS_CN[detail.status] || detail.status)}</div>
        <div style="margin:8px 0">
          <strong class="small">本地联系人</strong>
          ${detail.contacts.length ? detail.contacts.map(c =>
            `<div class="small">${esc(c.name)} · ${esc(c.phone)}<br><span class="muted">${esc(c.address || "")}</span></div>`).join("")
            : '<div class="small muted">暂无登记联系人</div>'}
        </div>
        ${detail.status === "normal" && !activeOrder ? `
        <button class="btn primary sm" id="btnNewOrder">巡查建单并划警戒</button>` : ""}
        ${orderPanel}
      </div>
    </div>`;
  $("#btnNewOrder")?.addEventListener("click", () => createOrderModal([id]));
  if (activeOrder) bindSidebarWork(activeOrder.id, id);
}

/* 地图侧栏展开后的现场操作（照片与工序仅点开侧栏后可操作） */
function bindSidebarWork(orderId, inletId) {
  $("#btnStart")?.addEventListener("click", async () => {
    try { await api("/api/start", { order_id: orderId }); toast("已开工", "ok"); selectInlet(inletId); }
    catch (e) { toast(e.message, "err"); }
  });
  $("#btnBag")?.addEventListener("click", async () => {
    try { await api("/api/bags", { order_id: orderId, inlet_id: inletId, bags: +$("#bagInput").value });
      toast("袋数已保存", "ok"); } catch (e) { toast(e.message, "err"); }
  });
  $("#resetChk")?.addEventListener("change", async (e) => {
    if (!e.target.checked) return;
    try { await api("/api/steps", { order_id: orderId, inlet_id: inletId, step: "reset", note: "井盖复位检查通过" });
      toast("复位检查完成", "ok"); selectInlet(inletId);
    } catch (err) { toast(err.message, "err"); e.target.checked = false; }
  });
  $("#btnStep")?.addEventListener("click", async () => {
    try {
      const order = await api(`/api/order?id=${orderId}`);
      const li = order.inlets.find(x => x.inlet_id === inletId);
      const next = STEPS.find(s => !li.steps.some(x => x.step === s));
      if (!next) return toast("四道工序已全部记录", "info");
      await api("/api/steps", { order_id: orderId, inlet_id: inletId, step: next });
      toast(`${STEP_CN[next]} 已记录`, "ok"); selectInlet(inletId);
    } catch (e) { toast(e.message, "err"); }
  });
  $("#btnPhoto")?.addEventListener("click", () => photoModal(orderId, inletId,
    STATE.overview.inlets.find(i => i.id === inletId)?.code || ""));
  $("#btnSubmit")?.addEventListener("click", async () => {
    try { await api("/api/submit", { order_id: orderId, inlet_ids: [inlet_id] });
      toast("已申请验收", "ok"); selectInlet(inletId);
    } catch (e) { toast(e.message, "err"); }
  });
}
function renderOrderWorkPanel(order, inletDetail) {
  const li = order.inlets.find(x => x.inlet_id === inletDetail.id);
  if (!li) return "";
  const before = li.photos.find(p => p.phase === "before");
  const after = li.photos.find(p => p.phase === "after");
  const canWork = ["working", "rejected"].includes(order.status);
  return `
    <div style="border-top:1px solid var(--line);margin-top:12px;padding-top:12px">
      <div class="small"><strong>工单 ${esc(order.order_no)}</strong>
        <span class="tag ${STATUS_TAG[order.status]}">${esc(STATUS_CN[order.status])}</span></div>
      <div class="compare" style="margin:10px 0">
        ${photoFrame("before", before)}
        ${photoFrame("after", after)}
      </div>
      <div>
        ${STEPS.map((s, idx) => {
          const rec = li.steps.find(x => x.step === s);
          return `<div class="step-line ${rec ? "done" : ""}">
            <div class="step-dot">${idx + 1}</div>
            <div><div>${STEP_CN[s]} ${rec ? "✓" : ""}</div>
              <div class="step-meta">${rec ? `${esc(rec.operator)} · ${fmt(rec.recorded_at)}` : "未记录"}</div>
            </div></div>`;
        }).join("")}
      </div>
      <div class="row" style="margin-top:8px">
        <label class="fld">淤泥袋数
          <input type="number" min="0" value="${li.bag_count ?? ""}" id="bagInput" style="width:90px">
        </label>
        <button class="btn sm" id="btnBag">保存袋数</button>
        <label class="small" style="flex-direction:row;align-items:center;gap:5px">
          <input type="checkbox" id="resetChk" ${li.reset_checked ? "checked" : ""}> 复位检查完成</label>
      </div>
      ${canWork ? `<div class="row" style="margin-top:10px">
        <button class="btn sm primary" id="btnStep">记录下一步工序</button>
        <button class="btn sm" id="btnPhoto">补拍照片</button>
        <button class="btn sm" id="btnSubmit">申请验收</button>
      </div>` : order.status === "dispatched" ? `
      <button class="btn sm primary" style="margin-top:10px" id="btnStart">班组到场 · 开工</button>` : ""}
      ${order.reject_reason ? `<div class="small" style="color:var(--red);margin-top:8px">退回意见：${esc(order.reject_reason)}</div>` : ""}
    </div>`;
}

function photoFrame(phase, photo) {
  if (!photo) return `<div class="frame ${phase} empty"><span class="cap">${phase === "before" ? "清掏前" : "清掏后"}</span></div>`;
  return `<div class="frame ${phase}">
    <span class="cap">${phase === "before" ? "清掏前" : "清掏后"}</span>
    <img src="/uploads/${esc(photo.file_path)}" alt="${phase}照片"></div>`;
}

/* ================= 巡查列表（建单/派工/合单） ================= */
async function pagePatrol(view) {
  const orders = await api("/api/orders");
  view.innerHTML = `
    <div class="card">
      <div class="card-hd">巡查与工单
        <span class="spacer"></span>
        <button class="btn warn sm" id="btnSos2">⚑ 应急处理</button>
        <button class="btn primary sm" id="btnCreate">+ 巡查建单</button>
      </div>
      <div class="card-bd" style="padding:0">
        <div style="padding:10px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <label class="small muted">合单（相邻点位合并派车）：勾选多张“待派工”工单
            <input type="checkbox" id="mergeMode"></label>
          <span id="mergeSel" class="small muted"></span>
          <button class="btn sm" id="btnMerge" disabled>合并派车</button>
        </div>
        <div id="orderList"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-hd">居民反馈（本地样例联系人）</div>
      <div class="card-bd" style="padding:0"><div style="overflow-x:auto"><table class="tbl">
        <thead><tr><th>反馈人</th><th>点位</th><th>内容</th><th>状态</th><th>关联工单</th><th></th></tr></thead>
        <tbody id="fbBody"></tbody></table></div></div>
    </div>`;
  $("#btnCreate").onclick = () => createOrderModal();
  $("#btnSos2").onclick = sosModal;
  renderOrderList(orders);
  renderFeedback();
  const picked = new Set();
  $("#mergeMode").onchange = e => {
    if (!e.target.checked) { picked.clear(); renderOrderList(orders); $("#mergeSel").textContent = ""; $("#btnMerge").disabled = true; }
    else renderOrderList(orders, picked);
  };
  $("#btnMerge").onclick = async () => {
    try {
      const r = await api("/api/merge", { order_ids: [...picked] });
      toast(`已合并为 ${r.order_no}（验收仍逐点进行）`, "ok");
      route();
    } catch (e) { toast(e.message, "err"); }
  };
}

function renderOrderList(orders, picked) {
  $("#orderList").innerHTML = orders.map(o => `
    <div class="order-card">
      <div class="order-hd">
        ${picked && o.status === "pending" ? `<input type="checkbox" class="mergeChk" data-id="${o.id}">` : ""}
        <span class="no">${esc(o.order_no)}</span>
        <span class="tag ${STATUS_TAG[o.status]}">${esc(STATUS_CN[o.status])}</span>
        <span class="tag ${o.type === "normal" ? "gray" : "orange"}">${esc(TYPE_CN[o.type] || o.type)}</span>
        ${o.urgent ? '<span class="tag red">加急</span>' : ""}
        <span class="small muted">${esc(o.inlet_codes || "")}</span>
        <span class="spacer"></span>
        <span class="small muted">${esc(fmt(o.created_at))} · ${esc(o.reporter || "")}</span>
      </div>
      <div class="order-bd">
        <div class="small">堵塞等级：<b style="color:${LVL_COLOR[o.block_level]}">${esc(LVL_CN[o.block_level])}</b>
          ${o.alert_radius ? ` · 警戒范围 ${o.alert_radius}m（${esc(o.alert_note || "")}）` : ""}
          ${o.crew_name ? ` · 班组：${esc(o.crew_name)}` : ""}
          ${o.vehicle_plate ? ` · 车辆：${esc(o.vehicle_plate)}` : ""}
        </div>
        <div class="small" style="margin-top:4px">${esc(o.description || "")}</div>
        ${o.merged_from ? `<div class="small muted" style="margin-top:4px">由 ${esc((o.merged_from || []).join("、"))} 合单而来（仅合派车）</div>` : ""}
        ${o.reject_reason ? `<div class="small" style="color:var(--red);margin-top:4px">退回：${esc(o.reject_reason)}（继续沿用原工单整改）</div>` : ""}
        <div style="margin-top:10px" class="row">
          ${["pending", "rejected"].includes(o.status) ? `<button class="btn sm primary" onclick="dispatchModal(${o.id})">派工/重新派工</button>` : ""}
          ${o.status === "dispatched" ? `<button class="btn sm" onclick="quickStart(${o.id})">开工</button>` : ""}
          <button class="btn sm" onclick="orderWorkModal(${o.id})">现场记录</button>
          ${["working", "rejected", "submitted"].includes(o.status) ? `<button class="btn sm" onclick="location.hash='#/acceptance'">去验收</button>` : ""}
        </div>
      </div>
    </div>`).join("");
  if (picked) {
    $$(".mergeChk").forEach(c => c.onchange = () => {
      (+c.dataset.id);
      c.checked ? picked.add(+c.dataset.id) : picked.delete(+c.dataset.id);
      $("#mergeSel").textContent = picked.size ? `已选 ${picked.size} 张` : "";
      $("#btnMerge").disabled = picked.size < 2;
    });
  }
}

async function renderFeedback() {
  const list = STATE.meta.feedback;
  $("#fbBody").innerHTML = list.map(f => `
    <tr><td>${esc(f.contact_name || "匿名")}<br><span class="muted small">${esc(f.contact_id ? "" : "")}</span></td>
      <td>${esc(f.inlet_code || "—")}</td>
      <td>${esc(f.content)}</td>
      <td><span class="tag ${f.status === "open" ? "orange" : "gray"}">${f.status === "open" ? "待处理" : "已关联"}</span></td>
      <td>${f.order_id ? "#" + f.order_id : "—"}</td>
      <td>${f.status === "open" ? `<button class="btn sm" onclick="linkFb(${f.id})">关联工单</button>` : ""}</td>
    </tr>`).join("");
}
window.linkFb = (id) => modal("关联到工单",
  `<label class="fld">工单 ID
    <input id="fbOid" type="number" placeholder="在巡查列表中查看工单编号后的 #ID"></label>`,
  `<button class="btn" onclick="closeModal()">取消</button>
   <button class="btn primary" onclick="doLinkFb(${id})">关联</button>`);
window.doLinkFb = async (id) => {
  try { await api("/api/feedback/link", { feedback_id: id, order_id: +$("#fbOid").value });
    closeModal(); toast("已关联", "ok"); route();
  } catch (e) { toast(e.message, "err"); }
};

/* ---------- 建单弹窗 ---------- */
async function ensureOverview() {
  if (!STATE.overview) STATE.overview = await api("/api/overview");
  return STATE.overview;
}
async function createOrderModal(preselect = []) {
  const ov = await ensureOverview();
  const inlets = ov.inlets;
  modal("巡查建单 · 划定警戒范围", `
    <label class="fld">雨水口（可多选，仅相邻点位建议同单；非相邻请分别建单或用合单）
      <select id="oInlets" multiple size="6">
        ${inlets.map(i => `<option value="${i.id}" ${preselect.includes(i.id) ? "selected" : ""}>
          ${esc(i.code)} · ${esc(LVL_CN[i.block_level])} · ${esc(i.status)}</option>`).join("")}
      </select></label>
    <div class="row">
      <label class="fld">堵塞等级<select id="oLevel">
        <option value="1">1 轻微</option><option value="2" selected>2 中度</option><option value="3">3 严重</option>
      </select></label>
      <label class="fld">警戒范围(米)<input id="oRadius" type="number" min="0" value="20"></label>
    </div>
    <label class="fld">警戒说明<input id="oAlertNote" value="锥桶围挡，行人绕行"></label>
    <label class="fld">问题描述<textarea id="oDesc" rows="2"></textarea></label>
  `, `<button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" id="oCreateBtn">建单</button>`);
  $("#oCreateBtn").onclick = async () => {
    const inlet_ids = [...$("#oInlets").selectedOptions].map(o => +o.value);
    try {
      const o = await api("/api/orders", {
        inlet_ids, block_level: +$("#oLevel").value,
        alert_radius: +$("#oRadius").value || 0,
        alert_note: $("#oAlertNote").value, description: $("#oDesc").value
      });
      closeModal(); toast(`工单 ${o.order_no} 已创建`, "ok"); route();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ---------- 派工弹窗 ---------- */
window.dispatchModal = (orderId) => {
  const m = STATE.meta;
  modal("派工调度", `
    <div class="row">
      <label class="fld" style="flex:1">班组<select id="dCrew">
        ${m.crews.map(c => `<option value="${c.id}">${esc(c.name)}（${esc(c.leader)} ${esc(c.phone)}）</option>`).join("")}
      </select></label>
      <label class="fld" style="flex:1">清运车辆<select id="dVehicle">
        ${m.vehicles.map(v => `<option value="${v.id}">${esc(v.plate)} · ${esc(v.driver)} · 载重${v.capacity_kg}kg</option>`).join("")}
      </select></label>
    </div>
    <div class="row">
      <label class="fld" style="flex:1">开始<input id="dStart" type="datetime-local" value="${nowLocal()}"></label>
      <label class="fld" style="flex:1">结束<input id="dEnd" type="datetime-local" value="${plusHours(2)}"></label>
    </div>
    <div class="small muted">同一班组时段重叠时系统将阻止派工，避免冲突。</div>
  `, `<button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" id="dBtn">确认派工</button>`);
  $("#dBtn").onclick = async () => {
    try {
      await api("/api/dispatch", { order_id: orderId, crew_id: +$("#dCrew").value,
        vehicle_id: +$("#dVehicle").value, planned_start: $("#dStart").value,
        planned_end: $("#dEnd").value });
      closeModal(); toast("派工成功", "ok"); route();
    } catch (e) { toast(e.message, "err"); }
  };
};

window.quickStart = async (id) => {
  try { await api("/api/start", { order_id: id }); toast("已开工", "ok"); route(); }
  catch (e) { toast(e.message, "err"); }
};

/* ---------- 现场记录弹窗（工单所有点位：工序/照片/袋数） ---------- */
window.orderWorkModal = async (orderId) => {
  const order = await api(`/api/order?id=${orderId}`);
  const canWork = ["working", "rejected"].includes(order.status);
  modal(`现场记录 · ${order.order_no}`, order.inlets.map(li => `
    <div style="border:1px solid var(--line);border-radius:10px;padding:12px" data-iid="${li.inlet_id}">
      <div style="display:flex;align-items:center;gap:8px">
        <strong>${esc(li.code)}</strong>
        <span class="tag ${STATUS_TAG[li.submit_status] || "gray"}">${esc(li.submit_status ? STATUS_CN[li.submit_status] || li.submit_status : "未申请")}</span>
      </div>
      <div class="compare" style="margin:10px 0">
        ${photoFrame("before", li.photos.find(p => p.phase === "before"))}
        ${photoFrame("after", li.photos.find(p => p.phase === "after"))}
      </div>
      <div class="small" style="display:flex;gap:12px;flex-wrap:wrap">
        ${STEPS.map(s => { const r = li.steps.find(x => x.step === s);
          return `<span class="${r ? "" : "muted"}">${STEP_CN[s]} ${r ? "✓ " + esc(fmt(r.recorded_at)) : "○"}</span>`; }).join("")}
      </div>
      ${canWork ? `<div class="row" style="margin-top:10px">
        <button class="btn sm primary" onclick="doStep(${orderId},${li.inlet_id})">记录下一步工序</button>
        <button class="btn sm" onclick="photoModal(${orderId},${li.inlet_id},'${li.code}')">拍照/补拍</button>
        <input type="number" min="0" value="${li.bag_count ?? ""}" id="bag-${li.inlet_id}" style="width:80px" placeholder="袋数">
        <button class="btn sm" onclick="doBag(${orderId},${li.inlet_id})">保存袋数</button>
        <label class="small" style="display:flex;align-items:center;gap:4px">
          <input type="checkbox" id="reset-${li.inlet_id}" ${li.reset_checked ? "checked" : ""}
            onchange="doReset(${orderId},${li.inlet_id},this.checked)">复位检查</label>
        <button class="btn sm warn" onclick="doSubmitOne(${orderId},${li.inlet_id})">申请验收</button>
      </div>` : ""}
      ${li.reject_reason ? `<div class="small" style="color:var(--red);margin-top:6px">退回：${esc(li.reject_reason)}</div>` : ""}
    </div>`).join("")
    + (order.status === "dispatched"
      ? `<button class="btn primary" onclick="quickStart(${orderId});closeModal()">班组到场 · 开工</button>` : ""),
    `<button class="btn" onclick="closeModal()">关闭</button>`);
};

window.doStep = async (oid, iid) => {
  const order = await api(`/api/order?id=${oid}`);
  const li = order.inlets.find(x => x.inlet_id === iid);
  const done = new Set(li.steps.map(s => s.step));
  const next = STEPS.find(s => !done.has(s));
  if (!next) return toast("四道工序已全部记录", "info");
  try {
    await api("/api/steps", { order_id: oid, inlet_id: iid, step: next });
    toast(`${STEP_CN[next]} 已记录`, "ok"); orderWorkModal(oid);
  } catch (e) { toast(e.message, "err"); }
};
window.doBag = async (oid, iid) => {
  try {
    await api("/api/bags", { order_id: oid, inlet_id: iid, bags: +$(`#bag-${iid}`).value });
    toast("袋数已保存", "ok");
  } catch (e) { toast(e.message, "err"); }
};
window.doReset = async (oid, iid, checked) => {
  if (!checked) return;
  try { await api("/api/steps", { order_id: oid, inlet_id: iid, step: "reset", note: "井盖复位检查通过" });
    toast("复位检查完成", "ok");
  } catch (e) { toast(e.message, "err"); orderWorkModal(oid); }
};
window.doSubmitOne = async (oid, iid) => {
  try {
    const r = await api("/api/submit", { order_id: oid, inlet_ids: [iid] });
    toast("已申请验收", "ok"); orderWorkModal(oid);
    if (r.order_status === "submitted") location.hash = "#/acceptance";
  } catch (e) { toast(e.message, "err"); }
};

/* 现场拍照：本地环境无相机，生成带点位与时间的 SVG 样片落到 uploads */
window.photoModal = (oid, iid, code) => {
  modal(`现场照片 · ${code}`, `
    <div class="row">
      <label class="fld" style="flex:1">照片阶段<select id="phPhase">
        <option value="before">清掏前</option><option value="after">清掏后</option></select></label>
    </div>
    <div class="small muted">本地样例：点击“拍照”生成带编号/时间戳的样例照片并落到本地 uploads 目录。</div>
  `, `<button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" id="phBtn">📷 拍照</button>`);
  $("#phBtn").onclick = async () => {
    const phase = $("#phPhase").value;
    const svg = samplePhotoSvg(code, phase);
    try {
      await api("/api/photos", { order_id: oid, inlet_id: iid, phase,
        filename: `shot_${phase}.svg`, content: svg });
      closeModal(); toast("照片已保存", "ok"); orderWorkModal(oid);
    } catch (e) { toast(e.message, "err"); }
  };
};

function samplePhotoSvg(code, phase) {
  const after = phase === "after";
  const t = fmt(new Date().toISOString());
  const tag = after ? ["#1f7ae0", "清掏后", "#7cc3f2", "#3d8fd1"] : ["#e8833a", "清掏前", "#5d4a2f", "#4a3a24"];
  const blobs = after ? "" : [300, 360, 450, 500].map((x, k) =>
    `<ellipse cx="${x}" cy="${400 + (k % 2) * 30}" rx="${20 + k * 3}" ry="14" fill="#2f2618"/>`).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 800 600">
  <rect width="800" height="600" fill="#d9dde1"/><rect width="800" height="150" fill="#c7ccd2"/>
  <text x="24" y="48" font-family="sans-serif" font-size="26" fill="#5a6470">${esc(code)}</text>
  <text x="24" y="86" font-family="sans-serif" font-size="18" fill="#7a848f">${esc(t)}</text>
  <rect x="650" y="24" width="126" height="44" rx="6" fill="${tag[0]}"/>
  <text x="713" y="54" font-family="sans-serif" font-size="24" fill="#fff" text-anchor="middle">${tag[1]}</text>
  <ellipse cx="400" cy="430" rx="300" ry="140" fill="#8f969f"/>
  <ellipse cx="400" cy="420" rx="246" ry="104" fill="#6b727b"/>
  <ellipse cx="400" cy="418" rx="208" ry="80" fill="${tag[2]}"/>
  <ellipse cx="400" cy="412" rx="208" ry="60" fill="${tag[3]}"/>${blobs}
  <text x="400" y="568" font-family="sans-serif" font-size="26" fill="#3a4149" text-anchor="middle">现场拍照样片</text></svg>`;
}

/* ================= 应急六入口 ================= */
function sosModal() {
  const items = [
    ["storm", "暴雨前加急", "填写降雨时间，自动建加急单与暴雨前复查提醒"],
    ["cover", "井盖破损", "标记破损、自动建处置单并扩大警戒"],
    ["backflow", "积水反涌", "严重堵塞加急处置，禁止靠近"],
    ["spill", "淤泥遗撒", "登记遗撒，主管确认处置"],
    ["noshow", "车辆未到", "车次标记未到，调度重新派车"],
    ["photo", "照片缺失", "登记缺失，补拍后自动关闭"]
  ];
  modal("应急 / 特殊处理入口", `
    <div class="sos-grid">${items.map(([k, t, d]) =>
      `<div class="sos-card" onclick="sosForm('${k}')"><div class="ico">⚑</div><h4>${t}</h4><p class="small">${d}</p></div>`).join("")}
    </div>`, `<button class="btn" onclick="closeModal()">关闭</button>`);
}

window.sosForm = async (kind) => {
  const inlets = (await ensureOverview()).inlets;
  const inletSel = `<label class="fld">雨水口<select id="sInlet">
    ${inlets.map(i => `<option value="${i.id}">${esc(i.code)}</option>`).join("")}</select></label>`;
  const desc = `<label class="fld">情况描述<textarea id="sDesc" rows="2"></textarea></label>`;
  let body = "", submit = "";
  if (kind === "storm") {
    body = `<label class="fld">雨水口（可多选）<select id="sInlets" multiple size="5">
      ${inlets.map(i => `<option value="${i.id}">${esc(i.code)}</option>`).join("")}</select></label>
      <label class="fld">预计降雨时间<input id="sStorm" type="datetime-local" value="${plusHours(6)}"></label>${desc}`;
    submit = `sosSubmit('storm')`;
  } else if (kind === "cover") { body = inletSel + desc; submit = `sosSubmit('cover')`; }
  else if (kind === "backflow") { body = inletSel + desc; submit = `sosSubmit('backflow')`; }
  else if (kind === "spill") {
    body = `<label class="fld">工单 ID<input id="sOid" type="number"></label>${desc}`; submit = `sosSubmit('spill')`;
  } else if (kind === "noshow") {
    body = `<label class="fld">车次 ID（车辆交接页查看 #ID）<input id="sTrip" type="number"></label>${desc}`;
    submit = `sosSubmit('noshow')`;
  } else {
    body = `<label class="fld">工单 ID<input id="sOid" type="number"></label>` + inletSel
      + `<label class="fld">说明<input id="sDesc" value="现场照片缺失，要求班组补拍"></label>`;
    submit = `sosSubmit('photo')`;
  }
  modal("应急处理", body,
    `<button class="btn" onclick="sosModal()">返回</button>
     <button class="btn warn" id="sBtn" onclick="${submit}">提交</button>`);
};

window.sosSubmit = async (kind) => {
  try {
    let r;
    if (kind === "storm") {
      const ids = [...$("#sInlets").selectedOptions].map(o => +o.value);
      r = await api("/api/rush", { inlet_ids: ids, storm_at: $("#sStorm").value, description: $("#sDesc").value });
    } else if (kind === "cover") r = await api("/api/cover-damage", { inlet_id: +$("#sInlet").value, description: $("#sDesc").value });
    else if (kind === "backflow") r = await api("/api/backflow", { inlet_id: +$("#sInlet").value, description: $("#sDesc").value });
    else if (kind === "spill") r = await api("/api/spill", { order_id: +$("#sOid").value, description: $("#sDesc").value });
    else if (kind === "noshow") {
      r = await api("/api/vehicle-noshow", { trip_id: +$("#sTrip").value, description: $("#sDesc").value || "车辆未到场" });
      closeModal();
      modal("重新派车", `<label class="fld">改派车辆<select id="rVehicle">
        ${STATE.meta.vehicles.map(v => `<option value="${v.id}">${esc(v.plate)} · ${esc(v.driver)}</option>`).join("")}
      </select></label>`,
        `<button class="btn" onclick="closeModal()">暂不</button>
         <button class="btn primary" onclick="doReassign(${r.order_id})">确认改派</button>`);
      return;
    } else r = await api("/api/photo-missing", { order_id: +$("#sOid").value, inlet_id: +$("#sInlet").value, description: $("#sDesc").value });
    closeModal(); toast("已受理并留痕", "ok"); route();
  } catch (e) { toast(e.message, "err"); }
};
window.doReassign = async (oid) => {
  try { await api("/api/reassign-vehicle", { order_id: oid, vehicle_id: +$("#rVehicle").value });
    closeModal(); toast("已重新派车", "ok"); route();
  } catch (e) { toast(e.message, "err"); }
};

/* ================= 班组日程 ================= */
async function pageSchedule(view) {
  const items = await api("/api/schedule");
  const crews = STATE.meta.crews;
  view.innerHTML = `
    <div class="sched-grid">
      ${crews.map(c => `<div class="crew-col card">
        <div class="card-bd"><h3 style="background:${c.color}">${esc(c.name)}
          <span class="small" style="font-weight:400;opacity:.9">${esc(c.leader)} ${esc(c.phone)}</span></h3>
          <div id="crew-${c.id}"></div></div></div>`).join("")}
    </div>`;
  for (const c of crews) {
    const list = items.filter(i => i.crew_id === c.id);
    $(`#crew-${c.id}`).innerHTML = list.length ? list.map(i => `
      <div class="sched-item" style="border-left-color:${c.color}">
        <div class="time">${fmt(i.planned_start)} ~ ${fmt(i.planned_end, false)}</div>
        <div style="margin:5px 0"><strong>${esc(i.order_no)}</strong>
          <span class="tag ${STATUS_TAG[i.status]}">${esc(STATUS_CN[i.status])}</span>
          ${i.urgent ? '<span class="tag red">加急</span>' : ""}</div>
        <div class="small muted">${esc(i.inlet_codes || "")}</div>
        <div class="small" style="margin-top:4px">${esc(i.description || "")}</div>
      </div>`).join("") : `<div class="small muted" style="padding:10px">暂无排程</div>`;
  }
}

/* ================= 清掏记录 ================= */
async function pageRecords(view) {
  const rows = await api("/api/records");
  view.innerHTML = `<div class="card"><div class="card-hd">清掏记录与验收台账</div>
    <div class="card-bd" style="padding:0"><div style="overflow-x:auto"><table class="tbl">
      <thead><tr><th>时间</th><th>工单号</th><th>雨水口</th><th>袋数</th><th>验收</th><th>验收人</th><th>意见</th></tr></thead>
      <tbody>${rows.map(r => `<tr>
        <td>${fmt(r.created_at)}</td><td>${esc(r.order_no)}</td><td>${esc(r.inlet_code)}</td>
        <td>${r.bag_count ?? "—"}</td>
        <td><span class="tag ${r.result === "passed" ? "green" : "red"}">${r.result === "passed" ? "通过" : "退回"}</span></td>
        <td>${esc(r.inspector)}</td><td class="small">${esc(r.acceptance_comment || "")}</td></tr>`).join("")}
      </tbody></table></div></div></div>`;
}

/* ================= 车辆交接 ================= */
async function pageHandover(view) {
  const trips = await api("/api/handovers");
  const TRIP_CN = { dispatched: "已派车", arrived: "已到场", weighed: "已过磅", completed: "已完成", noshow: "车辆未到" };
  const TRIP_TAG = { dispatched: "gray", arrived: "", weighed: "orange", completed: "green", noshow: "red" };
  view.innerHTML = `<div class="card"><div class="card-hd">车辆交接与过磅
    <span class="small muted">过磅重量由模拟地磅适配器返回；修订重量需主管写明原因</span></div>
    <div class="card-bd" style="padding:0"><div style="overflow-x:auto"><table class="tbl">
      <thead><tr><th>#</th><th>工单</th><th>点位</th><th>车牌</th><th>状态</th><th>袋数</th>
        <th>自报(kg)</th><th>过磅(kg)</th><th>修订(kg)</th><th>交接</th><th>操作</th></tr></thead>
      <tbody>${trips.map(t => `<tr>
        <td>${t.id}</td><td>${esc(t.order_no)}</td><td class="small">${esc(t.inlet_codes || "")}</td>
        <td>${esc(t.plate || "—")}</td>
        <td><span class="tag ${TRIP_TAG[t.status]}">${TRIP_CN[t.status]}</span></td>
        <td>${t.bag_count ?? "—"}</td>
        <td>${t.declared_weight_kg ?? "—"}</td>
        <td>${t.measured_weight_kg ?? "—"}${t.measured_weight_kg != null && t.declared_weight_kg != null
          ? (Math.abs(t.measured_weight_kg - t.declared_weight_kg) / t.declared_weight_kg > 0.02
            ? ' <span class="tag orange small">偏差大</span>' : "") : ""}</td>
        <td>${t.revised_weight_kg ?? "—"}${t.revision_reason
          ? `<div class="small muted">${esc(t.revised_by)}：${esc(t.revision_reason)}</div>` : ""}</td>
        <td class="small">${esc(t.handover_crew || "")}${t.handover_driver ? "<br>" + esc(t.handover_driver) : ""}</td>
        <td>${tripActions(t)}</td></tr>`).join("")}</tbody></table></div></div></div>

  <div class="card"><div class="card-hd">车辆未到 / 其他应急</div>
    <div class="card-bd"><button class="btn warn sm" onclick="sosModal()">⚑ 打开应急处理入口</button></div></div>`;
}

function tripActions(t) {
  const btns = [];
  if (["dispatched", "noshow"].includes(t.status))
    btns.push(`<button class="btn sm" onclick="tripAct(${t.id},'arrive')">到场签到</button>`);
  if (t.status === "arrived")
    btns.push(`<button class="btn sm primary" onclick="handoverModal(${t.id})">交接过磅</button>`);
  if (t.status === "weighed")
    btns.push(`<button class="btn sm" onclick="tripAct(${t.id},'complete')">完成清运</button>`);
  if (t.measured_weight_kg != null)
    btns.push(`<button class="btn sm danger" onclick="reviseModal(${t.id})">修订重量</button>`);
  if (t.status === "dispatched")
    btns.push(`<button class="btn sm warn" onclick="sosNoshowQuick(${t.id})">车辆未到</button>`);
  return btns.join(" ");
}
window.sosNoshowQuick = async (tid) => {
  try { const r = await api("/api/vehicle-noshow", { trip_id: tid });
    toast("已登记车辆未到", "ok"); route();
  } catch (e) { toast(e.message, "err"); }
};
window.tripAct = async (id, act) => {
  try {
    if (act === "arrive") await api("/api/trips/arrive", { trip_id: id });
    if (act === "complete") await api("/api/trips/complete", { trip_id: id });
    toast("操作成功", "ok"); route();
  } catch (e) { toast(e.message, "err"); }
};
window.handoverModal = (id) => modal("淤泥交接 · 模拟过磅", `
  <div class="row">
    <label class="fld" style="flex:1">淤泥袋数<input id="hBags" type="number" min="0" value="6"></label>
    <label class="fld" style="flex:1">自报重量(kg)<input id="hWeight" type="number" min="0" value="180"></label>
  </div>
  <label class="fld">交接班组人员<input id="hCrew" value="现场班组长"></label>
  <div class="small muted">提交后由模拟地磅适配器返回过磅重量。</div>
`, `<button class="btn" onclick="closeModal()">取消</button>
   <button class="btn primary" onclick="doHandover(${id})">交接并过磅</button>`);
window.doHandover = async (id) => {
  try { const r = await api("/api/trips/handover", { trip_id: id, bag_count: +$("#hBags").value,
    declared_weight_kg: +$("#hWeight").value, handover_crew: $("#hCrew").value });
    closeModal(); toast(`过磅 ${r.measured_weight_kg}kg（票据 ${r.ticket}）`, "ok"); route();
  } catch (e) { toast(e.message, "err"); }
};
window.reviseModal = (id) => modal("修订清运重量（仅主管）", `
  <label class="fld">修订后重量(kg)<input id="rWeight" type="number" min="0"></label>
  <label class="fld">修订原因（必填）<textarea id="rReason" rows="3" placeholder="如：过磅时含桶重，扣减 2 个淤泥桶自重"></textarea></label>
`, `<button class="btn" onclick="closeModal()">取消</button>
   <button class="btn danger" onclick="doRevise(${id})">提交修订</button>`);
window.doRevise = async (id) => {
  try { await api("/api/trips/revise", { trip_id: id, revised_weight_kg: +$("#rWeight").value,
    reason: $("#rReason").value });
    closeModal(); toast("修订已记录", "ok"); route();
  } catch (e) { toast(e.message, "err"); }
};

/* ================= 验收台账 ================= */
async function pageAcceptance(view) {
  const orders = await api("/api/orders");
  const reviewable = orders.filter(o =>
    ["submitted", "working", "rejected"].includes(o.status));
  view.innerHTML = `
    <div class="card"><div class="card-hd">验收台账
      <span class="small muted">合单逐点独立验收；前后照片并排固定比例对照，窄屏自动上下对照</span></div>
      <div class="card-bd" style="padding:0"><div style="overflow-x:auto"><table class="tbl">
        <thead><tr><th>时间</th><th>工单</th><th>点位</th><th>结果</th><th>验收人</th><th>意见</th></tr></thead>
        <tbody id="accBody"></tbody></table></div></div></div>
    <div id="reviewList"></div>`;
  const records = await api("/api/records");
  $("#accBody").innerHTML = records.map(r => `<tr>
    <td>${fmt(r.created_at)}</td><td>${esc(r.order_no)}</td><td>${esc(r.inlet_code)}</td>
    <td><span class="tag ${r.result === "passed" ? "green" : "red"}">${r.result === "passed" ? "通过" : "退回"}</span></td>
    <td>${esc(r.inspector)}</td><td class="small">${esc(r.acceptance_comment || "")}</td></tr>`).join("");

  const full = await Promise.all(reviewable.map(o => api(`/api/order?id=${o.id}`)));
  $("#reviewList").innerHTML = full.length ? full.map(order => order.inlets.map(li => {
    const before = li.photos.find(p => p.phase === "before");
    const after = li.photos.find(p => p.phase === "after");
    const canReview = li.submit_status === "submitted";
    return `<div class="card">
      <div class="card-hd">${esc(order.order_no)} · ${esc(li.code)}
        <span class="tag ${STATUS_TAG[order.status]}">${esc(STATUS_CN[order.status])}</span>
        ${li.submit_status === "accepted" ? '<span class="tag green">该点已通过</span>' : ""}
        ${li.submit_status === "rejected" ? '<span class="tag red">该点已退回</span>' : ""}
        ${!li.submit_status ? '<span class="tag gray">未申请验收</span>' : ""}
      </div>
      <div class="card-bd">
        <div class="compare">${photoFrame("before", before)}${photoFrame("after", after)}</div>
        <div class="row" style="margin-top:12px">
          <span class="small">袋数：<b>${li.bag_count ?? "—"}</b></span>
          <span class="small">复位：<b>${li.reset_checked ? "已检查" : "未检查"}</b></span>
          <span class="small">工序：${STEPS.map(s => {
            const r = li.steps.find(x => x.step === s);
            return `<span class="${r ? "" : "muted"}">${STEP_CN[s]}${r ? "✓" : "○"}</span>`;
          }).join(" / ")}</span>
        </div>
        ${canReview ? `<div class="row" style="margin-top:12px">
          <input id="cmt-${li.inlet_id}" placeholder="退回时必填整改意见" style="flex:1;min-width:200px">
          <button class="btn danger" onclick="doReview(${order.id},${li.inlet_id},false)">退回（沿用原工单）</button>
          <button class="btn primary" onclick="doReview(${order.id},${li.inlet_id},true)">验收通过</button>
        </div>` : `<div class="small muted" style="margin-top:10px">${li.submit_status === "submitted" ? "" : "班组尚未就该点位申请验收"}</div>`}
      </div></div>`;
  }).join("")).join("") : `<div class="card"><div class="card-bd muted">当前没有待验收点位</div></div>`;
}

window.doReview = async (oid, iid, passed) => {
  const comment = $(`#cmt-${iid}`)?.value || "";
  try {
    const r = await api("/api/review", { order_id: oid, inlet_id: iid, passed, comment });
    toast(passed ? (r.remaining ? `该点通过，剩余 ${r.remaining} 个点` : "全部点位验收通过") : "已退回，沿用原工单整改", "ok");
    route();
  } catch (e) { toast(e.message, "err"); }
};

/* ================= 启动 ================= */
$("#roleSelect").addEventListener("change", e => {
  STATE.user = +e.target.value;
  localStorage.setItem("uid", STATE.user);
});
$("#bell").addEventListener("click", () => $("#notifPanel").classList.toggle("open"));

(async function init() {
  await loadMeta();
  $("#roleSelect").innerHTML = STATE.meta.users.map(u =>
    `<option value="${u.id}">${esc(u.name)}（${ROLE_CN[u.role]}）</option>`).join("");
  renderRoleBox();
  if (!location.hash) location.hash = "#/map";
  route();
})();
