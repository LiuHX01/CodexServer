# AgentRelay

Agent 能力的管理中枢。管理执行端、会话、运行控制、人工交互和操作回执，附带完整浏览器工作台。

当前接入 agent-execution。中枢不定义业务 Task、Execution、父子任务或交付流程；`Run` 是执行端运行事实的镜像。新增业务入口调用现有会话能力，新增 Agent 实现适配器。

## 本机启动与体验

需要 Python 3.12、uv、PostgreSQL 17；agent-execution 单独运行并完成登录。本机已准备好开发数据库和配置。

终端一：启动执行端（已有服务时跳过）。

```bash
cd /path/to/CodexServer/agent-execution
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

终端二：启动中枢、内嵌调度 Worker、容器云监听和页面。

```bash
cd /path/to/CodexServer/agent-management
uv sync --locked
uv run python -m scripts.dev --register-codex
```

打开 [管理中枢](http://127.0.0.1:8080)。首次登录所需密钥只在自己的终端查看：

```bash
uv run python -m scripts.access
```

登录后会保留 8 小时 HttpOnly 会话 cookie。执行端凭证加密保存在中枢数据库，不返回浏览器。
`--register-codex` 仅首次登记同级工程的本机地址和凭证，已有记录不会覆盖。之后可在页面“执行端 → 设置”更新凭证。

如果其他机器已有 PostgreSQL，运行 `scripts.bootstrap` 生成配置，设置 `RELAY_DATABASE_URL`，执行 `uv run alembic upgrade head`，再直接启动 Uvicorn。`scripts.dev` 专用于本机默认数据库。

本机数据库位于 `data/postgres`，使用独立端口 55432，仅监听回环地址，开发认证为 trust；不要将此配置暴露到公网。可选容器数据库见 `compose.yaml`，需自设 `POSTGRES_PASSWORD`，并在数据库 URL 中填写密码；容器与本机数据库二选一。

## 页面与手动验收

| 页面 | 可以体验什么 |
| --- | --- |
| 总览 | 在线执行端、会话、运行数量、人工待办、待核对操作 |
| 执行端 | 连接检查、能力与容量、禁止新分派、凭证更新、导入已有会话 |
| 会话 | 创建、搜索、筛选、实时输出、继续对话、追加输入、中断、归档 |
| 会话详情 | 人工交互、运行参数、事件原文、原生标识、会话授权 |
| 人工待办 | 问题选项与自定义答案、命令/文件审批、其他协议响应 |
| 操作记录 | 操作者、投递结果、错误原因、查询未知操作回执 |
| 实时日志 | 中枢受理、Worker 领取、执行端投递、回执、观察连接与原始事件，支持 SSE 实时跟随 |
| 测试指南 | 可填入会话的四组测试指令，不自动发送 |

建议依次体验：

1. 在“执行端”确认 Local Codex 在线，点击“新建会话”，工作目录留空会创建隔离目录。
2. 在“测试指南”填入“开始对话”示例并发送。等完成后追问标记，检查上下文保留。
3. 另建测试会话，发送长输出示例。在运行中追加“数到 20 即可”；再启动一次并点击中断。追加可能与运行结束竞争，过晚提交会明确拒绝。
4. 使用 Plan 提问示例，在“人工交互”或“人工待办”回答问题，观察执行继续。Default 模式不支持结构化 `request_user_input`。
5. 使用命令审批示例。确认当前工作目录和命令后选择允许或拒绝；允许会实际创建隔离目录中的 `approval-demo.txt`。模型是否请求审批取决于运行时策略和命令。
6. 运行中刷新或关闭页面，再次打开，检查状态、历史和人工待办恢复。断开浏览器不取消云端执行。
7. 执行端“禁止新分派”后，新建会话和新轮次返回拒绝；已有运行仍能追加、中断和回答请求。
8. 打开“实时日志”，观察同一操作从 `command.queued`、`command.dispatching`、`command.sending` 到 `command.accepted` / `command.rejected` / `command.unknown` 的完整链路；展开任意记录可查看结构化数据。
9. 操作结果待核对时，在“操作记录”点击“核对”。查询原回执不会重新执行原命令。

真实执行会使用已连接执行端的模型额度；页面不会自动发送指令。测试过程中不要手动批准不理解的命令。

## 结构与数据流

```text
浏览器 / 其他客户端
        │ HTTP 命令 + SSE 事件
        ▼
FastAPI API ── PostgreSQL ── Worker
                              │ Provider adapter
                              ▼
                         agent-execution
                              │
                         Codex Runtime
```

| 位置 | 职责 |
| --- | --- |
| `app/api.py` | 管理接口、权限入口、SSE 订阅 |
| `app/services/commands.py` / `sessions.py` | 原子受理、幂等、并发控制 |
| `app/services/events.py` | 原生事件归并、运行和请求镜像、游标提交 |
| `app/workers/dispatch.py` | 领取命令、执行端投递、回执核对 |
| `app/workers/observe.py` | 按会话租约订阅、断线恢复与状态核对 |
| `app/adapters/` | 执行端协议边界，当前实现 Codex |
| `app/security.py` | 角色、会话 ACL、密钥和凭证加密 |
| `app/models.py` / `migrations/` | 存储模型和可审查的数据库迁移 |
| `app/static/` | 原生 ESM 前端，视图/控制/API/样式分离，无构建依赖 |
| `tests/` | PostgreSQL 集成测试与真实 SDK 可选联调 |

API 返回 `202` 表示命令已持久化；命令 `accepted` 表示执行端已受理。运行完成必须查看 `Run.status` 或 `run.finished`。中断请求已投递也不等于运行已结束。

命令状态：`queued → dispatching → accepted / rejected / unknown`。相同身份和幂等键只对应一个操作；更换内容复用键会被拒绝。HTTP 响应丢失或 worker 租约过期进入 `unknown`，仅核对远端回执；不会盲目重发。进程退出发生在远端副作用与回执保存之间时，结果仍可能无法自动判定。

每个会话绑定执行端 ID、数据实例 ID 和原生 thread ID。执行端的数据实例变化会冻结原绑定；请求中也携带实例条件，防止向替换后的实例投递。源游标、事件及镜像在一个事务提交，重复源事件无副作用。全局事件发布使用事务 advisory lock，避免 PostgreSQL 序列先分配后提交导致 SSE 漏读。这里只串行发布事务，不包含远端网络调用；高吞吐时可以替换为独立 outbox 发布序列。

## 权限与接口调试

初版使用配置式身份，角色为 `admin / operator / viewer`，会话拥有者和成员权限决定可见范围。管理员可以管理执行端与导入原生会话；操作者可以使用自己的或被授权的会话；只读身份不能执行控制操作。

在 `.env` 配置 `RELAY_ACCESS_KEYS` JSON 数组，例如对象字段为 `principal`、`role`、`token`；为每个身份生成不同随机 token。更改配置后重启。会话“详情 → 共享会话”可向已经配置的身份授权 `view / operate`。全局 viewer 即使被授予 operate 也保持只读。详情中可查看和撤销已有成员授权；撤销后已打开的会话事件订阅也会停止。

[API 文档](http://127.0.0.1:8080/api-docs) 与页面同源，登录后可直接调试；程序请求用 `Authorization: Bearer <访问密钥>`。所有命令 POST 都要带 `Idempotency-Key`，网络重试时沿用原键。

主要接口：

- `/v1/agents`：登记、查询；`/{id}/probe` 检查，`/{id}/sessions` 创建，`/{id}/import` 导入。
- `/v1/sessions`：查询；`/{id}` 详情，`/{id}/messages` 发消息，`/{id}/history` 历史，`/{id}/archive` 归档。
- `/v1/runs/{id}/input`、`/interrupt`：追加和中断。
- `/v1/requests`、`/{id}/response`：人工交互。
- `/v1/commands`、`/{id}/reconcile`：操作回执与核对。
- `/v1/events?after=0`：SSE，支持 `Last-Event-ID`、`session_id`、`follow=false`。

## 验证

```bash
uv run pytest -q
RUN_CODEX_INTEGRATION=1 uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run alembic check
```

测试使用同一数据库中的独立随机 schema，结束后删除测试 schema。真实联调需同一 monorepo 下的 agent-execution 及其已安装的依赖，也可用 `CODEXSERVER_PATH` 指定路径。它启动真实服务和 SDK/runtime，以隔离 CODEX_HOME 访问本地模拟模型，验证创建、Plan 提问、人工回复、流式事件、历史和完成状态，不使用真实模型额度。

## 部署与扩展边界

管理端进程内包含数据库调度/观察 Worker 和容器云 WebSocket 监听，不需要另起容器云 Worker 进程。前端随 API 提供，默认仅本机 8080。数据库调度 Worker 仍可通过 `RELAY_EMBEDDED_WORKER=false` 拆出运行，但这不是默认启动方式。

新增 Provider 实现 `app/adapters/base.py` 协议并登记工厂和输入 schema；业务入口可单独加 router/service，不需要改变原生运行镜像。初版没有自动调度、机器供应、SSO、业务任务编排和通知系统。也不提供容器级运行隔离或跨执行端工作目录迁移；实际执行隔离由执行端部署负责。

列表暂有数量上限（会话/操作 200，详情子记录 300，历史最近一页 50），未做长历史分页 UI、日志保留策略或数据库事件压缩。当前一会话一个 SSE 观察连接，适用于首版；规模扩大后需执行端聚合订阅、订阅上限及负载测试。旧版缺少数据实例和回执协议的 CodexServer 只能降级接入，无法保证实例冻结或自动核对，建议配套升级。

数据库与 `.env` 中的 `RELAY_SECRET_KEY` 必须配套备份；更换密钥会使已有执行端凭证无法解密。正式部署使用认证数据库、HTTPS，并设置 `RELAY_COOKIE_SECURE=true`。本机开发数据库不自动随 API 停止，停止命令：`uv run python -m scripts.postgres stop`。

## 容器云消息接入

`agent-management` 内置容器云消息接入。管理端启动后，它通过 WebSocket 接收容器云任务，再调用本地管理 API；管理动作仍经过现有的权限、幂等 Command、Dispatcher 和审计链路。

在管理端 `.env` 配置 endpoint 和 pairing token 后启动管理端：

```bash
cd agent-management
uv sync --locked
uv run python -m scripts.dev
```

底层 `scripts.container_cloud_worker` 入口仍保留用于兼容性和专项调试；日常启动不要再额外运行它。管理端配置示例：

```bash
RELAY_CONTAINER_CLOUD_ENDPOINT=wss://example.test/api/v1/relay-demo/worker
RELAY_CONTAINER_CLOUD_TOKEN=container-cloud-pairing-token
RELAY_CONTAINER_CLOUD_MANAGEMENT_URL=http://127.0.0.1:8080
```

容器云配对 token 和 management API token 是两种凭证。统一启动时在仓库根目录 `.env` 中配置：

```dotenv
RELAY_CONTAINER_CLOUD_ENDPOINT=wss://example.test/api/v1/relay-demo/worker
RELAY_CONTAINER_CLOUD_TOKEN=container-cloud-pairing-token
# 不设置时默认使用 RELAY_ADMIN_TOKEN
RELAY_CONTAINER_CLOUD_MANAGEMENT_TOKEN=management-api-token
RELAY_CONTAINER_CLOUD_MANAGEMENT_URL=http://127.0.0.1:8080
# [qwe] echo prompts are sent to this existing management session
RELAY_CONTAINER_CLOUD_TARGET_SESSION_ID=session-id
RELAY_CONTAINER_CLOUD_LOCAL_MODE=false
```

当前 Worker 支持的任务操作包括：

- `echo`：兼容原始演示 Worker 的回显操作。
  当 `text` 以 `[qwe]` 开头时，去掉前缀后的内容会作为 prompt 投递到
  `RELAY_CONTAINER_CLOUD_TARGET_SESSION_ID` 指定的会话，同时保留原始 `text` 回传。
- `agent_probe`：检查执行端连接和能力。
- `agent_patch`：更新执行端配置。
- `create_session`：创建会话。
- `start_session`：向会话发送消息。
- `archive_session`：归档会话。
- `steer_run`：向运行追加输入。
- `interrupt_run`：中断运行。
- `reply_request`：回答人工交互请求。

任务使用显式的 `operation` 和 `payload`，示例：

```json
{
  "type": "task",
  "taskId": "container-event-123",
  "nonce": "retry-1",
  "operation": "start_session",
  "payload": {
    "session_id": "session-id",
    "body": {"prompt": "继续执行容器任务"}
  }
}
```

新增容器云行为时，在 `app/integrations/container_cloud/actions.py` 的 `ManagementActionRegistry` 中增加命名动作和对应的管理 API 映射。Worker 不接受任意 URL 或任意 HTTP 方法，避免外部消息绕过管理系统的边界。

## 无需改容器的预设短任务

兼容 ChangeAtlas `8305fa8`：收到指定 echo 文本后，先执行本地动作，成功才原样回传
`text`、`taskId`、`nonce`。具体结果在 Worker 日志中按 taskId 关联；额外 details
不会被当前容器转发到 Kim。失败回传不带原文，容器将忽略并在约 15 秒后提示超时。
本功能验证 Worker 本地动作，不调用 Codex。

显式指定管理端所在电脑上的现有测试目录：

```dotenv
RELAY_CONTAINER_CLOUD_ENDPOINT=wss://merchant-changeatlas-prt.test.gifshow.com/api/v1/relay-demo/worker
RELAY_CONTAINER_CLOUD_TOKEN=container-cloud-pairing-token
RELAY_CONTAINER_CLOUD_PRESET_DIR=/absolute/path/to/test-project
```

管理端启动后会使用这些配置。未设置目录时禁用预设任务；
不自动继承执行端 DEFAULT_CWD。

在 Kim 群真正 @机器人后发送：

| 指令 | 本地动作 |
| --- | --- |
| `/ws-ping test-project` | 校验项目目录存在，日志记录实际目录 |
| `/ws-ping test-git` | 读取仓库根目录和 HEAD；没有提交的仓库失败，Git 命令限时 4 秒 |
| `/ws-ping test-create-file` | 创建 `.agentrelay-smoke.txt` 并读取校验；同内容可重复测试，不覆盖不同内容或跟随符号链接 |

`test-` 前缀保留给预设任务，拼错会失败，不会当普通 echo 报成功；其他普通文字仍原样回显。
本地模拟器输入 `/echo test-project` 等；Python Worker 加 `--local` 并使用模拟器的 ws 地址。
成功后 Kim 仍显示原版“链路测试成功”，不显示任意执行输出。

## 两台电脑测试操作回执和最终回答

电脑 A 运行本项目的管理端和执行端，管理端内嵌容器云监听。电脑 B 运行
ChangeAtlas 的 `scripts/relay-demo-simulator.mjs`（需使用支持 `task_update` 的新版），
监听局域网地址；A 的管理端主动连接 B，管理 API 仍使用 A 本机的 8080。

B 的示例启动命令（先安装 `ws@8.22.0`）：

```bash
RELAY_SIM_HOST=0.0.0.0 RELAY_SIM_TOKEN=two-pc-test node relay-demo-simulator.mjs 8787
```

A 在本目录配置 `.env` 后启动管理端（替换 B 的地址）：

```bash
RELAY_CONTAINER_CLOUD_ENDPOINT=ws://192.168.1.20:8787
RELAY_CONTAINER_CLOUD_TOKEN=two-pc-test
RELAY_CONTAINER_CLOUD_LOCAL_MODE=true
uv run python -m scripts.dev
```

B 输入 `/echo hello` 测试回传，再在 A 的管理页面创建会话，复制管理会话 ID。
B 可输入一整行：

```text
/task start_session {"session_id":"管理会话ID","body":{"prompt":"执行 pwd，报告当前目录，不修改文件。"}}
```

回传分两部分：

- `type=result`：管理 API 的原始操作回执，HTTP 202/queued 不表示执行完成。
- `type=task_update`：带相同 taskId 和 nonce 的后续更新，event 为
  `command_status`、`command_ended`、`run_status`、`run_finished` 或 `observation_error`。
  `run_finished.data` 包含 status、run_id、session_id、final_response、error。
  模拟器会单独打印最终回答。当前提供状态变化与最终完整文本，未逐字推送模型输出。

Worker 按命令回执的原生 run ID 匹配运行，不取会话中任意一个最新结果。
等待结果不会阻塞新任务；可继续使用 `/task interrupt_run {"run_id":"管理运行ID"}` 中断。
`create_session` 等控制操作在命令 accepted 后回传 `command_ended`，含原始执行回执。
`unknown` 表示需要人工核对，不自动重发；`observation_error` 表示回传跟踪失败，
不代表 Codex 执行失败。默认每秒查询，最多等待 1800 秒，可用环境配置
`RELAY_CONTAINER_CLOUD_RESULT_POLL_SECONDS` 和 `RELAY_CONTAINER_CLOUD_RESULT_TIMEOUT_SECONDS` 调整。

连接断开或 Worker 退出会停止此次跟踪，但不取消 Codex 任务。重连不会自动补发更新；
在管理页面查看运行状态，避免为了取结果重复发送任务。线上旧 ChangeAtlas 容器不会
处理 task_update；这项扩展需要模拟器或后续容器端配套支持，原 echo 流程保持兼容。
