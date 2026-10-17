import { api, escape as e, toast, time } from "./api.js";
import * as views from "./views.js";

const state = {
  me: null,
  agents: [],
  sessions: [],
  requests: [],
  commands: [],
  logs: [],
  overview: {},
  detail: null,
  route: "overview",
  sessionId: null,
  sessionTab: "chat",
  sessionEvents: [],
  members: [],
  history: [],
  search: "",
  agentFilter: "",
  archived: false,
  drafts: new Map(),
  liveText: new Map(),
  compose: { mode: "default", effort: "medium", approval: "never", model: "" },
};
const main = document.querySelector("#main");
const modal = document.querySelector("#modal");
let renderedContext = null;
let stream = null,
  refreshTimer = null,
  refreshing = false,
  detailSequence = 0,
  actionBusy = false;

function showLogin() {
  stream?.close();
  stream = null;
  state.me = null;
  state.drafts.clear();
  state.liveText.clear();
  state.detail = null;
  state.history = [];
  state.sessions = [];
  state.requests = [];
  state.commands = [];
  state.logs = [];
  main.innerHTML = "";
  renderedContext = null;
  document.querySelector("#shell").hidden = true;
  document.querySelector("#login").hidden = false;
}
window.addEventListener("relay:unauthorized", showLogin);

function render(preserveInputs = true) {
  const context = `${state.route}/${state.sessionId || ""}/${state.sessionTab}`;
  const keep = preserveInputs && context === renderedContext;
  renderedContext = context;
  const focused = document.activeElement;
  const preserve = [
    ...(keep ? main.querySelectorAll("[data-preserve]") : []),
  ].map((el) => ({
    id: el.id,
    name: el.name,
    value: el.value,
    checked: el.checked,
    selector: el.id
      ? `#${CSS.escape(el.id)}`
      : `input[name="${CSS.escape(el.name)}"][value="${CSS.escape(el.value)}"]`,
  }));
  const focus = focused?.id,
    selection = focused?.selectionStart;
  const scrolls = [
    ...(keep ? main.querySelectorAll("[data-scroll-key]") : []),
  ].map((el) => ({
    key: el.dataset.scrollKey,
    top: el.scrollTop,
    bottom: el.scrollHeight - el.scrollTop - el.clientHeight < 80,
  }));
  const titles = {
    overview: "总览",
    agents: "执行端",
    sessions: "会话",
    requests: "人工待办",
    commands: "操作记录",
    logs: "实时日志",
    guide: "测试指南",
  };
  document.querySelector("#page-name").textContent =
    titles[state.route] || "总览";
  document
    .querySelectorAll("[data-nav]")
    .forEach((el) =>
      el.classList.toggle("active", el.dataset.nav === state.route),
    );
  document.querySelector("#agent-count").textContent = state.agents.length;
  document.querySelector("#request-count").textContent = state.requests.length;
  const renderer = state.sessionId
    ? views.session
    : views[state.route] || views.overview;
  main.innerHTML = renderer(state);
  for (const p of preserve) {
    const el = main.querySelector(p.selector);
    if (el) {
      el.value = p.value;
      if (el.type === "radio") el.checked = p.checked;
    }
  }
  for (const s of scrolls) {
    const el = main.querySelector(`[data-scroll-key="${s.key}"]`);
    if (el)
      el.scrollTop =
        s.bottom && s.key === "conversation" ? el.scrollHeight : s.top;
  }
  if (state.sessionId && !scrolls.some((s) => s.key === "conversation")) {
    const conversation = document.querySelector("#conversation");
    if (conversation) conversation.scrollTop = conversation.scrollHeight;
  }
  if (keep && focus) {
    const el = document.getElementById(focus);
    if (el && el.closest("main")) {
      el.focus({ preventScroll: true });
      if (typeof selection === "number" && el.setSelectionRange)
        el.setSelectionRange(selection, selection);
    }
  }
}

async function loadSession({ history = false } = {}) {
  if (!state.sessionId) return;
  const id = state.sessionId,
    sequence = ++detailSequence;
  const detail = await api(`/v1/sessions/${id}`);
  if (state.sessionId !== id || sequence !== detailSequence) return;
  state.detail = detail;
  if (state.me.role === "admin" || detail.owner === state.me.id) {
    const members = await api(`/v1/sessions/${id}/members`);
    if (state.sessionId === id) state.members = members;
  } else state.members = [];
  const response = await fetch(`/v1/events?session_id=${id}&follow=false`, {
    credentials: "same-origin",
  });
  if (response.ok) {
    const text = await response.text();
    const events = [];
    for (const block of text.split("\n\n")) {
      const line = block.split("\n").find((l) => l.startsWith("data: "));
      if (line) {
        try {
          events.push(JSON.parse(line.slice(6)));
        } catch {}
      }
    }
    if (state.sessionId === id) state.sessionEvents = events;
  }
  if (history && detail.provider_thread_id) {
    try {
      const result = await api(`/v1/sessions/${id}/history`);
      if (state.sessionId === id) state.history = result.data || [];
    } catch (error) {
      toast(error.message, true);
    }
  }
}

async function refresh({ history = false } = {}) {
  if (refreshing || !state.me) return;
  refreshing = true;
  try {
    const [agents, sessions, requests, commands, overview, logs] =
      await Promise.all([
        api("/v1/agents"),
        api(`/v1/sessions?archived=${state.archived}`),
        api("/v1/requests"),
        api("/v1/commands"),
        api("/v1/overview"),
        api("/v1/logs?limit=500"),
      ]);
    Object.assign(state, {
      agents,
      sessions,
      requests,
      commands,
      overview,
      logs,
    });
    if (state.sessionId) await loadSession({ history });
    render();
  } finally {
    refreshing = false;
  }
}

function scheduleRefresh(history = false) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(
    () => refresh({ history }).catch((error) => toast(error.message, true)),
    350,
  );
}

function subscribe() {
  stream?.close();
  stream = new EventSource("/v1/events");
  const indicator = document.querySelector("#stream-state");
  stream.onopen = () => {
    indicator.className = "connection connected";
    indicator.innerHTML = "<i></i>实时连接";
  };
  stream.onerror = () => {
    indicator.className = "connection error";
    indicator.innerHTML = "<i></i>正在重连";
  };
  stream.addEventListener("relay", (message) => {
    const event = JSON.parse(message.data),
      envelope = event.data;
    state.logs = [
      ...state.logs.filter((item) => item.id !== event.id),
      event,
    ].slice(-500);
    if (state.sessionId === event.session_id) {
      state.sessionEvents = [
        ...state.sessionEvents.filter((item) => item.id !== event.id),
        event,
      ].slice(-500);
    }
    if (event.type === "item/agentMessage/delta") {
      const payload = envelope.data || {};
      const runId = envelope.run_id || payload.turnId || "current";
      const draft = state.liveText.get(runId) || {
        session: event.session_id,
        text: "",
      };
      draft.text += payload.delta || "";
      state.liveText.set(runId, draft);
      if (state.sessionId === event.session_id && state.sessionTab === "chat")
        render();
    }
    if (event.type === "run.finished") {
      const providerRunId = envelope.data?.id;
      if (providerRunId) state.liveText.delete(providerRunId);
      scheduleRefresh(true);
    } else if (!event.type.endsWith("/delta")) scheduleRefresh();
    if (state.route === "logs") render();
    if (state.sessionId === event.session_id && state.sessionTab === "events")
      render();
  });
}

async function enter() {
  state.me = await api("/auth/me");
  document.querySelector("#login").hidden = true;
  document.querySelector("#shell").hidden = false;
  document.querySelector("#principal-name").textContent = state.me.id;
  document.querySelector("#principal-role").textContent = {
    admin: "管理员",
    operator: "操作者",
    viewer: "只读访问",
  }[state.me.role];
  await navigate();
  subscribe();
}

async function navigate() {
  const [route = "overview", id] = location.hash.slice(1).split("/");
  state.route = route || "overview";
  if (id !== state.sessionId) {
    state.detail = null;
    state.history = [];
    state.sessionEvents = [];
    state.sessionTab = "chat";
  }
  state.sessionId = state.route === "sessions" && id ? id : null;
  render();
  await refresh({ history: !!state.sessionId });
}
window.addEventListener("hashchange", () =>
  navigate().catch((error) => toast(error.message, true)),
);

function openModal(title, description, body, submitLabel, onSubmit) {
  modal.querySelector("#modal-content").innerHTML =
    `<div class="modal-header"><h2>${e(title)}</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">×</button></div><form id="modal-form" class="modal-body"><p>${e(description)}</p>${body}<p id="modal-error" class="error-text" role="alert"></p><div class="modal-footer"><button type="button" class="button" data-action="close-modal">取消</button><button type="submit" class="button primary">${e(submitLabel)}</button></div></form>`;
  modal.showModal();
  modal.querySelector("form").onsubmit = async (event) => {
    event.preventDefault();
    const submit = event.submitter;
    submit.disabled = true;
    try {
      await onSubmit(new FormData(event.target));
      modal.close();
      await refresh();
    } catch (error) {
      document.querySelector("#modal-error").textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  };
}
const field = (
  label,
  name,
  placeholder = "",
  type = "text",
  value = "",
  required = true,
) =>
  `<label class="form-field"><span>${e(label)}</span><input name="${name}" type="${type}" value="${e(value)}" placeholder="${e(placeholder)}" ${required ? "required" : ""} autocomplete="${type === "password" ? "new-password" : "off"}"></label>`;

function newAgent() {
  openModal(
    "连接执行端",
    "中枢将代理访问执行端，服务凭证不会发送给其他用户。",
    field("名称", "name", "例如：研发云端 Codex") +
      field("CodexServer 地址", "base_url", "http://127.0.0.1:8000", "url") +
      field(
        "服务 API_TOKEN",
        "token",
        "执行端 .env 中的 API_TOKEN",
        "password",
      ),
    "连接并检查",
    async (data) => {
      await api("/v1/agents", {
        method: "POST",
        body: Object.fromEntries(data),
      });
      toast("执行端已登记");
    },
  );
}
function newSession(agentId, example) {
  const agents = state.agents.filter((a) => a.enabled);
  if (!agents.length) {
    toast("请先连接一个允许新分派的执行端");
    return;
  }
  openModal(
    "新建会话",
    "在所选执行端创建独立上下文。工作目录留空时，使用执行端配置的默认目录；未配置则创建独立目录。",
    `<label class="form-field"><span>执行端</span><select name="agent_id">${agents.map((a) => `<option value="${a.id}" ${a.id === agentId ? "selected" : ""}>${e(a.name)}</option>`).join("")}</select></label>` +
      field(
        "会话名称",
        "name",
        "例如：接口兼容性修改",
        "text",
        example ? "交互测试" : "",
      ) +
      field(
        "工作目录（可选）",
        "cwd",
        "执行端允许范围内的现有目录",
        "text",
        "",
        false,
      ),
    "创建会话",
    async (data) => {
      const body = { name: data.get("name") };
      if (data.get("cwd")) body.cwd = data.get("cwd");
      const command = await api(`/v1/agents/${data.get("agent_id")}/sessions`, {
        method: "POST",
        body,
      });
      location.hash = `sessions/${command.session_id}`;
      if (example) applyExample(example, command.session_id);
      toast("创建操作已记录，正在准备会话");
    },
  );
}
function applyExample(name, sessionId = state.sessionId) {
  const example = views.examples[name];
  state.compose = {
    ...state.compose,
    mode: example.mode,
    approval: example.approval,
  };
  state.drafts.set(sessionId, example.prompt);
  state.sessionTab = "chat";
  render(false);
  document.querySelector("#prompt")?.focus();
}
async function submitReply(row, result) {
  await api(`/v1/requests/${row.id}/response`, {
    method: "POST",
    body: { result },
  });
  toast("回复已排队，稍后可查看执行端确认");
  await refresh();
}
function findRequest(id) {
  return (
    state.requests.find((r) => r.id === id) ||
    state.detail?.requests.find((r) => r.id === id)
  );
}

const actions = {
  refresh: async () => {
    await refresh({ history: !!state.sessionId });
    toast("日志已刷新");
  },
  "new-agent": newAgent,
  "new-session": (el) => newSession(el.dataset.agent),
  "close-modal": () => modal.close(),
  "session-tab": (el) => {
    state.sessionTab = el.dataset.tab;
    render();
  },
  "toggle-archived": async () => {
    state.archived = !state.archived;
    await refresh();
  },
  probe: async (el) => {
    const agent = await api(`/v1/agents/${el.dataset.id}/probe`, {
      method: "POST",
    });
    toast(
      agent.connection === "online" ? "连接检查通过" : agent.error,
      agent.connection !== "online",
    );
    await refresh();
  },
  "edit-agent": (el) => {
    const agent = state.agents.find((a) => a.id === el.dataset.id);
    openModal(
      "执行端设置",
      "禁止新分派不会中断已有运行；已有会话仍可接收控制和人工回复。",
      field("名称", "name", "", "text", agent.name) +
        `<label class="form-field"><span>接收新工作</span><select name="enabled"><option value="true" ${agent.enabled ? "selected" : ""}>允许新分派</option><option value="false" ${!agent.enabled ? "selected" : ""}>禁止新分派</option></select></label>` +
        field(
          "更新服务凭证（留空保持原值）",
          "token",
          "",
          "password",
          "",
          false,
        ),
      "保存设置",
      async (data) => {
        const body = {
          name: data.get("name"),
          enabled: data.get("enabled") === "true",
        };
        if (data.get("token")) body.token = data.get("token");
        await api(`/v1/agents/${agent.id}`, { method: "PATCH", body });
        toast("设置已保存");
      },
    );
  },
  "import-session": async (el) => {
    const agentId = el.dataset.agent;
    const result = await api(`/v1/agents/${agentId}/remote-sessions`);
    const sessions = [...(result.drafts || []), ...(result.data || [])];
    if (!sessions.length) {
      toast("执行端尚无可导入会话");
      return;
    }
    openModal(
      "导入已有会话",
      "建立中枢索引，不重新启动或修改原生会话。当前展示执行端最近一页会话。",
      `<label class="form-field"><span>选择会话</span><select name="thread_id">${sessions.map((s) => `<option value="${e(s.id)}">${e(s.name || s.preview || s.id)}</option>`).join("")}</select></label>`,
      "导入",
      async (data) => {
        const session = await api(`/v1/agents/${agentId}/import`, {
          method: "POST",
          body: Object.fromEntries(data),
        });
        location.hash = `sessions/${session.id}`;
        toast("会话已导入");
      },
    );
  },
  interrupt: async (el) => {
    await api(`/v1/runs/${el.dataset.id}/interrupt`, { method: "POST" });
    toast("中断请求已提交，等待执行端确认");
    await refresh();
  },
  archive: async (el) => {
    await api(`/v1/sessions/${el.dataset.id}/archive`, { method: "POST" });
    toast("归档操作已提交");
    location.hash = "sessions";
    await refresh();
  },
  "revoke-member": async (el) => {
    await api(
      `/v1/sessions/${state.sessionId}/members/${encodeURIComponent(el.dataset.principal)}`,
      { method: "DELETE" },
    );
    toast("会话授权已撤销");
    await refresh();
  },
  reconcile: async (el) => {
    const row = await api(`/v1/commands/${el.dataset.id}/reconcile`, {
      method: "POST",
    });
    toast(
      row.status === "unknown"
        ? "仍未确认远端结果，未重发操作"
        : "已核对原操作回执",
    );
    await refresh();
  },
  example: (el) => {
    const name = el.dataset.example;
    if (state.sessionId) applyExample(name);
    else newSession(null, name);
  },
  answer: async (el) => {
    const row = findRequest(el.dataset.id);
    let result;
    if (row.method === "item/tool/requestUserInput") {
      const answers = {};
      (row.params.questions || []).forEach((q, index) => {
        const custom = document
          .getElementById(`answer-${row.id}-${index}`)
          ?.value.trim();
        const selected = document.querySelector(
          `input[name="q-${row.id}-${index}"]:checked`,
        )?.value;
        if (!custom && !selected) throw new Error("请回答所有问题后提交");
        answers[q.id] = { answers: [custom || selected] };
      });
      result = { answers };
    } else result = JSON.parse(document.getElementById(`raw-${row.id}`).value);
    await submitReply(row, result);
  },
  decision: async (el) => {
    const row = findRequest(el.dataset.id);
    const decisions = row.params.availableDecisions || [
      "accept",
      "decline",
      "cancel",
    ];
    await submitReply(row, {
      decision: decisions[Number(el.dataset.decision)],
    });
  },
};

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-action]");
  if (!el || el.disabled) return;
  const action = actions[el.dataset.action];
  if (!action) return;
  if (actionBusy) return;
  actionBusy = true;
  try {
    await action(el);
  } catch (error) {
    toast(error.message, true);
  } finally {
    actionBusy = false;
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "prompt")
    state.drafts.set(state.sessionId, event.target.value);
  if (event.target.id === "session-search") {
    state.search = event.target.value;
    render();
  }
});
document.addEventListener("change", (event) => {
  const id = event.target.id;
  if (id === "agent-filter") {
    state.agentFilter = event.target.value;
    render();
  }
  const map = {
    "compose-mode": "mode",
    "compose-effort": "effort",
    "compose-approval": "approval",
    "compose-model": "model",
  };
  if (map[id]) state.compose[map[id]] = event.target.value;
});
document.addEventListener("keydown", (event) => {
  if (
    event.target.id === "prompt" &&
    event.key === "Enter" &&
    (event.metaKey || event.ctrlKey)
  ) {
    event.preventDefault();
    document.querySelector("#composer")?.requestSubmit();
  }
});
document.addEventListener("submit", async (event) => {
  if (event.target.id === "composer") {
    event.preventDefault();
    if (actionBusy) return;
    actionBusy = true;
    try {
      const prompt = document.querySelector("#prompt").value.trim();
      if (!prompt) return;
      const active = state.detail.runs.findLast((r) =>
        ["running", "queued"].includes(r.status),
      );
      if (active) {
        await api(`/v1/runs/${active.id}/input`, {
          method: "POST",
          body: { prompt },
        });
      } else {
        const options = {
          collaboration_mode: state.compose.mode,
          reasoning_effort: state.compose.effort,
          approval_policy: state.compose.approval,
        };
        if (state.compose.model.trim())
          options.model = state.compose.model.trim();
        await api(`/v1/sessions/${state.sessionId}/messages`, {
          method: "POST",
          body: { prompt, options },
        });
      }
      state.drafts.delete(state.sessionId);
      document.querySelector("#prompt").value = "";
      toast(active ? "追加输入已提交" : "消息已提交");
      await refresh();
    } catch (error) {
      toast(error.message, true);
    } finally {
      actionBusy = false;
    }
  }
  if (event.target.id === "share-form") {
    event.preventDefault();
    try {
      await api(`/v1/sessions/${state.sessionId}/members`, {
        method: "PUT",
        body: Object.fromEntries(new FormData(event.target)),
      });
      toast("会话访问权限已更新");
      await refresh();
    } catch (error) {
      toast(error.message, true);
    }
  }
});
document.querySelector("#login-form").onsubmit = async (event) => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  try {
    await api("/auth/login", {
      method: "POST",
      body: { token: document.querySelector("#access-key").value },
    });
    document.querySelector("#access-key").value = "";
    await enter();
  } catch (error) {
    document.querySelector("#login-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
};
document.querySelector("#logout").onclick = async () => {
  try {
    await api("/auth/logout", { method: "POST" });
  } finally {
    showLogin();
  }
};
document.querySelector("#refresh").onclick = () =>
  refresh({ history: true })
    .then(() => toast("已刷新"))
    .catch((error) => toast(error.message, true));
setInterval(() => {
  if (state.me && !document.hidden) refresh().catch(() => {});
}, 5000);
enter().catch((error) => {
  if (error.status !== 401) toast(error.message, true);
  showLogin();
});
