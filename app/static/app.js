/* Webhook 故障演练与回放台 — 原生 JS 单页前端 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  scenarios: [],
  scenarioId: null,
  scenario: null,       // editing form bound to this
  isNewScenario: false,
  deliveries: [],
  deliveryId: null,
  detail: null,
  compareA: null,
  compareB: "",
  compareClosed: false,
  inboxOpen: false,
  fork: null,           // {branchId, afterAttempt}
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

const short = (s, n = 10) => (s && s.length > n ? s.slice(0, n) + "…" : s ?? "");
const fmtT = (vtime, origin) =>
  vtime == null ? "—" : `T+${((vtime - (origin ?? 0)) / 1000).toFixed(2)}s`;

// ===========================================================================
// scenarios
// ===========================================================================

async function loadScenarios(selectId) {
  state.scenarios = await api("GET", "/api/scenarios");
  renderScenarioList();
  if (selectId) state.scenarioId = selectId;
  if (!state.scenarioId && state.scenarios[0]) state.scenarioId = state.scenarios[0].id;
  if (state.scenarioId) await selectScenario(state.scenarioId);
  else {
    state.scenario = null;
    $("#scenario-editor").classList.add("hidden");
  }
}

function renderScenarioList() {
  const el = $("#scenario-list");
  if (!state.scenarios.length) {
    el.innerHTML = `<p class="muted">还没有场景，点击「新建场景」。</p>`;
    return;
  }
  el.innerHTML = state.scenarios.map((s) => `
    <div class="list-item ${s.id === state.scenarioId ? "active" : ""}" data-id="${s.id}">
      <div class="title">${esc(s.name)}</div>
      <div class="sub">${esc(s.target_url)}</div>
      <div class="sub">${s.rules.length} 条规则 · 最多 ${s.max_attempts} 次 · ${s.delivery_count} 次投递</div>
    </div>`).join("");
  $$(".list-item", el).forEach((node) =>
    node.addEventListener("click", () => selectScenario(node.dataset.id)));
}

async function selectScenario(id) {
  state.scenarioId = id;
  state.scenario = await api("GET", `/api/scenarios/${id}`);
  state.isNewScenario = false;
  renderScenarioList();
  fillScenarioForm(state.scenario);
  $("#scenario-editor").classList.remove("hidden");
  await loadDeliveries();
}

function fillScenarioForm(s) {
  $("#se-title").textContent = s.id ? `场景 · ${s.name}` : "新建场景";
  $("#f-name").value = s.name || "";
  $("#f-url").value = s.target_url || "";
  $("#f-secret").value = s.secret || "";
  $("#f-body").value = JSON.stringify(s.body ?? {}, null, 2);
  const b = s.backoff || {};
  $("#f-bo-kind").value = b.kind || "exponential";
  $("#f-bo-base").value = b.base_ms ?? 1000;
  $("#f-bo-factor").value = b.factor ?? 2;
  $("#f-bo-cap").value = b.max_delay_ms ?? 60000;
  $("#f-bo-jitter").value = b.jitter || "none";
  $("#f-max").value = s.max_attempts ?? 5;
  renderRulesTable($("#f-rules-body"), s.rules || [], {});
}

function ruleRow(r = {}) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><input class="num" type="number" min="0" value="${r.attempt_no ?? ""}"></td>
    <td><select class="act">
      <option value="status" ${r.action === "status" ? "selected" : ""}>返回状态码</option>
      <option value="timeout" ${r.action === "timeout" ? "selected" : ""}>超时</option>
      <option value="disconnect" ${r.action === "disconnect" ? "selected" : ""}>断连</option>
    </select></td>
    <td><input class="code" type="number" min="100" max="599" value="${r.status_code ?? 200}"></td>
    <td><input class="det" value="${esc(r.detail || "")}"></td>
    <td class="x"><button type="button" class="btn btn-sm danger">×</button></td>`;
  $(".x button", tr).addEventListener("click", () => tr.remove());
  return tr;
}

function renderRulesTable(tbody, rules) {
  tbody.innerHTML = "";
  rules.forEach((r) => tbody.appendChild(ruleRow(r)));
}

function collectRules(tbody) {
  return $$("tr", tbody).map((tr) => ({
    attempt_no: parseInt($(".num", tr).value, 10),
    action: $(".act", tr).value,
    status_code: parseInt($(".code", tr).value, 10) || 200,
    detail: $(".det", tr).value,
  })).filter((r) => Number.isFinite(r.attempt_no));
}

function collectScenarioForm() {
  let body;
  try {
    body = JSON.parse($("#f-body").value || "{}");
  } catch (e) {
    throw new Error("请求体不是合法 JSON：" + e.message);
  }
  return {
    name: $("#f-name").value.trim(),
    target_url: $("#f-url").value.trim(),
    secret: $("#f-secret").value,
    body,
    backoff: {
      kind: $("#f-bo-kind").value,
      base_ms: parseInt($("#f-bo-base").value, 10) || 0,
      factor: parseFloat($("#f-bo-factor").value) || 1,
      max_delay_ms: parseInt($("#f-bo-cap").value, 10) || 0,
      jitter: $("#f-bo-jitter").value,
    },
    rules: collectRules($("#f-rules-body")),
    max_attempts: parseInt($("#f-max").value, 10) || 1,
  };
}

$("#btn-new-scenario").addEventListener("click", () => {
  state.isNewScenario = true;
  state.scenario = { name: "", target_url: "", secret: "", body: {},
    backoff: { kind: "exponential", base_ms: 1000, factor: 2, max_delay_ms: 60000, jitter: "none" },
    rules: [], max_attempts: 5 };
  fillScenarioForm(state.scenario);
  $("#se-title").textContent = "新建场景";
  $("#scenario-editor").classList.remove("hidden");
  $("#f-name").focus();
});

$("#btn-add-rule").addEventListener("click", () => {
  const n = $$("#f-rules-body tr").length + 1;
  $("#f-rules-body").appendChild(ruleRow({ attempt_no: n, action: "status", status_code: 500 }));
});

$("#btn-save-scenario").addEventListener("click", async () => {
  let payload;
  try { payload = collectScenarioForm(); } catch (e) { return alert(e.message); }
  if (!payload.name || !payload.target_url) return alert("名称和目标 URL 必填");
  try {
    if (state.isNewScenario) {
      const { id } = await api("POST", "/api/scenarios", payload);
      await loadScenarios(id);
    } else {
      await api("PUT", `/api/scenarios/${state.scenarioId}`, payload);
      await loadScenarios(state.scenarioId);
    }
  } catch (e) { alert("保存失败：" + e.message); }
});

$("#btn-delete-scenario").addEventListener("click", async () => {
  if (state.isNewScenario) return;
  if (!confirm("删除该场景及其全部投递？")) return;
  await api("DELETE", `/api/scenarios/${state.scenarioId}`);
  state.scenarioId = null;
  state.deliveryId = null;
  state.detail = null;
  await loadScenarios();
});

// ===========================================================================
// deliveries
// ===========================================================================

async function loadDeliveries(preserve = true) {
  if (!state.scenarioId) return;
  state.deliveries = await api("GET", `/api/deliveries?scenario_id=${state.scenarioId}`);
  renderDeliveryList();
  if (state.deliveryId && preserve) {
    if (state.deliveries.some((d) => d.id === state.deliveryId)) {
      await loadDetail(state.deliveryId);
    } else {
      state.deliveryId = null;
      state.detail = null;
      renderDetail();
    }
  }
}

function rootState(d) {
  const root = d.branches.find((b) => b.parent_branch_id === null) || d.branches[0];
  return root ? root.final_state || "running" : "?";
}

function renderDeliveryList() {
  const el = $("#delivery-list");
  if (!state.deliveries.length) {
    el.innerHTML = `<p class="muted">暂无投递。点击右上角「发起投递」开始一次演练。</p>`;
    return;
  }
  el.innerHTML = state.deliveries.map((d) => {
    const st = rootState(d);
    const badge = st === "delivered" ? `<span class="badge delivered">已送达</span>`
      : st === "exhausted" ? `<span class="badge exhausted">已放弃</span>`
      : st === "failed" ? `<span class="badge exhausted">永久失败</span>`
      : `<span class="badge running">进行中</span>`;
    return `<div class="delivery-item ${d.id === state.deliveryId ? "active" : ""}" data-id="${d.id}">
      <div class="top">
        <span><b>${short(d.id, 8)}</b>
          ${d.branches.length > 1 ? `<span class="badge fork">${d.branches.length} 条分支</span>` : ""}
        </span>
        ${badge}
      </div>
      <div class="muted" style="font-size:11px;margin-top:3px">
        ${new Date(d.created_at * 1000).toLocaleString()}</div>
    </div>`;
  }).join("");
  $$(".delivery-item", el).forEach((n) =>
    n.addEventListener("click", () => openDelivery(n.dataset.id)));
}

async function createDelivery(paused) {
  if (!state.scenarioId) return;
  const { id } = await api("POST", "/api/deliveries",
    { scenario_id: state.scenarioId, paused, speed: 10 });
  state.deliveryId = id;
  state.compareA = null;
  state.compareB = "";
  state.compareClosed = false;
  await loadDeliveries();
  await openDelivery(id);
}
$("#btn-new-delivery").addEventListener("click", () => createDelivery(false));
$("#btn-new-delivery-paused").addEventListener("click", () => createDelivery(true));

async function openDelivery(id) {
  state.deliveryId = id;
  renderDeliveryList();
  await loadDetail(id);
}

async function loadDetail(id) {
  state.detail = await api("GET", `/api/deliveries/${id}`);
  const ids = state.detail.branches.map((b) => b.id);
  if (!state.compareA || !ids.includes(state.compareA)) {
    state.compareA = ids.find((x) =>
      state.detail.branches.find((b) => b.id === x).parent_branch_id === null);
  }
  if (state.compareB && !ids.includes(state.compareB)) state.compareB = "";
  if (!state.compareB && !state.compareClosed && ids.length > 1) {
    state.compareB = ids.find((x) => x !== state.compareA);
  }
  renderDetail();
}

// ===========================================================================
// detail + compare
// ===========================================================================

function clientVtime(b) {
  if (b.paused) return b.base_vtime;
  return Math.floor(b.base_vtime + (Date.now() / 1000 - b.base_wall) * 1000 * b.speed);
}

function branchBadge(b) {
  if (b.final_state === "delivered") return `<span class="badge delivered">已送达</span>`;
  if (b.final_state === "exhausted") return `<span class="badge exhausted">已放弃</span>`;
  if (b.final_state === "failed") return `<span class="badge exhausted">永久失败</span>`;
  return `<span class="badge running">${b.paused ? "已暂停" : "投递中"}</span>`;
}

function timelineHTML(b) {
  const origin = b.origin_vtime;
  const items = b.events.map((e) => {
    const p = e.payload || {};
    const cls = ["timeline"];
    let html = "";
    const t = fmtT(p.vtime, origin);
    const inh = p.inherited ? `<span class="tag">继承</span>` : "";
    if (p.type === "scheduled") {
      cls.push("scheduled");
      html = `<div><span class="tl-vtime">计划 ${t}</span> 第 ${p.attempt_no} 次尝试
        ${p.delay_ms != null ? `<span class="tl-detail">（退避 ${p.delay_ms}ms）</span>` : ""}
        ${p.forked_from ? `<span class="tag">分叉调度</span>` : ""}${inh}</div>`;
    } else if (p.type === "attempt") {
      cls.push("attempt", `out-${p.outcome}`);
      if (p.inherited) cls.push("inherited");
      const ocn = p.outcome === "delivered" ? "✅ 送达"
        : p.outcome === "failed" ? "⛔ 不可重试失败" : "🔁 将重试";
      html = `<div><span class="tl-vtime">${t}</span> 第 ${p.attempt_no} 次尝试 ${ocn}${inh}
          <div class="tl-detail">${actionText(p)} ${esc(p.detail || "")}
            ${p.duplicate ? `<span class="tag diff-add">幂等命中·未重复投递</span>` : ""}</div>
          <div class="tl-key" title="signature: ${esc(p.signature)}">
            Idempotency-Key: ${esc(p.idempotency_key)}</div>
        </div>`;
    } else if (p.type === "terminal") {
      cls.push("terminal");
      const text = p.state === "delivered" ? "🏁 最终归宿：已送达"
        : p.state === "failed" ? "🏁 最终归宿：永久失败（不可重试）"
        : "🏁 最终归宿：已放弃（次数耗尽）";
      html = `<div><b>${text}</b>
        <span class="tl-vtime">${t}</span></div>`;
    } else if (p.type === "forked") {
      cls.push("forked");
      html = `<div>⑂ 在第 ${p.after_attempt} 次尝试后分叉 → <span class="tl-detail">${short(p.child_branch_id, 10)}</span></div>`;
    }
    return `<li class="${cls.join(" ")}" data-eid="${e.id}" data-attempt="${p.attempt_no || ""}">${html}</li>`;
  }).join("");
  return `<ul class="timeline">${items}</ul>`;
}

function actionText(p) {
  if (p.action === "timeout") return "⏱ 超时";
  if (p.action === "disconnect") return "🔌 断连";
  return `HTTP ${p.status_code}`;
}

function branchControlsHTML(b) {
  const speeds = [1, 10, 60, 600];
  const opts = speeds.map((s) =>
    `<option value="${s}" ${Math.abs(b.speed - s) < 0.001 ? "selected" : ""}>${s}×</option>`).join("");
  const running = !b.final_state;
  return `
    <div class="row-gap" style="flex-wrap:wrap">
      ${running
        ? (b.paused
            ? `<button class="btn btn-sm" data-act="resume">▶ 继续</button>`
            : `<button class="btn btn-sm" data-act="pause">⏸ 暂停</button>`)
        : ""}
      ${running ? `<select data-act="speed-sel" class="btn-sm">${opts}</select>` : ""}
      ${running && b.paused ? `
        <button class="btn btn-sm" data-act="jump" data-ms="1000">+1s</button>
        <button class="btn btn-sm" data-act="jump" data-ms="10000">+10s</button>
        <button class="btn btn-sm" data-act="jump" data-ms="60000">+60s</button>` : ""}
      <button class="btn btn-sm" data-act="fork">⑂ 在此分叉…</button>
    </div>`;
}

function summarize(b) {
  const att = b.attempts.filter((a) => !a.is_inherited);
  const codes = [...new Set(att.map((a) => a.status_code).filter((x) => x != null))];
  const last = b.attempts[b.attempts.length - 1];
  const duration = last ? last.started_at_vtime - b.origin_vtime : 0;
  return { attempts: att.length, total: b.attempts.length,
           final: b.final_state || (b.paused ? "paused" : "running"),
           codes: codes.join(", ") || "—", duration };
}

function compareSummaryHTML(a, b) {
  const sa = summarize(a), sb = summarize(b);
  const row = (k, label) => `<tr><td class="muted">${label}</td>
    <td><b>${esc(sa[k])}</b></td><td><b>${esc(sb[k])}</b></td></tr>`;
  const diff = (x, y) => x === y ? "" : ' style="color:var(--amber)"';
  return `<table style="width:100%;font-size:12px;border-collapse:collapse">
    <tr><th></th><th style="text-align:left">${esc(a.label)}（${short(a.id, 8)}）</th>
        <th style="text-align:left">${esc(b.label)}（${short(b.id, 8)}）</th></tr>
    <tr${diff(sa.attempts, sb.attempts)}>
      <td class="muted">新尝试数</td><td>${sa.attempts}</td><td>${sb.attempts}</td></tr>
    <tr${diff(sa.total, sb.total)}>
      <td class="muted">时间线事件尝试总数</td><td>${sa.total}</td><td>${sb.total}</td></tr>
    <tr${diff(sa.final, sb.final)}>
      <td class="muted">最终归宿</td><td>${badgeText(sa.final)}</td><td>${badgeText(sb.final)}</td></tr>
    <tr${diff(sa.codes, sb.codes)}>
      <td class="muted">出现的状态码</td><td>${esc(sa.codes)}</td><td>${esc(sb.codes)}</td></tr>
    <tr><td class="muted">总耗时（虚拟）</td><td>${(sa.duration / 1000).toFixed(2)}s</td>
        <td>${(sb.duration / 1000).toFixed(2)}s</td></tr>
  </table>`;
}

function badgeText(f) {
  return f === "delivered" ? "✅ 已送达"
    : f === "exhausted" ? "⛔ 已放弃"
    : f === "failed" ? "🚫 永久失败"
    : f === "paused" ? "⏸ 已暂停" : "🔁 进行中";
}

function branchBoxHTML(b) {
  const origin = b.origin_vtime;
  const next = b.final_state ? "—"
    : `${fmtT(b.next_vtime, origin)}（倒计时 <span data-clock="next">…</span>）`;
  const isFork = b.parent_branch_id !== null;
  return `
    <div class="branch-box" data-bid="${b.id}">
      <h4>
        <span>${isFork ? "⑂ " : "🌿 "}${esc(b.label)} <span class="muted" style="font-weight:400">${short(b.id, 8)}</span>
          ${isFork ? `<span class="tag">自 ${short(b.parent_branch_id, 6)}</span>` : ""}
        </span>
        ${branchBadge(b)}
      </h4>
      <div class="clock">
        虚拟时钟 <span data-clock="vtime">…</span> · ${b.paused ? "已暂停" : `${b.speed}×`}
        · 下次尝试：${next}
      </div>
      ${branchControlsHTML(b)}
      <div style="margin-top:8px">${timelineHTML(b)}</div>
    </div>`;
}

function renderDetail() {
  const el = $("#detail-body");
  if (!state.detail) {
    el.innerHTML = `<p class="muted">选择一次投递查看尝试、虚拟时钟与分叉。</p>`;
    $("#compare-b-select").classList.add("hidden");
    $("#btn-clear-compare").classList.add("hidden");
    return;
  }
  const branches = state.detail.branches;
  const A = branches.find((b) => b.id === state.compareA);
  const B = branches.find((b) => b.id === state.compareB);

  // compare pickers
  const sel = $("#compare-b-select");
  sel.classList.remove("hidden");
  sel.innerHTML = branches.map((b) =>
    `<option value="${b.id}" ${b.id === (B ? B.id : "") ? "selected" : ""}>
       对比分支：${esc(b.label)} ${short(b.id, 6)}</option>`).join("");
  $("#btn-clear-compare").classList.toggle("hidden", !B);

  let summary = "";
  if (A && B) {
    summary = `<div class="summary-cell">${compareSummaryHTML(A, B)}</div>`;
  }
  el.innerHTML = `
    <div class="muted" style="font-size:11px;margin-bottom:6px">
      投递 ${state.detail.id} · 场景 ${state.detail.scenario_id}</div>
    ${summary}
    <div class="branch-cols ${B ? "" : "single"}">
      ${A ? branchBoxHTML(A) : ""}
      ${B ? branchBoxHTML(B) : ""}
    </div>`;

  bindBranchControls(A);
  if (B) bindBranchControls(B);
  // fork buttons inside each attempt timeline
  $$(".branch-box").forEach((box) => {
    const bid = box.dataset.bid;
    $$(".timeline li.attempt", box).forEach((li) => {
      if (li.classList.contains("inherited")) return;
      const no = parseInt(li.dataset.attempt, 10);
      if (!Number.isFinite(no)) return;
      const btn = document.createElement("button");
      btn.className = "btn btn-sm";
      btn.style.cssText = "margin-left:6px;padding:0 6px;font-size:11px";
      btn.textContent = "⑂ 从这里分叉";
      btn.addEventListener("click", () => openForkModal(bid, no));
      li.appendChild(btn);
    });
  });
}

$("#compare-b-select").addEventListener("change", (e) => {
  state.compareB = e.target.value || "";
  state.compareClosed = !state.compareB;
  renderDetail();
});
$("#btn-clear-compare").addEventListener("click", () => {
  state.compareB = "";
  state.compareClosed = true;
  renderDetail();
});

function bindBranchControls(b) {
  if (!b) return;
  const box = $(`.branch-box[data-bid="${b.id}"]`);
  if (!box) return;
  $$("[data-act]", box).forEach((node) => {
    if (node.dataset.act === "speed-sel") return;  // handled below
    node.addEventListener("click", async () => {
      const act = node.dataset.act;
      try {
        if (act === "pause") await api("POST", `/api/branches/${b.id}/pause`);
        if (act === "resume") await api("POST", `/api/branches/${b.id}/resume`);
        if (act === "jump") await api("POST", `/api/branches/${b.id}/jump`,
          { vtime_ms: clientVtime(b) + parseInt(node.dataset.ms, 10) });
        if (act === "fork") {
          const maxDone = b.attempts.length;
          openForkModal(b.id, maxDone);
          return;
        }
        // refetch immediately so base_vtime/base_wall stay in sync with server
        const fresh = await api("GET", `/api/deliveries/${state.deliveryId}`);
        state.detail = fresh;
        renderDetail();
      } catch (e) { alert("操作失败：" + e.message); }
    });
    if (node.dataset.act === "speed-sel") {
      node.addEventListener("change", async () => {
        await api("POST", `/api/branches/${b.id}/speed`, { speed: parseFloat(node.value) });
        await loadDetail(state.deliveryId);
      });
    }
  });
}

// ===========================================================================
// fork modal
// ===========================================================================

function openForkModal(branchId, afterAttempt) {
  const b = state.detail.branches.find((x) => x.id === branchId);
  if (!b) return;
  if (afterAttempt == null) afterAttempt = b.attempts.length;
  if (afterAttempt < 0 || afterAttempt > b.attempts.length) return;
  if (!b.paused && !b.final_state) {
    alert("该分支仍在运行。请先暂停（⏸）或等待其结束后再分叉，以保证检查点时刻确定。");
    return;
  }
  state.fork = { branchId, afterAttempt };
  $("#fork-title").textContent = `在「${b.label}」上分叉`;
  $("#fork-hint").textContent =
    `第 1–${afterAttempt} 次尝试作为共享历史被继承（不会重新投递），此后按新规则重放。`;
  $("#fk-cp-text").textContent = afterAttempt;
  $("#fk-label").value = "";
  ["#fk-bo-kind", "#fk-bo-base", "#fk-bo-factor", "#fk-bo-cap", "#fk-bo-jitter",
   "#fk-max"].forEach((s) => { $(s).value = ""; });
  $("#fk-speed").value = "";
  $("#fk-paused").checked = false;
  renderRulesTable($("#fk-rules-body"), []);
  $("#modal-mask").classList.remove("hidden");
}

$("#btn-fk-add-rule").addEventListener("click", () => {
  const n = collectRules($("#fk-rules-body")).length + 1;
  $("#fk-rules-body").appendChild(ruleRow({ attempt_no: n, action: "status", status_code: 200 }));
});
$("#btn-fk-cancel").addEventListener("click", () => $("#modal-mask").classList.add("hidden"));

$("#btn-fk-confirm").addEventListener("click", async () => {
  if (!state.fork) return;
  const { branchId, afterAttempt } = state.fork;
  const rules = collectRules($("#fk-rules-body"));
  const kind = $("#fk-bo-kind").value;
  const body = {
    label: $("#fk-label").value.trim() || `fork@${afterAttempt}`,
    after_attempt: afterAttempt,
    rules: rules.length ? rules : null,
    max_attempts: $("#fk-max").value ? parseInt($("#fk-max").value, 10) : null,
    speed: $("#fk-speed").value ? parseFloat($("#fk-speed").value) : null,
    paused: $("#fk-paused").checked,
  };
  if (kind) {
    body.backoff = {
      kind,
      base_ms: parseInt($("#fk-bo-base").value, 10) || 0,
      factor: parseFloat($("#fk-bo-factor").value) || 1,
      max_delay_ms: parseInt($("#fk-bo-cap").value, 10) || 0,
      jitter: $("#fk-bo-jitter").value || "none",
    };
  }
  try {
    const { id } = await api("POST", `/api/branches/${branchId}/fork`, body);
    $("#modal-mask").classList.add("hidden");
    state.compareB = id;
    await loadDetail(state.deliveryId);
  } catch (e) { alert("分叉失败：" + e.message); }
});

// ===========================================================================
// inbox
// ===========================================================================

async function refreshInbox() {
  if (!state.inboxOpen) return;
  const rows = await api("GET", "/api/inbox");
  $("#inbox-body").innerHTML = rows.length ? rows.map((r) => `
    <div class="inbox-row">
      <div class="k">${esc(r.idempotency_key)}</div>
      <div class="muted">分支 ${short(r.branch_id, 10)} · 第 ${r.attempt_no} 次 ·
        vtime ${r.received_at_vtime}</div>
      <pre style="margin:4px 0 0;white-space:pre-wrap;font-size:11px">${esc(
        JSON.stringify(r.payload, null, 2))}</pre>
    </div>`).join("") : `<p class="muted">收件箱为空。</p>`;
}

$("#btn-open-inbox").addEventListener("click", async () => {
  state.inboxOpen = true;
  $("#inbox-drawer").classList.add("open");
  await refreshInbox();
});
$("#btn-close-inbox").addEventListener("click", () => {
  state.inboxOpen = false;
  $("#inbox-drawer").classList.remove("open");
});

// ===========================================================================
// live clock tick
// ===========================================================================

function tickClocks() {
  if (!state.detail) return;
  $$(".branch-box").forEach((box) => {
    const b = state.detail.branches.find((x) => x.id === box.dataset.bid);
    if (!b) return;
    const now = clientVtime(b);
    const rel = ((now - b.origin_vtime) / 1000).toFixed(2);
    const vn = $("[data-clock='vtime']", box);
    if (vn) vn.textContent = `T+${rel}s`;
    const nx = $("[data-clock='next']", box);
    if (nx && !b.final_state) {
      nx.textContent = Math.max(0, ((b.next_vtime - now) / 1000)).toFixed(2) + "s";
    }
  });
}
setInterval(tickClocks, 250);

// ===========================================================================
// SSE
// ===========================================================================

let detailDirty = false;
let listDirty = false;

function connectSSE() {
  const es = new EventSource("/api/stream");
  const dot = $("#sse-dot"), txt = $("#sse-text");
  es.onopen = () => { dot.className = "dot on"; txt.textContent = "实时已连接"; };
  es.onerror = () => { dot.className = "dot off"; txt.textContent = "连接断开，重连中…"; };

  const onChange = (deliveryId) => {
    if (state.deliveryId && deliveryId === state.deliveryId) {
      detailDirty = true;
    }
    listDirty = true;
  };

  es.addEventListener("evt", (e) => {
    let p;
    try { p = JSON.parse(e.data); } catch (_) { return; }
    onChange(p.delivery_id);
    if (state.inboxOpen && p.type === "attempt") listDirty = true;
  });
  es.addEventListener("clock", (e) => {
    let p;
    try { p = JSON.parse(e.data); } catch (_) { return; }
    if (state.detail && p.delivery_id === state.deliveryId) detailDirty = true;
  });

  setInterval(async () => {
    if (detailDirty) {
      detailDirty = false;
      try { await loadDetail(state.deliveryId); } catch (_) {}
    }
    if (listDirty) {
      listDirty = false;
      try {
        await loadDeliveries(true);
        if (state.inboxOpen) await refreshInbox();
      } catch (_) {}
    }
  }, 200);
}

// ===========================================================================
// boot
// ===========================================================================

(async function boot() {
  await loadScenarios();
  connectSSE();
})();
