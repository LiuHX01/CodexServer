# Codex Server

通过 HTTP + SSE 提供业务无关的 Codex Agent 能力，供云中枢或其他服务调用。
技术栈：官方 Python SDK + FastAPI + Uvicorn，使用 uv 管理环境。

本服务负责会话、执行控制、事件和 Agent 请求回传。用户体系、业务调度、通知、审批人分配及客户端展示由调用方负责。

默认使用自己的 ChatGPT 账号，模型 `gpt-6.1-sol`，推理强度 `medium`。

## 启动

统一启动时使用仓库根目录 `.env`。独立运行执行端时，才需要在本目录创建 `.env`：

```bash
uv sync --locked
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

登录并启动：

```bash
uv run python -m app.codex_cli login --device-auth
uv run python -m app.codex_cli login status
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

登录只需首次执行。HTTP 服务 token 与 ChatGPT 登录凭证是两回事；默认模式使用 ChatGPT 账号登录；也支持下文的 CC Switch 第三方 Provider 模式。

另一台电脑打开 `http://服务端IP:8000/docs`，点击 **Authorize**，填入 `.env` 中的 `API_TOKEN` 值。服务端防火墙及云安全组需要允许测试电脑访问 TCP 8000；公网连接使用 HTTPS 或 SSH 隧道。

`/docs` 提供浅色 API 工作台：左侧分组导航、接口搜索、三步使用提示及可复制的流式请求示例，支持手机浏览。接口仍可通过 **Try it out → Execute** 直接调试；查看 SSE 的实时输出建议使用页面提供的 `curl -N` 示例。

Windows 找不到 uv 时，先在 PowerShell 执行：

```powershell
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
```

## 日常使用

1. 创建会话：`POST /v1/threads`，填写 `{"name":"项目A"}`，保存响应的 `id`。
2. 发送消息：`POST /v1/threads/{thread_id}/messages`，填写 `{"prompt":"你好","stream":true}`。
3. 同一个请求会持续返回 SSE：`item/agentMessage/delta` 是文字片段，最后的 `run.finished` 包含 `status` 和完整 `final_response`。失败和取消分别为 `failed`、`interrupted`。
4. 继续向同一个会话发送消息，即可继续对话；服务会自动 resume。

创建会话只需名称，名称也可以省略。可选的 `cwd` 指定服务端 `WORKSPACE_ROOT` 下已经存在的项目目录；不填会自动创建独立目录。Swagger 的示例请求可以直接提交，无需填写占位字符串。

模型、推理强度和审批策略在 `.env` 设置默认值，每轮执行可通过 `options` 覆盖：

```dotenv
CODEX_MODEL=gpt-6.1-sol
CODEX_REASONING_EFFORT=medium
CODEX_APPROVAL_POLICY=never
ALLOWED_MODELS=[]
ALLOWED_APPROVAL_POLICIES=["never","on-request","untrusted"]
```

`ALLOWED_MODELS` 为额外允许的模型，默认模型始终允许；审批策略必须在部署方配置的允许列表中。目录和沙盒权限仍由服务端约束，调用方不能通过执行参数扩大范围。

`stream` 是 JSON 布尔值，两种模式使用同一个发消息接口：

| 请求字段 | HTTP 响应 | 使用方式 |
| --- | --- | --- |
| `"stream": true` | 200，`text/event-stream` | 发送消息后直接持续接收输出 |
| `"stream": false` 或省略 | 202，任务 JSON | 保存任务 `id`，再查询结果或订阅事件 |

流式响应头 `X-Run-ID` 以及首个 `run.queued` 事件给出任务 ID，可用于取消或断线补读。收到 HTTP 200 仅表示事件流已建立，执行结果以 `run.finished` 的状态为准。连接断开后任务继续执行；需要恢复输出时使用 `GET /v1/runs/{run_id}/events`，不要重新提交同一条消息。

## 接口

提供 17 个能力接口，文档按“会话 / 对话 / 交互 / 服务”分组：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/v1/threads` | 创建会话，可填 name、cwd |
| GET | `/v1/threads` | 会话列表 |
| GET | `/v1/threads/{thread_id}` | 会话当前状态 |
| GET | `/v1/threads/{thread_id}/history` | 对话历史 |
| GET | `/v1/threads/{thread_id}/events` | 跨轮次订阅会话事件 |
| GET | `/v1/threads/{thread_id}/requests` | 查询待回复的 Agent 请求 |
| POST | `/v1/threads/{thread_id}/resume` | 显式恢复会话，无需请求体 |
| POST | `/v1/threads/{thread_id}/archive` | 归档会话 |
| POST | `/v1/threads/{thread_id}/messages` | 发消息，prompt + 可选 stream，支持直接流式返回 |
| GET | `/v1/runs/{run_id}` | 查询任务和最终回复 |
| GET | `/v1/runs/{run_id}/events` | SSE 流式输出 |
| POST | `/v1/runs/{run_id}/input` | 向当前执行追加输入 |
| POST | `/v1/runs/{run_id}/cancel` | 取消任务 |
| POST | `/v1/requests/{request_id}/response` | 回传答案或审批决策 |
| GET | `/v1/status` | 查看并发及队列状态 |
| GET | `/v1/capabilities` | 查询协议、数据实例和能力 |
| GET | `/v1/operations/{key}` | 查询幂等操作回执 |

除 `/healthz` 和文档资源外，业务接口需要 `Authorization: Bearer <API_TOKEN>`。

会话列表及历史支持 `cursor`、`limit` 分页，使用响应的 `nextCursor` 继续读取。列表默认显示未归档会话，传 `archived=true` 可查看归档会话。空会话单独列在 `drafts` 中；发送第一条消息后才会保存历史，因此空会话不能归档，并会在服务重启后消失。

## 并行、流式输出与恢复

- 不同会话并行执行，默认最多 4 个，多余任务排队；同一会话忙碌时开始新执行返回 409，追加输入和回复 Agent 请求仍可使用。
- SSE 中 `item/agentMessage/delta` 的 `data.delta` 为文本片段，`run.finished` 给出最终状态。支持使用 `Last-Event-ID` 或 `?after=<id>` 补读事件。客户端断开不会取消任务。
- 服务重启后，未结束的任务标为中断。已保存的会话可以继续 resume，旧消息不会自动重试。
- 归档只隐藏默认列表中的会话，保留历史。当前精简版不提供取消归档接口。

任务和事件保存在 `DATA_DIR/server.sqlite3`，Codex 登录和历史保存在 `CODEX_HOME`，项目文件保存在 `WORKSPACE_ROOT`，默认都在 `data/` 下。升级代码不会删除这些数据。

使用 `--workers 1`；文件锁会拒绝重复占用同一个 DATA_DIR。这里的多会话并行由异步任务实现，不需要启动多个 Uvicorn worker。

## 执行中追加输入

```http
POST /v1/runs/{run_id}/input
Content-Type: application/json

{"prompt":"补充要求：兼容旧接口"}
```

成功时返回 `{"turnId":"..."}`，并发布 `run.input_accepted`。这表示输入已被 Runtime 接受，不表示模型已经完成处理，也不会撤销已执行的操作。
追加输入沿用当前 turn，不创建新 run。排队、已结束或取消中的 run 返回 409，未知 run 返回 404。若提交时 Runtime 恰好结束当前 turn，也会拒绝；本服务不会自动创建下一轮执行。

## Agent 提问与审批

执行参数示例：

```json
{
  "prompt": "分析两个实现方案，需要时向我提问",
  "options": {
    "reasoning_effort": "high",
    "approval_policy": "on-request",
    "collaboration_mode": "plan"
  }
}
```

执行模式支持 `default` 和 `plan`，每轮省略时重置为 `default`。当前锁定 Runtime 的结构化 `request_user_input` 在 Plan 模式可用，在 Default 模式不可用；Default 模式中的普通对话补充使用追加输入接口。是否调用提问工具仍取决于模型和 Runtime。

支持五种 Runtime 请求：`item/tool/requestUserInput`、`item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`item/permissions/requestApproval`、`mcpServer/elicitation/request`。
请求通过 `request.pending` 事件发布，也可用 `GET /v1/threads/{thread_id}/requests` 查询。返回的 `id` 为本服务生成的请求 ID，`params` 保留 Runtime 原始参数。未知请求类型会向 Runtime 返回明确的不支持错误，不会默认批准。

回答提问时，使用 `params.questions` 中的真实问题 ID：

```http
POST /v1/requests/{request_id}/response
Content-Type: application/json

{"result":{"answers":{"strategy":{"answers":["方案 A"]}}}}
```

命令或文件审批使用 `{"result":{"decision":"accept"}}` 等对应协议结果；命令审批还必须遵循请求中的 `availableDecisions`（如有）。权限和 MCP 请求的回复结构以 `app/protocol/` 下对应方法的响应 schema 为准。

回复先按锁定版本的协议 schema 校验，格式不符返回 422；重复或失效回复返回 409。状态为：

- `pending`：等待调用方回复。
- `responded`：回复已写入 Runtime，尚未收到处理确认。
- `resolved`：回复后收到 Runtime 的请求处理确认。
- `expired`：Runtime 提前清除请求，或执行结束、取消、连接丢失、服务停止或重启。

默认只查询 `pending`，传 `pending_only=false` 可查询所有状态。等待回答不阻塞其他会话的消息接收，但仍占用一个执行槽，且计入 `RUN_TIMEOUT_SECONDS`。重启后原请求不可继续回复；恢复历史不会恢复原执行或审批。

## 会话事件协议

`GET /v1/threads/{thread_id}/events` 保持订阅，跨 run 接收执行输出、会话状态及交互请求事件。
使用 `follow=false` 只读取当前已保存事件，读完即结束；`after` 或 `Last-Event-ID` 可补读。流关闭不取消执行。

SSE 的 `event` 保留事件名，`data` 为带关联信息的事件信封：

```json
{
  "id": 42,
  "version": 1,
  "event": "item/agentMessage/delta",
  "thread_id": "...",
  "turn_id": "...",
  "run_id": "...",
  "created_at": "2026-10-17T00:00:00+00:00",
  "data": {"delta": "文字片段"}
}
```

关联不到执行的会话事件允许 `run_id` 或 `turn_id` 为 `null`。ID 全局单调增长，单个会话内不要求连续。已有 run SSE 接口保持原始 `data` 格式兼容。
事件表示服务端记录顺序；会话通知与 turn 通知经 SDK 独立队列处理，不承诺跨队列的 Runtime 原始全序。未携带幂等键的指令不会自动去重。中枢集成可使用下述回执协议。

## 中枢集成与幂等回执

`GET /v1/capabilities` 返回持久化 `instance_id` 与能力声明。实例标识保存在 DATA_DIR 中，重启不变；替换数据目录后变化。调用方应将会话绑定到该实例。

POST 命令可携带 `Idempotency-Key`（最多 200 字符），发送消息时同时使用 `stream=false`。服务持久化请求摘要与 JSON 回执，相同键和内容返回原回执，内容不同返回 409。带 `X-Agent-Instance` 的幂等命令还会检查实例身份，不匹配时拒绝投递。服务 token 认证在回执写入前执行。

`GET /v1/operations/{key}` 返回原操作的 `status`、`status_code`、`response`。`completed` 只代表 HTTP 命令已有回执；实际运行完成仍需查询 run 或订阅事件。正在投递或崩溃中断的操作不能安全重试；重启后遗留投递标记为 `unknown`，副作用与回执写入不是跨系统原子事务，服务不承诺严格 exactly-once。

## 代码结构

- `main.py`：应用生命周期、异常映射和文档资源。
- `api.py` / `streams.py`：HTTP 能力接口和 SSE 订阅。
- `idempotency.py`：可选幂等命令、实例条件及持久化操作回执。
- `manager.py`：执行排队、追加输入、取消及状态收敛。
- `interactions.py`：Agent 请求校验、回复和失效管理。
- `backend.py` / `sdk.py`：Codex 操作和 SDK 延迟回复适配。
- `store.py`：执行、事件和交互状态持久化；自动迁移旧事件表并保留事件 ID。

SDK 固定为 `0.162.1`。该版本没有公开的异步请求回传钩子，`sdk.py` 集中适配其内部读取/写入接口；升级时必须同步检查适配层、协议 schema 和传输测试。

## 验证与部署

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

`tests/test_sdk_smoke.py` 默认跳过，设置 `RUN_CODEX_SMOKE=1` 后可验证真实 SDK/runtime 与本地模拟模型的完整流程，包括追加输入、交互回复和等待时的并行执行，不消耗真实模型额度。

对已经登录并启动的真实服务执行 curl 集成测试：

```bash
uv run python -m scripts.http_smoke --base-url http://127.0.0.1:8000
```

该脚本进行真实模型调用，只操作自己创建的测试会话，结束后将其归档；报告保存到 `data/http-smoke-*.json`。

可选部署模板保留在 `Dockerfile`、`compose.yaml` 和 `deploy/`。Compose 默认仅映射主机回环地址 8000，可配合 HTTPS 反向代理或 SSH 隧道访问。文档的脚本和样式由服务本地提供，测试电脑无需访问外部 CDN。

默认项目目录可在执行端 `.env` 设置 `DEFAULT_CWD`（重启生效）。该目录必须已经存在且位于 `WORKSPACE_ROOT` 内；相对路径相对于 `WORKSPACE_ROOT`。创建会话时显式传入的 `cwd` 优先；两者均未配置时仍创建独立目录。仅影响新会话。


## 使用 CC Switch 的第三方服务

在 CC Switch 中为 Codex 选好支持 Responses API 的供应商和模型，在执行端 `.env` 配置：

```dotenv
CODEX_AUTH_MODE=ccswitch
CODEX_PROVIDER_CONFIG_DIR=~/.codex
CODEX_HOME=./data/codex
```

`CODEX_PROVIDER_CONFIG_DIR` 指向 CC Switch 实际管理的 Codex 配置目录，可以与 `CODEX_HOME`
不同。重启执行端后，读取源 `config.toml` 中当前选中的 `model_provider`、对应 Provider 和
`model`。该模型会成为服务默认值，替代 `.env` 的 CODEX_MODEL；每轮仍可以在允许的模型列表内覆盖。
CC Switch 切换后需要再次重启执行端，不会在正在执行的轮次中自动更换供应商。
建议切换后新建测试会话，已有会话记录不会自动迁移供应商身份。

支持从 Provider 的 `experimental_bearer_token`、`env_key` 指向的进程环境变量，或源目录
`auth.json` 的 `OPENAI_API_KEY` 读取 API key。使用 `env_key` 时，需在启动服务的终端 export
对应变量；这里不会将源目录 `.env` 自动导入。不会拿 ChatGPT OAuth token 当第三方 API key。
API key 和静态 HTTP headers 通过子进程环境变量传入，不放到 CLI argv 或写入新的认证文件。
当前暂不导入 Provider 的 auth 命令。供应商需实现 Responses API 及 Codex 使用的流式/工具调用协议；
仅支持 Chat Completions 的服务无法接入。

保留 `CODEX_HOME=./data/codex` 可将执行器的会话和数据与桌面端隔离；源配置和凭据文件不会被复制或改写。
启动辅助 CLI 也使用相同供应商设置：

```bash
uv run python -m app.codex_cli --version
```

使用第三方模式无需执行 ChatGPT 登录。恢复原方式：将 `CODEX_AUTH_MODE=chatgpt`，重启执行端，
必要时再运行 `uv run python -m app.codex_cli login --device-auth`。
