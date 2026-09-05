# JobIntel

JobIntel 是一个面向中文求职场景的本地优先智能助手。它把候选人简历、真实职位发现、岗位结构化、匹配分析和持续追踪放在同一套工作流中，并提供 CLI、Web 页面和 FastMCP 三种入口。

当前版本首先支持通过用户自己的 Chrome 会话读取 BOSS 直聘公开展示的职位信息。项目默认采用低频串行请求、随机等待、详情缓存和雷达冷却时间，尽量降低对平台和账号的影响。

> JobIntel 与 BOSS 直聘无隶属或合作关系。使用者应遵守目标平台的服务条款、访问限制和适用法律。请勿用于绕过验证、大规模采集或骚扰招聘者。

## 已有功能

- 导入 PDF、Markdown 或纯文本简历，提取候选人经历与技能证据
- 在确认后保存候选人档案，保留来源和版本信息
- 从 BOSS 直聘发现真实职位，支持关键词、城市、薪资和数量筛选
- 按保守节奏抓取职位详情，并使用本地缓存减少重复访问
- 使用 Anthropic、OpenAI 或 DeepSeek 分析岗位匹配度
- 输出中文的优势、差距、证据引用、行动建议和推荐等级
- 保存发现批次和分析记录，支持列表查询与重新分析
- 配置职位雷达，按冷却时间检查新的匹配职位
- 提供本地 Web 页面，覆盖主要简历、发现、分析和雷达操作
- 通过 FastMCP 暴露与进程内工具箱一致的工具契约

当前不会自动向 HR 发消息，也不会代替用户投递。沟通文案和人工确认后的发送流程适合后续版本实现。

## 快速开始

要求 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 Chrome/Chromium。

```bash
git clone <your-repository-url>
cd jobintel
cp .env.example .env
uv sync --all-extras
uv run jobintel seed
```

在 `.env` 中填写要使用的模型服务密钥。默认示例使用 DeepSeek：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-key
```

启动 Web 页面：

```bash
uv run jobintel web
```

默认访问 `http://127.0.0.1:8000`。需要局域网访问时可使用：

```bash
uv run jobintel web --host 0.0.0.0 --port 8000
```

## 连接 BOSS 直聘

JobIntel 不读取你的日常 Chrome 配置，而是连接一个显式启用远程调试的独立浏览器实例。先退出占用相同调试配置的 Chrome，再运行：

Linux：

```bash
google-chrome --remote-debugging-port=9222 \
  --remote-allow-origins=http://127.0.0.1:9222 \
  --user-data-dir="$HOME/.jobintel/chrome-profile" \
  https://www.zhipin.com/web/user/
```

Windows PowerShell：

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --remote-allow-origins=http://127.0.0.1:9222 `
  --user-data-dir="$env:USERPROFILE\.jobintel\chrome-profile" `
  https://www.zhipin.com/web/user/
```

在打开的浏览器中登录后进行诊断：

```bash
uv run jobintel source-doctor
```

如果 JobIntel 部署在服务器，而 Chrome 运行在本机，可从本机建立反向 SSH 隧道：

```bash
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 127.0.0.1:9222:127.0.0.1:9222 user@server
```

服务器上的 `9222` 端口必须空闲，并且 SSH 服务端需要允许 TCP 转发。隧道应仅监听 `127.0.0.1`，不要把 Chrome 调试端口暴露到公网。

## 常用 CLI

查看全部入口：

```bash
uv run jobintel --help
```

导入并确认候选人档案：

```bash
uv run jobintel profile import ./resume.pdf --candidate-id C001
uv run jobintel profile show C001
uv run jobintel profile confirm C001
```

发现职位并分析排名靠前的结果：

```bash
uv run jobintel discover \
  --candidate-id C001 \
  --query "Python 后端" \
  --city 上海 \
  --salary-min 20 \
  --limit 50 \
  --analyze-top 3
```

查看记录和执行单个岗位分析：

```bash
uv run jobintel discovery list
uv run jobintel analysis list --candidate-id C001
uv run jobintel analyze --candidate-id C001 --job-id J001
```

职位雷达：

```bash
uv run jobintel radar check --candidate-id C001
uv run jobintel radar show --candidate-id C001
```

启动 MCP 服务：

```bash
uv run jobintel serve-mcp
```

## 配置

完整配置见 [`.env.example`](.env.example)。常用变量包括：

- `LLM_PROVIDER`：`deepseek`、`anthropic` 或 `openai`
- `JOBINTEL_DB_PATH`：本地 SQLite 数据库路径
- `DISCOVERY_CDP_PORT`：Chrome 调试端口
- `DISCOVERY_*_DELAY_SECONDS`：搜索页和详情页访问间隔
- `DISCOVERY_DETAIL_CACHE_HOURS`：职位详情缓存时间
- `RADAR_MIN_INTERVAL_HOURS`：同一雷达的最短检查间隔
- `AGENT_MAX_*`：模型循环、修复和工具调用上限

`.env`、SQLite 数据库和生成的简历预览均被 Git 忽略。请勿提交 API 密钥、浏览器配置或个人求职数据。

## 开发与打包

```bash
make install
make check
make package
```

`make check` 会运行 Ruff、mypy 严格类型检查和带分支覆盖率门槛的离线测试。`make package` 在检查通过后生成 wheel 和源码包。

项目采用 `src/` 布局，核心模块包括：

```text
src/jobintel/
├── agent/          # 模型循环、提示词和进程内工具箱
├── discovery/      # BOSS/CDP 连接器与发现服务
├── mcp_server/     # FastMCP 适配层
├── persistence/    # SQLite、迁移、仓储和种子数据
├── providers/      # Anthropic/OpenAI/DeepSeek 适配器
├── services/       # 简历、JD、分析、证据和雷达服务
└── web/            # FastAPI 应用与静态前端
```

第三方代码声明见 [`docs/THIRD_PARTY_NOTICES.md`](docs/THIRD_PARTY_NOTICES.md)。

## 许可证

[MIT](LICENSE)
