import { escape as e, time, badge, empty, kind, formatted } from "./api.js";

const button = (action, label, extra = "", style = "") =>
  `<button class="button ${style}" data-action="${action}" ${extra}>${label}</button>`;
export const heading = (title, description, action = "") =>
  `<div class="page-heading"><div><p class="eyebrow">AGENTRELAY / WORKSPACE</p><h1>${e(title)}</h1><p>${e(description)}</p></div>${action}</div>`;
const agentName = (state, id) =>
  state.agents.find((a) => a.id === id)?.name || "执行端";
const sessionName = (state, id) =>
  state.sessions.find((s) => s.id === id)?.name || "会话";

const json = (value) => e(JSON.stringify(value ?? {}, null, 2));
const eventLabel = (type) =>
  ({
    "command.queued": "命令入队",
    "command.dispatching": "Worker 领取",
    "command.sending": "投递执行端",
    "command.accepted": "执行端已回执",
    "command.rejected": "执行端拒绝",
    "command.unknown": "回执待核对",
    "observer.connected": "观察连接建立",
    "observer.error": "观察连接异常",
    "observer.disconnected": "观察连接关闭",
    "run.queued": "运行排队",
    "run.running": "运行中",
    "run.finished": "运行结束",
  })[type] || type;

function sessionRow(state, session) {
  return `<a class="row" href="#sessions/${session.id}"><div class="row-icon">▤</div><div class="row-content"><div class="row-title">${e(session.name)}</div><div class="row-meta">${e(agentName(state, session.agent_id))}<span>·</span>${time(session.updated_at)}</div></div>${badge(session.state)}<span class="muted">↗</span></a>`;
}

export function overview(state) {
  const stats = state.overview;
  const metrics = [
    ["执行端", stats.agents, "◇", `${stats.online_agents || 0} 个在线`],
    ["会话", stats.sessions, "▤", "跨轮次持续工作"],
    ["进行中的运行", stats.running, "↗", "执行端报告的最近状态"],
    ["等待人工处理", stats.pending_requests, "◎", "方案选择与执行授权"],
  ];
  return (
    heading(
      "让每一段工作，保持连接。",
      "查看执行端、跟进会话，并在需要时介入。",
      button("new-session", "＋ 新建会话", "", "primary"),
    ) +
    `<div class="stats-grid">${metrics.map(([title, value, icon, footer]) => `<div class="stat-card"><div class="stat-label">${title}<span>${icon}</span></div><div class="stat-value">${value ?? 0}</div><div class="stat-footer">${footer}</div></div>`).join("")}</div>` +
    `<section class="diagnostic-strip"><div><span class="diagnostic-kicker">链路诊断</span><strong>${stats.online_agents || 0}/${stats.agents || 0} 个执行端在线</strong><p>${stats.unknown_commands || 0} 个回执待核对 · ${state.logs.filter((item) => item.type === "observer.error" || item.type === "command.rejected").length} 个最近连接/投递错误</p></div><a class="button small" href="#logs">查看实时链路</a></section>` +
    `<div class="overview-grid"><section class="panel"><div class="panel-heading"><h2>最近会话</h2><a href="#sessions" class="text-link">查看全部 ↗</a></div>${
      state.sessions.length
        ? state.sessions
            .slice(0, 5)
            .map((s) => sessionRow(state, s))
            .join("")
        : empty(
            "从一段会话开始",
            "选择一个已连接的执行端，向 Agent 发送第一条指令。",
            button("new-session", "新建会话", "", "mint"),
          )
    }</section>` +
    `<section class="panel"><div class="panel-heading"><h2>执行端状态</h2><a href="#agents" class="text-link">管理执行端 ↗</a></div>${
      state.agents.length
        ? state.agents
            .slice(0, 5)
            .map(
              (a) =>
                `<div class="row"><div class="row-icon">◇</div><div class="row-content"><div class="row-title">${e(a.name)}</div><div class="row-meta">${e(a.base_url)}</div></div>${badge(a.connection)}</div>`,
            )
            .join("")
        : empty(
            "尚未连接执行端",
            "添加 CodexServer 地址与服务凭证，即可开始。",
            button("new-agent", "连接执行端", "", "mint"),
          )
    }</section></div>` +
    (stats.unknown_commands
      ? `<div class="alert" style="margin-top:20px">有 ${stats.unknown_commands} 项操作结果待核对。系统不会自动重发这些操作。 <a href="#commands" class="text-link">查看操作记录 →</a></div>`
      : "") +
    `<section class="panel recent-events"><div class="panel-heading"><h2>最近链路事件</h2><a href="#logs" class="text-link">打开完整日志 ↗</a></div>${state.logs.length ? state.logs.slice(-6).toReversed().map((event) => `<div class="event-line"><span class="event-time">${time(event.created_at)}</span><span class="event-label">${e(eventLabel(event.type))}</span><span class="event-session">${e(sessionName(state, event.session_id))}</span><code>#${event.id}</code></div>`).join("") : `<div class="empty compact"><h3>暂无链路事件</h3><p>连接检查、命令投递和运行状态会在这里出现。</p></div>`}</section>` +
    `<div class="getting-started"><div><h3>从输入到人工交互，走一遍完整链路</h3><p>内置测试指令，体验流式输出、执行中追加、Plan 提问和命令审批。</p></div><a class="button" href="#guide">打开测试指南 →</a></div><p class="footer-note">执行状态来自远端观察。连接中断时保留最后事实，并明确显示待核对。</p>`
  );
}

export function agents(state) {
  return (
    heading(
      "执行端",
      "连接 Agent 服务，查看能力、运行容量与连接状态。",
      state.me.role === "admin"
        ? button("new-agent", "＋ 连接执行端", "", "primary")
        : "",
    ) +
    (state.agents.length
      ? `<div class="agents-grid">${state.agents
          .map((a) => {
            const capabilities = a.capabilities.capabilities || {};
            const names = {
              steering: "执行中追加",
              human_requests: "人工交互",
              thread_events: "会话事件",
              idempotency: "操作幂等",
            };
            const admin = state.me.role === "admin";
            return `<article class="agent-card"><div class="agent-card-top"><div class="agent-icon">↗</div><div><h3>${e(a.name)}</h3><div class="agent-url">${e(a.base_url)}</div></div>${badge(a.connection)}</div><div class="agent-facts"><span>实例 <code>${e(a.instance_id || "未上报")}</code></span><span>观察 <code>${e(time(a.observed_at))}</code></span></div><div class="agent-metrics"><div><small>正在运行</small><strong>${a.remote_status.running ?? "—"}</strong></div><div><small>排队</small><strong>${a.remote_status.queued ?? "—"}</strong></div><div><small>并行容量</small><strong>${a.remote_status.max_concurrent_runs ?? "—"}</strong></div></div><div class="capabilities">${Object.entries(
              names,
            )
              .filter(([key]) => capabilities[key])
              .map(([, label]) => `<span class="capability">✓ ${label}</span>`)
              .join(
                "",
              )}${!a.enabled ? '<span class="capability">已禁止新分派</span>' : ""}</div>${a.error ? `<div class="alert error">${e(a.error)}</div>` : ""}<div class="card-footer"><span class="muted">观察于 ${time(a.observed_at)}</span><div class="inline-actions">${admin ? button("probe", "检查", `data-id="${a.id}"`, "small") + button("edit-agent", "设置", `data-id="${a.id}"`, "small") : ""}</div></div><div class="inline-actions" style="margin-top:13px">${button("new-session", "新建会话", `data-agent="${a.id}" ${!a.enabled ? "disabled" : ""}`, "small mint")}${admin ? button("import-session", "导入会话", `data-agent="${a.id}"`, "small") : ""}</div></article>`;
          })
          .join("")}</div>`
      : empty(
          "尚未添加执行端",
          "连接一个已启动的 CodexServer。凭证加密保存在中枢服务端。",
          button("new-agent", "连接执行端", "", "primary"),
        ))
  );
}

export function sessions(state) {
  const search = state.search.toLowerCase();
  const list = state.sessions.filter(
    (s) =>
      (!search || s.name.toLowerCase().includes(search)) &&
      (!state.agentFilter || s.agent_id === state.agentFilter),
  );
  return (
    heading(
      "会话",
      "保存上下文，持续跟进同一段工作。",
      button("new-session", "＋ 新建会话", "", "primary"),
    ) +
    `<div class="toolbar"><input id="session-search" data-preserve placeholder="搜索会话名称…" value="${e(state.search)}"><select id="agent-filter" data-preserve><option value="">全部执行端</option>${state.agents.map((a) => `<option value="${a.id}" ${state.agentFilter === a.id ? "selected" : ""}>${e(a.name)}</option>`).join("")}</select><button class="filter-pill ${state.archived ? "active" : ""}" data-action="toggle-archived">${state.archived ? "查看未归档" : "查看已归档"}</button><span class="count">${list.length} 段会话</span></div>` +
    (list.length
      ? `<div class="panel table-wrap"><table class="table"><thead><tr><th>会话</th><th>执行端</th><th>状态</th><th>负责人</th><th>最近观察</th><th></th></tr></thead><tbody>${list.map((s) => `<tr><td><a class="title" href="#sessions/${s.id}">${e(s.name)}</a><div class="sub">${e(s.id.slice(0, 8))}</div></td><td>${e(agentName(state, s.agent_id))}</td><td>${badge(s.state)}</td><td>${e(s.owner)}</td><td class="muted">${time(s.observed_at)}</td><td><a href="#sessions/${s.id}" class="text-link">打开 ↗</a></td></tr>`).join("")}</tbody></table></div>`
      : empty(
          "没有匹配的会话",
          "创建新会话，或导入执行端已有的会话。",
          button("new-session", "新建会话", "", "mint"),
        ))
  );
}

export function commandTable(state, commands) {
  return commands.length
    ? `<div class="panel table-wrap"><table class="table"><thead><tr><th>操作</th><th>会话</th><th>状态</th><th>操作者</th><th>提交时间</th><th></th></tr></thead><tbody>${commands.map((c) => `<tr><td><span class="title">${e(kind(c.kind))}</span><div class="sub">${e(c.id.slice(0, 10))}</div></td><td><a href="#sessions/${c.session_id}">${e(sessionName(state, c.session_id))}</a></td><td>${badge(c.status)}${c.error ? `<div class="command-error">${e(c.error)}</div>` : ""}</td><td>${e(c.actor)}</td><td class="muted">${time(c.created_at)}</td><td><details class="inline-details"><summary>详情</summary><div class="command-detail"><div><b>请求</b><pre>${json(c.payload)}</pre></div><div><b>回执</b><pre>${json(c.result || (c.error ? { error: c.error } : null))}</pre></div></div></details>${c.status === "unknown" ? button("reconcile", "核对", `data-id="${c.id}"`, "small") : ""}</td></tr>`).join("")}</tbody></table></div>`
    : empty("暂无操作记录", "新建会话、发送消息和控制操作都会在这里留下回执。");
}
export function commands(state) {
  return (
    heading("操作记录", "沿着命令 ID 查看入队、投递、回执和最终结果。") +
    `<div class="diagnostic-strip compact-strip"><div><span class="diagnostic-kicker">调试提示</span><strong>已受理 ≠ 已完成</strong><p>unknown 表示平台无法确认远端结果，禁止自动重发，请先核对原始回执。</p></div></div>` +
    commandTable(state, state.commands)
  );
}

const logLabels = {
  "command.queued": "命令已入队",
  "command.dispatching": "Worker 已领取",
  "command.sending": "正在投递执行端",
  "command.accepted": "执行端已回执",
  "command.rejected": "执行端拒绝",
  "command.unknown": "结果待核对",
  "command.reconcile.started": "开始核对回执",
  "command.reconcile.pending": "回执仍未完成",
  "observer.connected": "观察连接建立",
  "observer.error": "观察连接异常",
  "observer.disconnected": "观察连接关闭",
  "run.queued": "运行排队",
  "run.running": "运行中",
  "run.finished": "运行完成",
};

function logRow(state, event) {
  const payload = event.data || {};
  return `<details class="log-row" open><summary><time>${time(event.created_at)}</time><span class="log-type">${e(logLabels[event.type] || event.type)}</span><span class="log-session"><a href="#sessions/${event.session_id}">${e(sessionName(state, event.session_id))}</a></span><span class="muted">#${event.id}</span></summary><div class="log-meta"><span>事件类型：<code>${e(event.type)}</code></span>${event.actor ? `<span>操作者：<code>${e(event.actor)}</code></span>` : ""}</div><pre>${e(JSON.stringify(payload, null, 2))}</pre></details>`;
}

export function logs(state) {
  const search = state.logSearch.toLowerCase();
  const types = [...new Set(state.logs.map((event) => event.type))].sort();
  const filtered = state.logs.filter((event) => {
    const haystack = `${event.type} ${event.actor || ""} ${JSON.stringify(event.data || {})}`.toLowerCase();
    return (!state.logType || event.type === state.logType) &&
      (!search || haystack.includes(search));
  });
  return (
    heading(
      "实时日志",
      "查看中枢受理、Worker 投递、执行端回执和事件观察的完整链路。",
      `<span class="connection connected"><i></i>自动跟随实时事件</span>`,
    ) +
    `<div class="log-toolbar"><input id="log-search" data-preserve placeholder="搜索事件、操作者或字段…" value="${e(state.logSearch)}"><select id="log-type" data-preserve><option value="">全部事件类型</option>${types.map((type) => `<option value="${e(type)}" ${state.logType === type ? "selected" : ""}>${e(type)}</option>`).join("")}</select><span class="count">显示 ${filtered.length}/${state.logs.length} 条 · 最多保留 500 条</span><button class="button small" data-action="refresh">↻ 刷新</button></div>` +
    `<section class="panel log-panel" data-scroll-key="logs">${
      filtered.length
        ? filtered
            .toReversed()
            .map((event) => logRow(state, event))
            .join("")
        : empty(
            "暂无实时日志",
            "发送消息或执行控制操作后，链路日志会显示在这里。",
          )
    }</section>`
  );
}

const requestTitles = {
  "item/tool/requestUserInput": "Agent 需要你的选择",
  "item/commandExecution/requestApproval": "命令执行需要授权",
  "item/fileChange/requestApproval": "文件变更需要授权",
  "item/permissions/requestApproval": "权限申请",
  "mcpServer/elicitation/request": "外部工具需要补充信息",
};
export function requestCard(row, state) {
  const p = row.params,
    isQuestion = row.method === "item/tool/requestUserInput",
    isApproval =
      row.method.includes("Execution/requestApproval") ||
      row.method.includes("fileChange/requestApproval");
  const disabled = row.status !== "pending" || state.me.role === "viewer";
  let body = "";
  if (isQuestion) {
    body = (p.questions || [])
      .map(
        (q, index) =>
          `<div class="question"><h3>${e(q.question)}</h3>${(q.options || []).map((o, j) => `<label class="option"><input type="radio" name="q-${row.id}-${index}" value="${e(o.label)}" ${disabled ? "disabled" : ""} data-preserve><span><strong>${e(o.label)}</strong><br><span class="muted">${e(o.description)}</span></span></label>`).join("")}<input id="answer-${row.id}-${index}" data-preserve placeholder="或输入自定义回答" style="width:100%;margin-top:7px" ${disabled ? "disabled" : ""}></div>`,
      )
      .join("");
  } else if (isApproval) {
    body = `${p.reason ? `<p>${e(p.reason)}</p>` : ""}<div class="request-code">${e(p.command || JSON.stringify(p.changes || p, null, 2))}</div><p class="field-hint">授权范围以本次请求为准。选择结果会发送至原执行端。</p>`;
  } else {
    body = `<div class="request-code">${e(JSON.stringify(p, null, 2))}</div><label class="form-field"><span>协议响应 JSON</span><textarea id="raw-${row.id}" data-preserve ${disabled ? "disabled" : ""}>${row.method === "item/permissions/requestApproval" ? "{}" : '{"action":"decline","content":null}'}</textarea></label>`;
  }
  let actions = "";
  if (!disabled) {
    if (isQuestion)
      actions = button(
        "answer",
        "提交回答",
        `data-id="${row.id}"`,
        "primary small",
      );
    else if (isApproval) {
      const decisions = p.availableDecisions || ["accept", "decline", "cancel"];
      actions = decisions
        .map((d, i) =>
          button(
            "decision",
            typeof d === "string"
              ? {
                  accept: "允许本次",
                  acceptForSession: "允许此会话",
                  decline: "拒绝",
                  cancel: "取消执行",
                }[d] || d
              : "允许并保存规则",
            `data-id="${row.id}" data-decision="${i}"`,
            typeof d === "string" && d === "accept" ? "primary small" : "small",
          ),
        )
        .join("");
    } else
      actions = button(
        "answer",
        "提交响应",
        `data-id="${row.id}"`,
        "primary small",
      );
  }
  return `<article class="request-card" data-request="${row.id}"><div class="request-head"><span class="row-icon">◎</span><h3>${requestTitles[row.method] || e(row.method)}</h3>${badge(row.status)}</div>${body}<div class="request-footer"><span class="muted">${time(row.created_at)}</span><div class="inline-actions">${actions}</div></div>${row.reason ? `<p class="muted">${e(row.reason)}</p>` : ""}<details><summary>请求来源与协议</summary><pre class="request-code">${e(JSON.stringify({ request_id: row.id, method: row.method, provider_request_id: row.provider_request_id }, null, 2))}</pre></details></article>`;
}
export function requests(state) {
  return (
    heading("人工待办", "在原始执行仍有效时回答问题或作出授权。") +
    `<div class="requests-layout">${state.requests.length ? state.requests.map((r) => `<div class="request-session"><a href="#sessions/${r.session_id}">${e(sessionName(state, r.session_id))} ↗</a></div>${requestCard(r, state)}`).join("") : empty("当前没有需要处理的请求", "Agent 发起提问或审批后，会在这里出现。可以从测试指南体验完整交互。", '<a class="button mint" href="#guide">打开测试指南 →</a>')}</div>`
  );
}

function transcript(state, detail) {
  const messages = [];
  const seenTurns = new Set();
  // Native history is authoritative for item ordering, including steering messages.
  for (const turn of state.history || []) {
    seenTurns.add(turn.id);
    for (const item of turn.items || []) {
      if (item.type === "userMessage") {
        const text = (item.content || [])
          .filter((p) => p.type === "text")
          .map((p) => p.text)
          .join("\n");
        if (text) messages.push({ role: "user", text, key: item.id });
      }
      if (item.type === "agentMessage" && item.text)
        messages.push({ role: "assistant", text: item.text, key: item.id });
    }
  }
  const subsequent = [];
  for (const c of detail.commands) {
    if (c.kind !== "start" && c.kind !== "steer") continue;
    const run =
      c.kind === "start"
        ? detail.runs.find((r) => r.provider_run_id === c.result?.id)
        : detail.runs.find((r) => r.id === c.payload.run_id);
    if (run && seenTurns.has(run.provider_turn_id)) continue;
    subsequent.push({
      role: "user",
      text: c.payload.prompt,
      date: c.created_at,
      key: c.id,
      status: c.status,
    });
  }
  for (const run of detail.runs) {
    if (run.final_response && !seenTurns.has(run.provider_turn_id))
      subsequent.push({
        role: "assistant",
        text: run.final_response,
        date: run.updated_at,
        key: run.id,
      });
  }
  subsequent.sort((a, b) =>
    String(a.date || "").localeCompare(String(b.date || "")),
  );
  messages.push(...subsequent);
  for (const [runId, draft] of state.liveText) {
    if (
      draft.session === detail.id &&
      !detail.runs.some(
        (r) => r.provider_run_id === runId && r.final_response,
      ) &&
      draft.text
    )
      messages.push({
        role: "assistant",
        text: draft.text,
        date: null,
        key: runId,
        live: true,
      });
  }
  return messages.length
    ? messages
        .map(
          (m) =>
            `<article class="message ${m.role}"><div class="message-avatar">${m.role === "user" ? "U" : "↗"}</div><div class="message-content"><div class="message-meta"><strong>${m.role === "user" ? "你" : "Agent"}</strong>${m.date ? `<time>${time(m.date)}</time>` : ""}${m.status && m.status !== "accepted" ? badge(m.status) : ""}${m.live ? '<span class="muted">正在输出</span>' : ""}</div><div class="message-body">${formatted(m.text)}</div></div></article>`,
        )
        .join("")
    : empty("会话已准备好", "发送第一条指令，或使用测试指南里的示例。");
}

export function session(state) {
  const detail = state.detail;
  if (!detail) return '<div class="loading">正在读取会话…</div>';
  const active = detail.runs.findLast((r) =>
    ["running", "queued"].includes(r.status),
  );
  const pending = detail.requests.filter((r) => r.status === "pending");
  const readOnly =
    state.me.role === "viewer" || detail.archived || !detail.provider_thread_id;
  const list = `<aside class="session-list" data-scroll-key="session-list"><div class="session-list-header">会话 <button class="text-link" data-action="new-session">＋</button></div>${state.sessions.map((s) => `<a href="#sessions/${s.id}" class="session-choice ${s.id === detail.id ? "active" : ""}"><h3>${e(s.name)}</h3><small>${badge(s.state)}<span style="float:right">${time(s.updated_at).split(" ")[0]}</span></small></a>`).join("")}</aside>`;
  let body = "";
  if (state.sessionTab === "chat") {
    const cfg = state.compose;
    body = `<div id="conversation" class="conversation" data-scroll-key="conversation">${transcript(state, detail)}${active ? '<div class="typing">Agent 正在工作</div>' : ""}</div><form id="composer" class="composer"><div class="composer-options"><select id="compose-mode" aria-label="执行模式" ${active ? "disabled" : ""}><option value="default" ${cfg.mode === "default" ? "selected" : ""}>Default · 执行</option><option value="plan" ${cfg.mode === "plan" ? "selected" : ""}>Plan · 分析与提问</option></select><select id="compose-effort" aria-label="推理强度" ${active ? "disabled" : ""}>${["medium", "high", "xhigh", "low"].map((v) => `<option ${cfg.effort === v ? "selected" : ""}>${v}</option>`).join("")}</select><select id="compose-approval" aria-label="审批策略" ${active ? "disabled" : ""}>${[
      ["never", "无需交互审批"],
      ["on-request", "按需审批"],
      ["untrusted", "不可信命令审批"],
    ]
      .map(
        ([v, label]) =>
          `<option value="${v}" ${cfg.approval === v ? "selected" : ""}>${label}</option>`,
      )
      .join(
        "",
      )}</select><input id="compose-model" value="${e(cfg.model)}" placeholder="模型：使用执行端默认值" aria-label="模型" ${active ? "disabled" : ""}></div><div class="composer-box"><textarea id="prompt" data-preserve placeholder="${active ? "补充当前这轮工作的要求…" : "告诉 Agent 要做什么…"}" ${readOnly ? "disabled" : ""} required>${e(state.drafts.get(detail.id) || "")}</textarea><button type="submit" class="send-button" aria-label="${active ? "追加输入" : "发送消息"}" ${readOnly ? "disabled" : ""}>↑</button></div><div class="composer-hint"><span>${active ? "追加至当前轮次，不创建新运行" : "开始新一轮，沿用当前会话上下文"} · ⌘ / Ctrl + Enter 发送</span><span>HTTP + SSE</span></div></form>`;
  } else if (state.sessionTab === "requests")
    body = `<div class="detail-pane">${
      detail.requests.length
        ? detail.requests
            .toReversed()
            .map((r) => requestCard(r, state))
            .join("")
        : empty("暂无人工请求", "结构化提问需要 Plan 模式。")
    }</div>`;
  else if (state.sessionTab === "events")
    body = `<div class="detail-pane" data-scroll-key="event-list">${
      state.sessionEvents.length
        ? state.sessionEvents
            .toReversed()
            .map(
              (ev) =>
                `<details class="event-item"><summary><span>${e(ev.type)}</span><time>${time(ev.created_at)}</time></summary><pre>${e(JSON.stringify(ev.data, null, 2))}</pre></details>`,
            )
            .join("")
        : empty("暂无事件", "操作会在此留下可追溯的记录。")
    }</div>`;
  else if (state.sessionTab === "runs")
    body = `<div class="detail-pane">${
      detail.runs
        .toReversed()
        .map(
          (r) =>
            `<div class="request-card"><div class="request-head"><h3>运行 ${e(r.id.slice(0, 8))}</h3>${badge(r.status)}</div><p class="muted">${time(r.created_at)}</p>${r.error ? `<div class="alert error">${e(r.error)}</div>` : ""}<pre class="request-code">${e(JSON.stringify(r.options, null, 2))}</pre></div>`,
        )
        .join("") || empty("尚未开始运行", "发送一条消息开始。")
    }</div>`;
  else
    body = `<div class="detail-pane"><dl class="details-grid"><dt>执行端</dt><dd>${e(agentName(state, detail.agent_id))}</dd><dt>拥有者</dt><dd>${e(detail.owner)}</dd><dt>工作目录</dt><dd><code>${e(detail.cwd || "尚未准备")}</code></dd><dt>会话标识</dt><dd><code>${e(detail.id)}</code></dd><dt>原生 Thread</dt><dd><code>${e(detail.provider_thread_id)}</code></dd><dt>最近观察</dt><dd>${time(detail.observed_at)}</dd></dl>${state.me.role === "admin" || detail.owner === state.me.id ? `<div class="divider"></div><h3>共享会话</h3><p class="muted" style="font-size:11px;margin-top:7px">向已配置的身份授予查看或操作权限。</p><form id="share-form" class="share-row"><input name="principal" placeholder="身份 ID" required><select name="permission"><option value="view">仅查看</option><option value="operate">可操作</option></select><button class="button small" type="submit">授权</button></form>${(state.members || []).map((m) => `<div class="row"><div class="row-content"><strong>${e(m.principal)}</strong><p class="muted">${m.permission === "operate" ? "可操作" : "仅查看"}</p></div>${button("revoke-member", "撤销", `data-principal="${e(m.principal)}"`, "small danger")}</div>`).join("")}` : ""}</div>`;
  return `<div class="split-view">${list}<section class="session-main"><div class="session-header"><div><h2>${e(detail.name)}</h2><p class="muted">${e(agentName(state, detail.agent_id))} · ${e(detail.owner)}</p></div><div class="session-actions">${badge(detail.state)}${active ? button("interrupt", "■ 中断", `data-id="${active.id}"`, "danger small") : button("archive", "归档", `data-id="${detail.id}" ${readOnly ? "disabled" : ""}`, "small")}</div></div>${detail.error ? `<div class="alert" style="margin:12px 15px 0">${e(detail.error)}</div>` : ""}<div class="tabs">${[
    ["chat", "对话"],
    ["requests", `人工交互${pending.length ? " · " + pending.length : ""}`],
    ["runs", "运行"],
    ["events", "事件"],
    ["info", "详情"],
  ]
    .map(
      ([id, label]) =>
        `<button class="tab ${state.sessionTab === id ? "active" : ""}" data-action="session-tab" data-tab="${id}">${label}</button>`,
    )
    .join("")}</div>${body}</section></div>`;
}

export const examples = {
  chat: {
    prompt: "请记住标记 ORCHID-731，只回复“已记住”，不要调用工具。",
    mode: "default",
    approval: "never",
  },
  steer: {
    prompt: "不要调用工具。从 1 数到 10000，每个数字单独一行。",
    mode: "default",
    approval: "never",
  },
  question: {
    prompt:
      "请先调用 request_user_input 工具询问我：待办列表数据保存在内存还是本地文件？收到回答后再分析方案，不要修改文件。",
    mode: "plan",
    approval: "never",
  },
  approval: {
    prompt: "请使用终端执行 touch approval-demo.txt，不要改用文件编辑工具。",
    mode: "default",
    approval: "untrusted",
  },
};
export function guide(state) {
  const cards = [
    [
      "chat",
      "01 / 开始对话",
      "验证上下文延续",
      "发送示例后，在同一会话询问“刚才让你记住的标记是什么？”，验证上下文。",
    ],
    [
      "steer",
      "02 / 运行控制",
      "追加要求与中断",
      "在输出过程中发送“调整要求：数到 20 即可”。再次启动长输出，然后点击中断。",
    ],
    [
      "question",
      "03 / 人工交互",
      "让 Agent 先问你",
      "以 Plan 模式发送示例，在人工交互标签中回答问题，观察 Agent 继续工作。",
    ],
    [
      "approval",
      "04 / 执行授权",
      "审批一条真实命令",
      "在独立测试会话中发送。确认工作目录和命令后允许本次，或取消执行。",
    ],
  ];
  return (
    heading(
      "走一遍完整链路",
      "示例会调用已连接执行端的真实模型；点击填入后，由你确认发送。",
    ) +
    `<div class="guide"><div class="guide-grid">${cards.map(([id, step, title, description]) => `<article class="guide-card"><div class="step">${step}</div><h3>${title}</h3><p>${description}</p><pre>${e(examples[id].prompt)}</pre><button class="button mint" data-action="example" data-example="${id}">填入会话 →</button></article>`).join("")}</div><div class="alert" style="margin-top:20px">“操作已受理”与“本轮已完成”是两件事。关闭页面不会中断远端工作。结构化提问当前仅在 Plan 模式可用。</div><div class="guide-card"><h3>用接口进行同样的操作</h3><p style="margin-top:10px">中枢 API 位于 /v1，浏览器登录后可直接在文档中调试。命令接口返回操作 id 和回执，未知结果通过核对处理，不会自动重发。</p><a href="/api-docs" class="button small" target="_blank">打开 API 文档 ↗</a></div></div>`
  );
}
