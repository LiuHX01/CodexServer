# Codex Server

用 HTTP 控制云主机上的 Codex。技术栈：官方 Python SDK + FastAPI + Uvicorn，使用 uv 管理环境。

默认使用自己的 ChatGPT 账号，模型 `gpt-6.1-sol`，推理强度 `medium`。

## 启动

项目已有 `.env` 时直接使用。首次部署时将 `.env.example` 复制为 `.env`，生成随机服务 token，填入 `API_TOKEN`：

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

登录只需首次执行。HTTP 服务 token 与 ChatGPT 登录凭证是两回事；本服务固定使用 ChatGPT 账号登录，不需要 OpenAI API key。

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

模型和推理强度统一在 `.env` 配置，每次对话都会应用：

```dotenv
CODEX_MODEL=gpt-6.1-sol
CODEX_REASONING_EFFORT=medium
```

`stream` 是 JSON 布尔值，两种模式使用同一个发消息接口：

| 请求字段 | HTTP 响应 | 使用方式 |
| --- | --- | --- |
| `"stream": true` | 200，`text/event-stream` | 发送消息后直接持续接收输出 |
| `"stream": false` 或省略 | 202，任务 JSON | 保存任务 `id`，再查询结果或订阅事件 |

流式响应头 `X-Run-ID` 以及首个 `run.queued` 事件给出任务 ID，可用于取消或断线补读。收到 HTTP 200 仅表示事件流已建立，执行结果以 `run.finished` 的状态为准。连接断开后任务继续执行；需要恢复输出时使用 `GET /v1/runs/{run_id}/events`，不要重新提交同一条消息。

## 接口

保留 10 个业务接口，文档按“会话 / 对话 / 服务”分组：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/v1/threads` | 创建会话，可填 name、cwd |
| GET | `/v1/threads` | 会话列表 |
| GET | `/v1/threads/{thread_id}/history` | 对话历史 |
| POST | `/v1/threads/{thread_id}/resume` | 显式恢复会话，无需请求体 |
| POST | `/v1/threads/{thread_id}/archive` | 归档会话 |
| POST | `/v1/threads/{thread_id}/messages` | 发消息，prompt + 可选 stream，支持直接流式返回 |
| GET | `/v1/runs/{run_id}` | 查询任务和最终回复 |
| GET | `/v1/runs/{run_id}/events` | SSE 流式输出 |
| POST | `/v1/runs/{run_id}/cancel` | 取消任务 |
| GET | `/v1/status` | 查看并发及队列状态 |

除 `/healthz` 和文档资源外，业务接口需要 `Authorization: Bearer <API_TOKEN>`。

会话列表及历史支持 `cursor`、`limit` 分页，使用响应的 `nextCursor` 继续读取。列表默认显示未归档会话，传 `archived=true` 可查看归档会话。空会话单独列在 `drafts` 中；发送第一条消息后才会保存历史，因此空会话不能归档，并会在服务重启后消失。

## 并行、流式输出与恢复

- 不同会话并行执行，默认最多 4 个，多余任务排队；同一会话忙碌时再次发消息返回 409。
- SSE 中 `item/agentMessage/delta` 的 `data.delta` 为文本片段，`run.finished` 给出最终状态。支持使用 `Last-Event-ID` 或 `?after=<id>` 补读事件。客户端断开不会取消任务。
- 服务重启后，未结束的任务标为中断。已保存的会话可以继续 resume，旧消息不会自动重试。
- 归档只隐藏默认列表中的会话，保留历史。当前精简版不提供取消归档接口。

任务和事件保存在 `DATA_DIR/server.sqlite3`，Codex 登录和历史保存在 `CODEX_HOME`，项目文件保存在 `WORKSPACE_ROOT`，默认都在 `data/` 下。升级代码不会删除这些数据。

使用 `--workers 1`；文件锁会拒绝重复占用同一个 DATA_DIR。这里的多会话并行由异步任务实现，不需要启动多个 Uvicorn worker。

## 验证与部署

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

`tests/test_sdk_smoke.py` 默认跳过，设置 `RUN_CODEX_SMOKE=1` 后可验证真实 SDK/runtime 与本地模拟模型的完整流程，不消耗真实模型额度。

对已经登录并启动的真实服务执行 curl 集成测试：

```bash
uv run python -m scripts.http_smoke --base-url http://127.0.0.1:8000
```

该脚本进行真实模型调用，只操作自己创建的测试会话，结束后将其归档；报告保存到 `data/http-smoke-*.json`。

可选部署模板保留在 `Dockerfile`、`compose.yaml` 和 `deploy/`。Compose 默认仅映射主机回环地址 8000，可配合 HTTPS 反向代理或 SSH 隧道访问。文档的脚本和样式由服务本地提供，测试电脑无需访问外部 CDN。
