/* Webhook 故障演练与回放台 —— 原生 JS SPA */
"use strict";

// ----------------------------------------------------------------- 工具

const $ = (sel, root = document) => root.querySelector(sel);
const h = (tag, attrs = {}, ...children) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
};

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = JSON.stringify(await res.json()); } catch (_) {}
    throw new Error(`${res.status} ${detail}`);
  }
  return res.status === 204 ? null : res.json();
};

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  setTimeout(() => t.classList.add("hidden"), 3200);
  t.classList.remove("hidden");
}

const fmtMs = (ms) => {
  if (ms === null || ms === undefined) return "—";
  const d = new Date(ms);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
};
const fmtRel = (ms) => {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(2)} s`;
  return `${(ms / 60000).toFixed(1)} min`;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const statusBadge = (s) => h("span", { class: `badge ${s}` }, s);
const runBadge = (s) => h("span", { class: `badge b-${s}` }, s);

// ----------------------------------------------------------------- 全局 SSE

let evtSource = null;
const sseListeners = new Set();

function connectSSE() {
  if (evtSource) evtSource.close();
  const es = new EventSource("/api/events");
  evtSource = es;
  const setStatus = (cls, txt) => {
    const el = $("#sse-status");
    el.className = "sse-status " + cls;
    el.textContent = txt;
  };
  es.onopen = () => setStatus("online", "● 实时已连接");
  es.onerror = () => setStatus("offline", "○ 重连中…");
  es.onmessage = (ev) => {
    let evt;
    try { evt = JSON.parse(ev.data); } catch (_) { return; }
    sseListeners.forEach((fn) => { try { fn(evt); } catch (e) { console.error(e); } });
  };
}

// ----------------------------------------------------------------- 路由

const routes = {};
function route(pattern) {
  return (fn) => { routes[pattern] = fn; };
}

function parseHash() {
  const hash = location.hash || "#/scenarios";
  const [path, query = ""] = hash.slice(1).split("?");
  const parts = path.split("/").filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(query));
  return { parts, params };
}

async function render() {
  document.querySelectorAll(".topbar nav a").forEach((a) => a.classList.remove("active"));
  const { parts, params } = parseHash();
  const view = $("#view");
  view.innerHTML = "";
  const key = parts[0] || "scenarios";
  const navEl = $(`.topbar nav a[data-route="${key}"]`);
  if (navEl) navEl.classList.add("active");
  try {
    if (parts[0] === "runs" && parts[1]) await runView(view, parts[1]);
    else if (parts[0] === "scenarios" && parts[1]) await scenarioView(view, parts[1]);
    else if (parts[0] === "compare") await compareView(view, params);
    else if (parts[0] === "effects") await effectsView(view);
    else await scenariosView(view);
  } catch (e) {
    view.appendChild(h("div", { class: "panel" }, `加载失败：${esc(e.message)}`));
  }
}
window.addEventListener("hashchange", render);

// ----------------------------------------------------------------- 场景列表

async function scenariosView(view) {
  const scenarios = await api("/api/scenarios");
  view.appendChild(h("h1", {}, "故障场景"));

  const list = h("div", { class: "cards" });
  if (!scenarios.length) {
    list.appendChild(h("div", { class: "panel muted" }, "还没有场景，先在下面创建一个。"));
  }
  for (const s of scenarios) {
    const rules = s.rules.length
      ? s.rules.map((r) => `#${r.attempt}:${r.action}${r.status_code ? `(${r.status_code})` : ""}`).join("，")
      : `默认: ${s.default_action}`;
    list.appendChild(h("div", {
      class: "card",
      onclick: () => location.hash = `#/scenarios/${s.id}`,
    },
      h("div", { class: "row spread" }, h("strong", {}, s.name), h("span", { class: "muted small mono" }, s.id.slice(0, 6))),
      h("div", { class: "muted small", style: "margin:8px 0" }, rules),
      h("div", { class: "muted small mono" }, `退避 ${s.backoff.base_ms}ms ×${s.backoff.factor}，最多 ${s.backoff.max_attempts} 次 · ${s.verify_signature ? "验签" : "不验签"}`),
    ));
  }
  view.appendChild(list);
  scenarioForm(view, null);
}

// ----------------------------------------------------------------- 场景表单

function ruleRow(r = {}) {
  const attempt = h("input", { type: "number", min: "1", value: r.attempt ?? "", placeholder: "次" });
  const action = h("select", {},
    ...["success", "status", "timeout", "disconnect"].map((a) =>
      h("option", { value: a, ...(r.action === a ? { selected: "selected" } : {}) }, a)));
  const code = h("input", { type: "number", value: r.status_code ?? "", placeholder: "状态码" });
  const delay = h("input", { type: "number", value: r.delay_ms ?? 0, placeholder: "延迟ms" });
  const del = h("button", { class: "ghost", type: "button", onclick: (e) => e.target.closest(".rule-row").remove() }, "✕");
  return h("div", { class: "rule-row" }, attempt, action, code, delay, del);
}

function scenarioForm(view, existing) {
  const data = existing || {
    name: "", body: JSON.stringify({ event: "order.paid", data: { id: 42 } }, null, 2),
    secret: "shh-secret", verify_signature: true, url_path: "/hook",
    backoff: { base_ms: 500, factor: 2, max_ms: 30000, max_attempts: 5, jitter: false },
    rules: [], default_action: "success", default_status_code: 200, default_delay_ms: 0,
  };

  const name = h("input", { value: data.name, placeholder: "场景名称，如：支付回调前两次 500" });
  const body = h("textarea", {}, typeof data.body === "string" ? data.body : JSON.stringify(data.body, null, 2));
  const secret = h("input", { value: data.secret });
  const urlPath = h("input", { value: data.url_path });
  const verify = h("input", { type: "checkbox", ...(data.verify_signature ? { checked: "checked" } : {}) });
  const base = h("input", { type: "number", value: data.backoff.base_ms });
  const factor = h("input", { type: "number", step: "0.1", value: data.backoff.factor });
  const maxMs = h("input", { type: "number", value: data.backoff.max_ms });
  const maxAttempts = h("input", { type: "number", min: "1", max: "50", value: data.backoff.max_attempts });
  const jitter = h("input", { type: "checkbox", ...(data.backoff.jitter ? { checked: "checked" } : {}) });

  const rulesBox = h("div", {});
  data.rules.forEach((r) => rulesBox.appendChild(ruleRow(r)));
  const addRule = h("button", {
    class: "secondary", type: "button",
    onclick: () => rulesBox.appendChild(ruleRow({ attempt: rulesBox.children.length + 1 })),
  }, "+ 添加按次数的规则");

  const defAction = h("select", {},
    ...["success", "status", "timeout", "disconnect"].map((a) =>
      h("option", { value: a, ...(data.default_action === a ? { selected: "selected" } : {}) }, a)));
  const defCode = h("input", { type: "number", value: data.default_status_code });
  const defDelay = h("input", { type: "number", value: data.default_delay_ms });

  const panel = h("div", { class: "panel" },
    h("h2", {}, existing ? "编辑场景" : "创建场景"),
    h("div", { class: "grid grid-2" },
      h("div", {},
        h("div", { class: "field" }, h("label", {}, "名称"), name),
        h("div", { class: "field" }, h("label", {}, "请求体（JSON）"), body),
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", {}, "签名密钥（HMAC）"), secret),
          h("div", { class: "field" }, h("label", {}, "接收端路径"), urlPath)),
        h("div", { class: "field" }, h("label", { class: "row", style: "gap:6px" }, verify, "校验 HMAC 签名（错误签名返回 401）"))),
      h("div", {},
        h("label", {}, "退避策略"),
        h("div", { class: "grid grid-2" },
          h("div", { class: "field" }, h("label", {}, "基础 ms"), base),
          h("div", { class: "field" }, h("label", {}, "倍率"), factor),
          h("div", { class: "field" }, h("label", {}, "上限 ms"), maxMs),
          h("div", { class: "field" }, h("label", {}, "最大尝试次数"), maxAttempts)),
        h("div", { class: "field" }, h("label", { class: "row", style: "gap:6px" }, jitter, "抖动（确定性，回放仍稳定）")),
        h("label", {}, "默认动作（未配置规则的尝试）"),
        h("div", { class: "grid grid-3" },
          h("div", { class: "field" }, h("label", {}, "动作"), defAction),
          h("div", { class: "field" }, h("label", {}, "状态码"), defCode),
          h("div", { class: "field" }, h("label", {}, "延迟 ms"), defDelay)),
      )),
    h("label", {}, "按尝试次数触发的规则（超时/断连/状态码）"),
    h("div", { class: "small muted", style: "margin-bottom:6px" }, "列：尝试次数 / 动作 / 状态码 / 挂起延迟(ms)"),
    rulesBox,
    h("div", { class: "row", style: "margin-top:8px" }, addRule),
    h("div", { class: "row", style: "margin-top:14px" },
      h("button", {
        onclick: async () => {
          let bodyJson;
          try { bodyJson = JSON.parse(body.value); }
          catch (_) { return toast("请求体不是合法 JSON", true); }
          const payload = {
            name: name.value.trim(), body: bodyJson, secret: secret.value,
            verify_signature: verify.checked, url_path: urlPath.value,
            backoff: {
              base_ms: +base.value, factor: +factor.value, max_ms: +maxMs.value,
              max_attempts: +maxAttempts.value, jitter: jitter.checked,
            },
            rules: [...rulesBox.querySelectorAll(".rule-row")].map((row) => {
              const [a, ac, c, d] = row.querySelectorAll("input, select");
              return {
                attempt: +a.value, action: ac.value,
                status_code: c.value ? +c.value : null,
                delay_ms: +d.value || 0,
              };
            }).filter((r) => r.attempt > 0),
            default_action: defAction.value,
            default_status_code: +defCode.value,
            default_delay_ms: +defDelay.value || 0,
          };
          if (!payload.name) return toast("名称不能为空", true);
          try {
            if (existing) {
              await api(`/api/scenarios/${existing.id}`, { method: "PUT", body: payload });
              toast("已保存（不影响已创建的演练快照）");
              render();
            } else {
              const s = await api("/api/scenarios", { method: "POST", body: payload });
              toast("已创建场景");
              location.hash = `#/scenarios/${s.id}`;
            }
          } catch (e) { toast(e.message, true); }
        },
      }, existing ? "保存修改" : "创建场景"),
      existing && h("button", {
        class: "danger",
        onclick: async () => {
          if (!confirm("删除该场景？（历史演练保留）")) return;
          await api(`/api/scenarios/${existing.id}`, { method: "DELETE" });
          location.hash = "#/scenarios";
        },
      }, "删除")),
  );
  view.appendChild(panel);
}

// ----------------------------------------------------------------- 场景详情 + 演练

async function scenarioView(view, id) {
  const s = await api(`/api/scenarios/${id}`).catch(() => null);
  if (!s) { view.appendChild(h("div", { class: "panel" }, "场景不存在")); scenarioForm(view, null); return; }

  const runs = await api(`/api/runs?scenario_id=${id}`);

  view.appendChild(h("div", { class: "breadcrumb" },
    h("a", { href: "#/scenarios" }, "场景"), " / ", esc(s.name)));
  view.appendChild(h("h1", {}, s.name));

  const startPanel = h("div", { class: "panel" },
    h("div", { class: "row spread" },
      h("div", {},
        h("strong", {}, "开始一次新演练"),
        h("div", { class: "muted small" },
          `虚拟时钟默认可暂停/变速，投递带稳定幂等键与 HMAC 签名，调度到 ${s.backoff.max_attempts} 次为止`)),
      h("div", { class: "row" },
        h("button", {
          onclick: async () => {
            const r = await api(`/api/scenarios/${id}/runs`, { method: "POST", body: {} });
            location.hash = `#/runs/${r.id}`;
          },
        }, "▶ 以 10× 时钟开始"),
        h("button", {
          class: "secondary",
          onclick: async () => {
            const r = await api(`/api/scenarios/${id}/runs`, { method: "POST", body: { speed: 1 } });
            location.hash = `#/runs/${r.id}`;
          },
        }, "1× 实时开始"),
        h("button", {
          class: "secondary",
          onclick: async () => {
            const r = await api(`/api/scenarios/${id}/runs`, { method: "POST", body: { speed: 0.0001 } });
            location.hash = `#/runs/${r.id}`;
          },
        }, "🐢 慢速（便于暂停）"))));
  view.appendChild(startPanel);

  const table = h("table", {},
    h("thead", {}, h("tr", {},
      h("th", {}, "演练"), h("th", {}, "状态"), h("th", {}, "谱系"),
      h("th", {}, "尝试"), h("th", {}, "创建时间"))),
    h("tbody", {}, ...runs.map((r) =>
      h("tr", {
        class: "clickable",
        onclick: () => location.hash = `#/runs/${r.id}`,
      },
        h("td", { class: "mono" }, r.id, r.label ? h("div", { class: "muted" }, r.label) : null),
        h("td", {}, runBadge(r.status)),
        h("td", { class: "mono small" }, ["根", ...r.lineage].join(" → ")),
        h("td", {}, r.outcome ? String(r.outcome.attempts) : "—"),
        h("td", { class: "small muted" }, fmtMs(r.created_at))))));
  view.appendChild(h("div", { class: "panel" }, h("h2", { style: "margin-top:0" }, "演练历史"), runs.length ? table : h("div", { class: "muted" }, "暂无")));

  scenarioForm(view, s);
}

// ----------------------------------------------------------------- 演练详情（时间线）

let currentRun = null;
let currentRunId = null;
let runClockOffset = 0;

async function runView(view, runId) {
  currentRunId = runId;
  currentRun = await api(`/api/runs/${runId}`);

  const s = currentRun.snapshot;
  const rootId = currentRun.root_run_id;
  const family = await api(`/api/families/${rootId}`);

  view.appendChild(h("div", { class: "breadcrumb" },
    h("a", { href: "#/scenarios" }, "场景"), " / ",
    h("a", { href: `#/scenarios/${currentRun.scenario_id}` }, esc(currentRun.name)), " / ",
    h("span", { class: "mono" }, runId)));

  view.appendChild(h("h1", {}, currentRun.name + " ",
    currentRun.label ? h("span", { class: "badge inherited" }, currentRun.label) : null));

  // 分支树
  const branchPanel = h("div", { class: "panel" });
  branchPanel.appendChild(h("h2", { style: "margin-top:0" },
    "分支谱系", h("span", { class: "muted small" },
      `　共 ${family.side_effects} 个业务副作用（${family.deliveries} 次已确认投递），重放不重复`)));
  const branchRow = h("div", { class: "row" });
  for (const b of family.branches) {
    const active = b.id === runId;
    branchRow.appendChild(h("a", {
      href: `#/runs/${b.id}`,
      class: "card",
      style: active ? "border-color:var(--accent);cursor:default" : "",
    },
      h("div", { class: "row spread" },
        h("strong", { class: "mono small" }, b.id.slice(0, 8)),
        runBadge(b.status)),
      h("div", { class: "muted small mono", style: "margin-top:4px" },
        ["根", ...b.lineage].join(" → ")),
      b.label ? h("div", { class: "small" }, b.label) : null));
  }
  branchPanel.appendChild(branchRow);
  view.appendChild(branchPanel);

  // 时钟控制条
  const clockBar = h("div", { class: "clockbar" });
  const vtEl = h("span", { class: "vt" }, fmtMs(currentRun.vt_now));
  const statusSlot = h("span", {});
  const speedInput = h("input", { type: "number", step: "0.1", min: "0", value: currentRun.speed, style: "width:90px" });
  const btnSlot = h("span", { class: "row" });
  const nextEl = h("span", { class: "small muted" });

  function buildButtons(run) {
    return [
      h("button", {
        class: "secondary", disabled: run.status !== "running",
        onclick: () => control({ action: "pause" }),
      }, "⏸ 暂停"),
      h("button", {
        disabled: run.status !== "paused",
        onclick: () => control({ action: "resume" }),
      }, "▶ 恢复"),
      h("button", { class: "secondary", onclick: () => control({ action: "speed", speed: +speedInput.value }) }, "应用倍率"),
      h("button", {
        class: "secondary", disabled: run.status !== "paused",
        onclick: () => control({ action: "advance", advance_ms: 1 }),
      }, "⏭ +1ms"),
      h("button", {
        class: "secondary", disabled: run.status !== "paused",
        onclick: () => control({ action: "advance", advance_ms: 1000 }),
      }, "⏭ +1s"),
    ];
  }
  async function control(body) {
    try {
      currentRun = await api(`/api/runs/${runId}/control`, { method: "POST", body });
      paint();
    } catch (e) { toast(e.message, true); }
  }

  clockBar.append(h("span", {}, "虚拟时钟 "), vtEl, statusSlot, h("span", { class: "muted small" }, "倍率"),
    speedInput, btnSlot, h("span", { style: "flex:1" }), nextEl);
  view.appendChild(clockBar);

  // 主体：尝试 + 事件
  const bodyGrid = h("div", { class: "grid grid-2" });
  const attemptsCol = h("div", { class: "panel compare-col" }, h("h2", { style: "margin-top:0" }, "投递尝试"));
  const eventsCol = h("div", { class: "panel compare-col" }, h("h2", { style: "margin-top:0" }, "事件序列（SSE 实时）"));
  bodyGrid.append(attemptsCol, eventsCol);
  view.appendChild(bodyGrid);

  // 结局 / 分叉操作
  const footer = h("div", { class: "panel" });
  view.appendChild(footer);

  function paint() {
    vtEl.textContent = fmtMs(currentRun.vt_now);
    statusSlot.replaceChildren(runBadge(currentRun.status));
    speedInput.value = currentRun.speed;

    // 重建按钮状态
    btnSlot.replaceChildren(...buildButtons(currentRun));

    if (currentRun.next_planned_ms) {
      const wait = Math.max(0, currentRun.next_planned_ms - currentRun.vt_now);
      nextEl.textContent = currentRun.status === "paused"
        ? `下次尝试 #${currentRun.next_attempt}：已排定，恢复后到期（等待 ${fmtRel(wait)}）`
        : `下次尝试 #${currentRun.next_attempt}：${fmtMs(currentRun.next_planned_ms)}（约 ${fmtRel(wait)} 后）`;
    } else {
      nextEl.textContent = currentRun.outcome
        ? `结局：${currentRun.outcome.result}（共 ${currentRun.outcome.attempts} 次尝试）`
        : "无计划任务";
    }

    // 尝试卡片
    attemptsCol.innerHTML = "";
    attemptsCol.appendChild(h("h2", { style: "margin-top:0" }, "投递尝试"));
    for (const a of currentRun.attempts) {
      const head = h("div", { class: "row spread" },
        h("div", { class: "row" },
          h("strong", {}, `#${a.attempt}`),
          statusBadge(a.result),
          a.inherited ? h("span", { class: "badge inherited" }, "继承") : null,
          a.replay ? h("span", { class: "badge replay" }, "幂等重放") : null,
          a.sig_valid === false ? h("span", { class: "badge failure" }, "签名无效") : null,
          a.response_status ? h("span", { class: "mono small" }, `HTTP ${a.response_status}`) : null),
        h("div", { class: "muted small mono" },
          `计划 ${fmtMs(a.scheduled_ms)}${a.real_duration_ms != null ? ` · 耗时 ${a.real_duration_ms}ms` : ""}`));
      const ruleLine = h("div", { class: "small muted", style: "margin:4px 0" },
        `规则：${a.action}${a.planned_status ? ` / ${a.planned_status}` : ""}${a.planned_delay ? ` / 挂起 ${a.planned_delay}ms` : ""}`);
      const keyLine = h("div", { class: "keyval small" }, a.idempotency_key);
      const errLine = a.error ? h("div", { class: "small", style: "color:var(--red)" }, esc(a.error)) : null;
      const forkBtn = (a.status === "done" && a.attempt < (currentRun.snapshot.backoff.max_attempts))
        ? h("button", { class: "secondary", style: "margin-top:8px", onclick: () => openForkModal(a.attempt) }, "⑂ 从此检查点分叉重放")
        : (a.status === "done" ? h("button", { class: "secondary", style: "margin-top:8px", onclick: () => openForkModal(a.attempt) }, "⑂ 从此检查点分叉") : null);
      attemptsCol.appendChild(h("div", { class: `attempt ${a.result}` }, head, ruleLine, keyLine, errLine, forkBtn));
    }

    // 事件
    eventsCol.innerHTML = "";
    eventsCol.appendChild(h("h2", { style: "margin-top:0" }, "事件序列（SSE 实时）"));
    for (const e of currentRun.events) {
      eventsCol.appendChild(h("div", { class: "event" + (e.inherited ? " inherited" : "") },
        h("span", { class: "t" }, fmtMs(e.at_ms)),
        h("span", { class: "event-kind" }, e.kind),
        h("span", {}, summarizeEvent(e))));
    }
    eventsCol.scrollTop = eventsCol.scrollHeight;

    // 结局面板
    footer.innerHTML = "";
    footer.appendChild(h("h2", { style: "margin-top:0" }, "最终归宿"));
    if (currentRun.outcome) {
      const o = currentRun.outcome;
      footer.appendChild(h("div", { class: "row" },
        runBadge(o.result === "succeeded" ? "succeeded" : "exhausted"),
        h("span", {}, o.result === "succeeded"
          ? `第 ${o.attempts} 次尝试投递成功（HTTP ${o.last_status}）${o.replay ? "，且为幂等重放，未产生重复副作用" : ""}`
          : `${o.attempts} 次尝试全部失败（${o.last_result}${o.last_status ? ` / HTTP ${o.last_status}` : ""}）`),
        o.side_effect_id ? h("span", { class: "badge success" }, `副作用 #${o.side_effect_id}`) : null));
    } else {
      footer.appendChild(h("div", { class: "muted" }, "演练进行中…"));
    }
    if (currentRun.parent_run_id) {
      footer.appendChild(h("div", { style: "margin-top:10px" },
        h("a", { href: `#/compare?a=${currentRun.parent_run_id}&b=${currentRun.id}` },
          "→ 与父分支并排比较")));
    }
  }

  paint();

  // SSE：本分支相关事件驱动轻量重取（节流）
  let scheduled = false;
  const onEvt = (evt) => {
    if (evt.run_id !== runId && evt.run_id !== "_clock") return;
    if (evt.run_id === "_clock") {
      const c = evt.data.clocks.find((x) => x.run_id === runId);
      if (c) {
        currentRun.vt_now = c.vt_now;
        currentRun.status = c.status;
        currentRun.next_planned_ms = c.next_planned_ms;
        currentRun.next_attempt = c.next_attempt;
        currentRun.speed = c.speed;
        vtEl.textContent = fmtMs(c.vt_now);
        if (currentRun.next_planned_ms) {
          const wait = Math.max(0, currentRun.next_planned_ms - c.vt_now);
          nextEl.textContent = c.status === "paused"
            ? `下次尝试 #${c.next_attempt}：已排定，恢复后到期（等待 ${fmtRel(wait)}）`
            : `下次尝试 #${c.next_attempt}：${fmtMs(currentRun.next_planned_ms)}（约 ${fmtRel(wait)} 后）`;
        }
      }
      return;
    }
    if (scheduled) return;
    scheduled = true;
    setTimeout(async () => {
      scheduled = false;
      try {
        currentRun = await api(`/api/runs/${runId}`);
        paint();
      } catch (_) {}
    }, 120);
  };
  sseListeners.add(onEvt);

  // 离开页面时注销
  const cleanup = () => {
    sseListeners.delete(onEvt);
    window.removeEventListener("hashchange", cleanup);
  };
  window.addEventListener("hashchange", cleanup, { once: true });
}

function summarizeEvent(e) {
  const d = e.data || {};
  switch (e.kind) {
    case "attempt_started": return `开始投递 #${d.attempt}`;
    case "attempt_finished": return `#${d.attempt} 结果：${d.result}${d.response_status ? ` (HTTP ${d.response_status})` : ""}${d.replay ? " [幂等重放]" : ""}`;
    case "attempt_scheduled": return `排定 #${d.attempt}：${fmtMs(d.scheduled_ms)}（退避 ${fmtRel(d.delay_ms)}），规则 ${d.rule?.action}`;
    case "run_created": return `演练创建，时钟倍率 ${d.speed}`;
    case "run_finished": return `结束：${d.result}，共 ${d.attempts} 次`;
    case "forked": return `从检查点 #${d.checkpoint} 分叉${d.label ? `（${d.label}）` : ""}`;
    case "paused": return "时钟暂停";
    case "resumed": return "时钟恢复";
    case "speed_changed": return `倍率调整为 ${d.speed}`;
    case "advanced": return `单步推进 ${fmtRel(d.by_ms)}`;
    default: return JSON.stringify(d);
  }
}

// ----------------------------------------------------------------- 分叉弹窗

function openForkModal(checkpoint) {
  const root = $("#modal-root");
  const snap = currentRun.snapshot;

  const rulesBox = h("div", {});
  // 默认拷贝当前规则
  snap.rules.forEach((r) => rulesBox.appendChild(ruleRow(r)));
  if (!snap.rules.length && checkpoint + 1 <= snap.backoff.max_attempts) {
    rulesBox.appendChild(ruleRow({ attempt: checkpoint + 1, action: "success" }));
  }

  const defAction = h("select", {},
    ...["success", "status", "timeout", "disconnect"].map((a) =>
      h("option", { value: a, ...(snap.default_action === a ? { selected: "selected" } : {}) }, a)));
  const defCode = h("input", { type: "number", value: snap.default_status_code });
  const defDelay = h("input", { type: "number", value: snap.default_delay_ms });
  const label = h("input", { placeholder: "分支标签（可选），如：把第二次 500 改成成功" });

  const close = () => root.innerHTML = "";
  const backdrop = h("div", { class: "modal-backdrop", onclick: (e) => { if (e.target === e.currentTarget) close(); } },
    h("div", { class: "modal" },
      h("h2", { style: "margin-top:0" }, `在检查点 #${checkpoint} 分叉并重放`),
      h("div", { class: "muted small", style: "margin-bottom:12px" },
        "分叉点之前（含检查点）的尝试与事件将被继承（幂等键不变）；之后的投递使用下面的新规则，生成新的幂等键，可与原分支并排比较。"),
      h("div", { class: "field" }, h("label", {}, "分支标签"), label),
      h("label", {}, "重放规则（按尝试次数）"),
      h("div", { class: "small muted", style: "margin-bottom:6px" }, "尝试次数 / 动作 / 状态码 / 挂起延迟(ms)"),
      rulesBox,
      h("button", {
        class: "secondary", type: "button", style: "margin:6px 0",
        onclick: () => rulesBox.appendChild(ruleRow({ attempt: checkpoint + 1 })),
      }, "+ 添加规则"),
      h("label", { style: "margin-top:8px" }, "默认动作覆盖"),
      h("div", { class: "grid grid-3" },
        h("div", { class: "field" }, h("label", {}, "动作"), defAction),
        h("div", { class: "field" }, h("label", {}, "状态码"), defCode),
        h("div", { class: "field" }, h("label", {}, "延迟 ms"), defDelay)),
      h("div", { class: "row", style: "margin-top:12px" },
        h("button", {
          onclick: async () => {
            const payload = {
              checkpoint_attempt: checkpoint,
              label: label.value || null,
              rules: [...rulesBox.querySelectorAll(".rule-row")].map((row) => {
                const [a, ac, c, d] = row.querySelectorAll("input, select");
                return {
                  attempt: +a.value, action: ac.value,
                  status_code: c.value ? +c.value : null,
                  delay_ms: +d.value || 0,
                };
              }).filter((r) => r.attempt > 0),
              default_action: defAction.value,
              default_status_code: +defCode.value,
              default_delay_ms: +defDelay.value || 0,
            };
            try {
              const child = await api(`/api/runs/${currentRun.id}/fork`, { method: "POST", body: payload });
              close();
              toast("已创建分叉（初始暂停）");
              location.hash = `#/compare?a=${currentRun.id}&b=${child.id}`;
            } catch (e) { toast(e.message, true); }
          },
        }, "创建分叉"),
        h("button", { class: "ghost", onclick: close }, "取消"))));
  root.appendChild(backdrop);
}

// ----------------------------------------------------------------- 并排对比

async function compareView(view, params) {
  const allRuns = await api("/api/runs?limit=200");
  view.appendChild(h("h1", {}, "分支并排比较"));

  const aSel = h("select", {}, ...allRuns.map((r) =>
    h("option", { value: r.id, ...(params.a === r.id ? { selected: "selected" } : {}) },
      `${r.id.slice(0, 8)} · ${r.name}${r.label ? ` (${r.label})` : ""}`)));
  const bSel = h("select", {}, ...allRuns.map((r) =>
    h("option", { value: r.id, ...(params.b === r.id ? { selected: "selected" } : {}) },
      `${r.id.slice(0, 8)} · ${r.name}${r.label ? ` (${r.label})` : ""}`)));
  const go = () => location.hash = `#/compare?a=${aSel.value}&b=${bSel.value}`;
  view.appendChild(h("div", { class: "panel row" },
    h("div", { style: "flex:1" }, h("label", {}, "分支 A"), aSel),
    h("div", { style: "flex:1" }, h("label", {}, "分支 B"), bSel),
    h("button", { onclick: go }, "比较")));

  const aid = params.a || allRuns[0]?.id;
  const bid = params.b || allRuns[1]?.id;
  if (!aid || !bid) {
    view.appendChild(h("div", { class: "panel muted" }, "至少需要两条演练（在某条演练的尝试卡片上点“从此检查点分叉”）。"));
    return;
  }
  const [ra, rb] = await Promise.all([api(`/api/runs/${aid}`), api(`/api/runs/${bid}`)]);
  const grid = h("div", { class: "grid grid-2" });
  grid.appendChild(compareColumn(ra, rb, "A"));
  grid.appendChild(compareColumn(rb, ra, "B"));
  view.appendChild(grid);
}

function compareColumn(run, other, tag) {
  const col = h("div", { class: "panel compare-col" });
  col.appendChild(h("div", { class: "row spread" },
    h("div", {},
      h("strong", {}, `${tag}：${run.name} `),
      run.label ? h("span", { class: "badge inherited" }, run.label) : null,
      h("div", { class: "muted small mono" }, run.id, " · 谱系 ", ["根", ...run.lineage].join(" → "))),
    h("div", { class: "row" }, runBadge(run.status),
      h("a", { href: `#/runs/${run.id}` }, h("button", { class: "secondary" }, "打开")))));

  if (run.status === "running" || run.status === "paused") {
    const speedBox = h("input", { type: "number", step: "0.1", min: "0",
      value: run.speed, style: "width:80px" });
    col.appendChild(h("div", { class: "row small", style: "margin:8px 0" },
      h("button", {
        class: "secondary", disabled: run.status !== "running",
        onclick: async () => { await api(`/api/runs/${run.id}/control`, { method: "POST", body: { action: "pause" } }); render(); },
      }, "暂停"),
      h("button", {
        disabled: run.status !== "paused",
        onclick: async () => { await api(`/api/runs/${run.id}/control`, { method: "POST", body: { action: "resume" } }); render(); },
      }, "恢复"),
      h("span", { class: "muted" }, "倍率"), speedBox,
      h("button", {
        class: "secondary",
        onclick: async () => { await api(`/api/runs/${run.id}/control`, { method: "POST", body: { action: "speed", speed: +speedBox.value } }); toast("已调整倍率"); },
      }, "应用")));
  }

  const o = run.outcome;
  col.appendChild(h("div", { class: "clockbar", style: "margin:10px 0" },
    h("span", {}, "结局："),
    o ? runBadge(o.result === "succeeded" ? "succeeded" : "exhausted")
      : h("span", { class: "muted" }, "进行中"),
    h("span", { class: "muted small" },
      o ? (o.result === "succeeded"
        ? `第 ${o.attempts} 次成功${o.replay ? "（幂等重放）" : ""}${o.side_effect_id ? ` · 副作用 #${o.side_effect_id}` : ""}`
        : `${o.attempts} 次后耗尽`) : "—")));

  const list = h("div", {});
  const maxLen = Math.max(run.attempts.length, other.attempts.length);
  for (let i = 0; i < maxLen; i++) {
    const a = run.attempts[i];
    const b = other.attempts[i];
    const differ = a && b && (a.result !== b.result || a.response_status !== b.response_status
      || a.action !== b.action || a.replay !== b.replay);
    const onlyHere = a && !b;
    if (!a) {
      list.appendChild(h("div", { class: "attempt pending" },
        h("div", { class: "row" }, h("strong", {}, `#${i + 1}`), h("span", { class: "muted small" }, "此分支无该尝试"))));
      continue;
    }
    const inherited = a.inherited ? h("span", { class: "badge inherited" }, "继承") : null;
    const replay = a.replay ? h("span", { class: "badge replay" }, "幂等重放") : null;
    list.appendChild(h("div", {
      class: `attempt ${a.result}` + (differ || onlyHere ? " diff-highlight" : ""),
    },
      h("div", { class: "row spread" },
        h("div", { class: "row" }, h("strong", {}, `#${a.attempt}`), statusBadge(a.result), inherited, replay,
          a.response_status ? h("span", { class: "mono small" }, `HTTP ${a.response_status}`) : null),
        h("span", { class: "muted small mono" }, fmtMs(a.scheduled_ms))),
      h("div", { class: "small muted" },
        `规则 ${a.action}${a.planned_status ? `/${a.planned_status}` : ""} · 耗时 ${a.real_duration_ms ?? "—"}ms`),
      a.error ? h("div", { class: "small", style: "color:var(--red)" }, esc(a.error)) : null,
      differ ? h("div", { class: "small", style: "color:var(--amber)" }, "⚑ 与另一分支此处不同")
        : (onlyHere ? h("div", { class: "small", style: "color:var(--amber)" }, "⚑ 该分支独有") : null)));
  }
  col.appendChild(list);

  // 事件序列对比
  col.appendChild(h("h2", {}, "事件序列"));
  const evBox = h("div", {});
  const otherSeq = other.events.map((e) => `${e.kind}:${e.attempt ?? ""}`);
  for (const e of run.events) {
    const sig = `${e.kind}:${e.attempt ?? ""}`;
    const diff = !otherSeq.includes(sig);
    evBox.appendChild(h("div", { class: "event" + (e.inherited ? " inherited" : "") + (diff ? " diff-highlight" : "") },
      h("span", { class: "t" }, fmtMs(e.at_ms)),
      h("span", { class: "event-kind" }, e.kind),
      h("span", {}, summarizeEvent(e))));
  }
  col.appendChild(evBox);
  return col;
}

// ----------------------------------------------------------------- 已确认投递

async function effectsView(view) {
  const effects = await api("/api/side-effects");
  view.appendChild(h("h1", {}, "已确认投递（业务副作用）"));
  view.appendChild(h("div", { class: "panel muted" },
    "接收端仅在收到 2xx 时创建副作用，且按幂等键全局去重。崩溃恢复、手动重放、继承的尝试都不会产生重复记录。"));
  const table = h("table", {},
    h("thead", {}, h("tr", {},
      h("th", {}, "#"), h("th", {}, "根演练"), h("th", {}, "所属分支"),
      h("th", {}, "幂等键"), h("th", {}, "时间"))),
    h("tbody", {}, ...effects.map((e) =>
      h("tr", { class: "clickable", onclick: () => location.hash = `#/runs/${e.run_id}` },
        h("td", {}, String(e.id)),
        h("td", { class: "mono small" }, e.root_run_id.slice(0, 8)),
        h("td", { class: "mono small" }, e.run_id.slice(0, 8)),
        h("td", { class: "keyval small" }, e.idem_key.slice(0, 24) + "…"),
        h("td", { class: "small muted" }, fmtMs(e.created_at))))));
  view.appendChild(h("div", { class: "panel" }, effects.length ? table : h("div", { class: "muted" }, "尚无已确认投递")));
}

// ----------------------------------------------------------------- 启动

connectSSE();
render();
