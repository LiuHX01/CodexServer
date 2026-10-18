# CodexServer

云 Agent 执行与管理平台，采用 monorepo 组织两个相互配套的服务：

- [`agent-execution`](agent-execution/)：Agent 执行服务。基于 Codex SDK 提供会话、运行控制、SSE 事件、人工交互和操作回执。
- [`agent-management`](agent-management/)：Agent 管理服务。提供执行端登记、会话管理、运行控制、权限、人工待办、操作审计和浏览器工作台。

## 架构

```text
浏览器 / 其他客户端
          │
          ▼
agent-management
  API、PostgreSQL、调度/观察 Worker、容器云监听、管理工作台
          │
          ▼
agent-execution
  HTTP + SSE、Codex SDK、Agent Runtime
```

## 能力边界

### 管理系统（agent-management）

- 管理执行端登记、健康状态、能力和容量。
- 管理会话、运行任务、命令队列、重试和状态回执。
- 提供权限校验、幂等处理、人工交互、操作审计和浏览器工作台。
- 接收容器云 Worker 消息，并将允许的动作转成管理命令。
- 负责调度和记录，不直接运行 Codex 任务。

### 执行器（agent-execution）

- 通过 Codex SDK 创建和管理会话。
- 执行对话、代码操作和运行任务，并返回运行状态与最终结果。
- 提供 HTTP API、SSE 实时事件、历史查询和人工交互接口。
- 使用配置的项目目录作为工作区，并复用 ChatGPT 登录或 CC Switch 配置。
- 负责实际调用 Codex；不负责容器云接入、权限策略和管理审计。

## 本机启动

根目录提供统一的环境准备和双服务启动脚本。首次使用执行：

```bash
./scripts/setup_environment.sh
```

脚本会分别执行两个服务的 `uv sync --locked`，并在缺少配置时创建根目录 `.env`：

- `.env`：合并执行端、管理端和容器云 Worker 配置，并迁移已有服务配置。

如果使用 ChatGPT 登录模式，执行端还需要首次登录自己的 ChatGPT 账号：

```bash
cd agent-execution
uv run python -m app.codex_cli login --device-auth
```

然后分别启动两个服务：

```bash
./scripts/start_management.sh
./scripts/start_execution.sh
```

执行端默认监听 `127.0.0.1:8000`，管理端默认监听 `127.0.0.1:8080`。管理端启动时会自动处理 PostgreSQL、执行迁移，并在配置了容器云 endpoint 和 pairing token 时启动内嵌监听，同时登记同级目录中的执行端。两边日志分别在各自终端查看。需要一次启动两个服务时，可使用兼容脚本 `./scripts/start_all.sh`。

常用选项：

```bash
./scripts/start_management.sh --register-codex
./scripts/start_management.sh --port 8081
./scripts/start_execution.sh --port 8001

# 容器云监听参数写入根目录 .env 后，管理端启动时会自动连接：
# RELAY_CONTAINER_CLOUD_ENDPOINT=...
# RELAY_CONTAINER_CLOUD_TOKEN=...
# RELAY_CONTAINER_CLOUD_LOCAL_MODE=true  # 仅 ws:// 本地测试时需要

# 如需指定管理端对容器云使用的回调地址：
./scripts/start_management.sh --management-url http://192.168.1.20:8080
```

管理工作台默认地址为 <http://127.0.0.1:8080>。默认启动会自动判断数据库：项目默认数据库已可连接时直接复用，否则启动项目自己的本地 PostgreSQL；如果配置的是其他数据库地址，则不会尝试启动本地数据库。容器云监听属于管理端，不再需要额外启动第三个 Worker 进程。完整参数可通过 `--help` 查看。

## 日志

两个服务使用统一的终端格式：

```text
12:34:56 INFO     app.integrations.container_cloud.worker | event=task_received task_id=... operation=start_session
12:34:57 INFO     app.workers.dispatch | event=command_accepted command_id=... duration_ms=412
12:35:02 INFO     app.manager | event=run_finished run_id=... status=completed duration_ms=4870
```

交互式终端会按级别显示颜色；输出重定向到文件时自动使用纯文本。设置 `NO_COLOR=1` 可以强制关闭颜色。日志只记录任务和运行标识、状态、耗时及错误，不记录 token、完整 prompt 或代码内容；完整业务审计仍保存在管理端的事件记录中。

## 验证

两个服务保持独立依赖和测试配置，分别在子目录执行：

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

管理端内置容器云消息接入，详见 [`agent-management/README.md`](agent-management/README.md#容器云消息接入)。
