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
  API、PostgreSQL、Worker、管理工作台
          │
          ▼
agent-execution
  HTTP + SSE、Codex SDK、Agent Runtime
```

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

然后从仓库根目录启动两个服务：

```bash
./scripts/start_all.sh
```

其中执行端默认监听 `127.0.0.1:8000`，管理端默认监听 `127.0.0.1:8080`。管理端启动时会复用 `scripts.dev`，自动处理 PostgreSQL、执行迁移，并登记同级目录中的执行端。按 `Ctrl+C` 会同时停止两个服务。

常用选项：

```bash
./scripts/start_all.sh --no-register-codex
./scripts/start_all.sh --execution-port 8001 --management-port 8081

# Worker 参数写入根目录 .env 后，启动脚本会自动启动 Worker：
# RELAY_CONTAINER_CLOUD_ENDPOINT=...
# RELAY_CONTAINER_CLOUD_TOKEN=...
# RELAY_CONTAINER_CLOUD_LOCAL_MODE=true  # 仅 ws:// 本地测试时需要

# 也可以临时覆盖配置，同时启动本地后端和容器云 Worker
./scripts/start_all.sh \
  --worker-local \
  --worker-token two-pc-test \
  --worker-endpoint ws://192.168.1.20:8787 \
  --worker-preset-dir /absolute/path/to/project
```

管理工作台默认地址为 <http://127.0.0.1:8080>。默认启动会自动判断数据库：项目默认数据库已可连接时直接复用，否则启动项目自己的本地 PostgreSQL；如果配置的是其他数据库地址，则不会尝试启动本地数据库。根目录 `.env` 同时配置了 Worker endpoint 和 pairing token 时，Worker 也会自动启动。`--skip-postgres`、`--worker` 及其他参数只用于临时覆盖或排障；完整参数可通过 `--help` 查看。两个服务和 Worker 仍保留各自目录中的独立启动方式，详见对应 README。

## 验证

两个服务保持独立依赖和测试配置，分别在子目录执行：

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

`agent-management` 还提供容器云消息接入 Worker，详见 [`agent-management/README.md`](agent-management/README.md#容器云消息-worker)。
