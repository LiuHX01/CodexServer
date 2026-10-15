(() => {
  "use strict";
  const config = JSON.parse(document.getElementById("docs-config").textContent);
  const search = document.getElementById("endpoint-search");
  const empty = document.getElementById("empty-state");
  let toastTimer;

  document.getElementById("model-label").textContent = config.model;
  document.getElementById("effort-label").textContent = config.effort;

  const baseUrl = location.origin + config.rootPath;
  const example = [
    "curl -N -X POST \\",
    `  '${baseUrl}/v1/threads/THREAD_ID/messages' \\`,
    "  -H 'Authorization: Bearer TOKEN' \\",
    "  -H 'Content-Type: application/json' \\",
    "  -d '{\"prompt\":\"你好\",\"stream\":true}'",
  ].join("\n");
  document.getElementById("curl-example").textContent = example;

  function toast(message) {
    const element = document.getElementById("toast");
    clearTimeout(toastTimer);
    element.textContent = message;
    element.hidden = false;
    toastTimer = setTimeout(() => { element.hidden = true; }, 2400);
  }

  async function copyText(text) {
    if (window.isSecureContext && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_) { /* Fall back for browsers that deny clipboard permissions. */ }
    }
    const field = document.createElement("textarea");
    field.value = text;
    field.style.cssText = "position:fixed;left:-9999px;top:0;";
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    if (!copied) throw new Error("Clipboard unavailable");
  }

  document.getElementById("copy-example").addEventListener("click", async () => {
    try {
      await copyText(example);
      toast("请求示例已复制，请替换 THREAD_ID 和 TOKEN");
    } catch (_) {
      toast("复制未成功，请直接选中示例文字复制");
    }
  });

  async function checkConnection() {
    const status = document.getElementById("connection-status");
    const label = document.getElementById("connection-label");
    status.className = "connection-status";
    label.textContent = "检查连接";
    try {
      const response = await fetch(config.healthUrl, { cache: "no-store", signal: AbortSignal.timeout(5000) });
      if (!response.ok || (await response.json()).status !== "ok") throw new Error("Unavailable");
      status.classList.add("online");
      label.textContent = "HTTP 已连接";
    } catch (_) {
      status.classList.add("offline");
      label.textContent = "HTTP 连接异常";
    }
  }
  document.getElementById("refresh-status").addEventListener("click", checkConnection);
  checkConnection();

  function tagOf(section) {
    const heading = section.querySelector(".opblock-tag");
    return heading ? heading.getAttribute("data-tag") || heading.textContent.trim() : "";
  }

  function selectNav(tag) {
    document.querySelectorAll(".side-nav .nav-item").forEach((item) => {
      const active = tag ? item.dataset.tag === tag : item.dataset.section === "overview";
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
  }

  function filterOperations() {
    const query = search.value.trim().toLocaleLowerCase();
    let visibleCount = 0;
    document.querySelectorAll("#swagger-ui .opblock-tag-section").forEach((section) => {
      const tag = tagOf(section);
      let count = 0;
      section.querySelectorAll(".opblock").forEach((operation) => {
        const summary = operation.querySelector(".opblock-summary");
        const text = `${tag} ${summary ? summary.textContent : ""}`.toLocaleLowerCase();
        const visible = !query || text.includes(query);
        operation.hidden = !visible;
        if (visible) count += 1;
      });
      section.hidden = count === 0;
      visibleCount += count;
    });
    empty.hidden = visibleCount !== 0;
  }
  search.addEventListener("input", filterOperations);
  document.addEventListener("keydown", (event) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
    if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      search.focus();
    } else if (event.key === "Escape" && document.activeElement === search) {
      search.value = "";
      filterOperations();
      search.blur();
    }
  });

  document.querySelectorAll("[data-tag], [data-operation], [data-section]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.dataset.section === "overview") {
        selectNav(null);
        window.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }
      search.value = "";
      filterOperations();
      let target;
      if (link.dataset.operation) {
        target = [...document.querySelectorAll("#swagger-ui .opblock")].find((operation) => {
          const path = operation.querySelector(".opblock-summary-path");
          return path && path.getAttribute("data-path") === link.dataset.operation && operation.classList.contains(`opblock-${link.dataset.method}`);
        });
        if (target && !target.classList.contains("is-open")) {
          target.querySelector(".opblock-summary-control").click();
        }
      } else {
        target = [...document.querySelectorAll("#swagger-ui .opblock-tag-section")].find((section) => tagOf(section) === link.dataset.tag);
      }
      if (target) {
        const section = target.classList.contains("opblock-tag-section") ? target : target.closest(".opblock-tag-section");
        const heading = section.querySelector(".opblock-tag");
        if (heading && heading.getAttribute("aria-expanded") === "false") heading.click();
        selectNav(tagOf(section));
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      } else {
        toast("接口仍在加载，请稍后再试");
      }
    });
  });

  function onReady() {
    const operations = document.querySelectorAll("#swagger-ui .opblock");
    document.getElementById("operation-count").textContent = String(operations.length).padStart(2, "0");
    document.querySelectorAll("#swagger-ui .opblock-tag-section").forEach((section) => {
      const tag = tagOf(section);
      const badge = document.querySelector(`.nav-count[data-count="${CSS.escape(tag)}"]`);
      if (badge) badge.textContent = String(section.querySelectorAll(".opblock").length).padStart(2, "0");
    });
    document.documentElement.dataset.docsReady = "true";
  }

  if (typeof SwaggerUIBundle !== "function") {
    const message = document.createElement("p");
    message.className = "error-state";
    message.textContent = "文档资源加载失败。请刷新页面，或检查 /docs-assets/ 是否可访问。";
    document.getElementById("swagger-ui").replaceChildren(message);
    return;
  }

  window.ui = SwaggerUIBundle({
    url: config.schemaUrl,
    dom_id: "#swagger-ui",
    deepLinking: true,
    docExpansion: "list",
    defaultModelsExpandDepth: -1,
    displayRequestDuration: true,
    persistAuthorization: false,
    validatorUrl: null,
    syntaxHighlight: { activated: true, theme: "agate" },
    presets: [SwaggerUIBundle.presets.apis],
    onComplete: () => { requestAnimationFrame(onReady); },
  });
})();
