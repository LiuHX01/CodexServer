export async function api(
  path,
  { method = "GET", body, key, ...options } = {},
) {
  const headers = new Headers(options.headers || {});
  if (body !== undefined) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD"].includes(method))
    headers.set("Idempotency-Key", key || crypto.randomUUID());
  const response = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new CustomEvent("relay:unauthorized"));
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail || "请求失败"),
    );
    error.status = response.status;
    throw error;
  }
  return data;
}
export const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        ch
      ],
  );
export const time = (value) =>
  value
    ? new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(new Date(value))
    : "尚未观察";
export function toast(message, error = false) {
  const el = document.querySelector("#toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 5000);
}
const statuses = {
  online: ["在线", "green"],
  unreachable: ["无法连接", "red"],
  unavailable: ["运行端未就绪", "amber"],
  unknown: ["待核对", "amber"],
  idle: ["空闲", ""],
  provisioning: ["准备中", "blue"],
  queued: ["排队中", "blue"],
  running: ["运行中", "green"],
  completed: ["本轮完成", ""],
  interrupted: ["已中断", "amber"],
  failed: ["本轮失败", "red"],
  pending: ["待回复", "amber"],
  responded: ["已投递回复", "blue"],
  resolved: ["已处理", "green"],
  expired: ["已失效", ""],
  dispatching: ["投递中", "blue"],
  accepted: ["已受理", "green"],
  rejected: ["已拒绝", "red"],
};
export function badge(status) {
  const [label, color] = statuses[status] || [status || "未知", ""];
  return `<span class="badge ${color}"><i class="status-dot ${status === "online" || status === "running" ? "online" : ""}"></i>${escape(label)}</span>`;
}
export const kind = (value) =>
  ({
    create_session: "创建会话",
    start: "开始运行",
    steer: "追加输入",
    interrupt: "中断运行",
    reply: "回复人工请求",
    archive: "归档会话",
  })[value] || value;
export function formatted(text) {
  return String(text || "")
    .split(/```/)
    .map((part, index) =>
      index % 2
        ? `<pre>${escape(part.replace(/^\w*\n/, ""))}</pre>`
        : escape(part),
    )
    .join("");
}
export function empty(title, description, action = "") {
  return `<div class="empty"><div class="empty-symbol">◇</div><h3>${escape(title)}</h3><p>${escape(description)}</p>${action}</div>`;
}
