# JobIntel 实现状态与后续规划

> 最后更新：2026-09-05。本文记录当前仓库已经交付的能力和下一阶段开发边界。

## 1. 产品目标

JobIntel 面向中文求职者，将真实职位发现、个人经历证据、岗位匹配分析和持续追踪整合为一个本地优先的求职工作台。

可信边界保持不变：模型负责理解和起草，程序负责 ID、版本、评分、推荐阈值、证据校验、时间戳和持久化。所有会影响外部平台或招聘者的动作必须由用户明确确认。

## 2. 已完成里程碑

| 里程碑 | 状态 | 主要交付 |
|---|---|---|
| M0 工程基线 | 完成 | Python 3.12+、uv、Ruff、mypy strict、Pytest、Coverage、CI |
| M1 Domain 与 Scoring | 完成 | 版本化 Job/Profile、Requirement/Evidence、确定性评分与推荐 |
| M2 持久化 | 完成 | SQLite migration、seed、Repository、原子 Analysis 保存 |
| M3 Evidence 与 Guardrail | 完成 | 作用域证据检索、Provenance Ledger、确定性校验与 repair |
| M4 Tool Contract 与 MCP | 完成 | 单一 Tool Contract、进程内 Toolbox、FastMCP Adapter、Finalizer |
| M5 Parser 与 Agent | 完成 | JD Parser、Provider-neutral Agent Loop、中英文结构化分析 |
| M6 CLI 与工作流 | 完成 | seed、profile、analyze、analysis/discovery/radar 等命令组 |
| M6.5 BOSS Discovery | 完成 | Chrome CDP、真实职位发现、详情抓取、筛选、风险控制 |
| M6.6 求职档案与雷达 | 完成 | PDF/文本简历导入、确认、发现批次、增量雷达、重新分析 |
| M6.7 Web 工作台 | 完成 | 本地 FastAPI + 静态前端，覆盖核心候选人工作流 |
| M8 仓库清理与打包 | 完成 | 独立 JobIntel 包、三家 LLM Provider、公开仓库文档和构建配置 |

## 3. 当前架构

```text
CLI / Web / FastMCP
        │
        ▼
Application Services ───────── Discovery Service
        │                              │
        ├── JobIntel Agent             └── Chrome CDP → BOSS
        │      │
        │      ├── Tool Contracts / Toolbox
        │      ├── Provenance / Guardrail
        │      └── Anthropic / OpenAI / DeepSeek
        │
        └── SQLite Repository
               ├── Candidate Profiles / Evidence
               ├── Jobs / Requirements
               ├── Discovery Runs
               ├── Analyses / Matches
               └── Radar State
```

关键约束：

- `src/jobintel` 是唯一应用包；
- Provider SDK 类型不能进入 Agent 和业务服务；
- 进程内工具与 MCP 工具共享相同契约；
- 正向匹配结论必须引用当前候选人版本且支持该 Requirement 的 Evidence；
- BOSS 访问必须保留串行详情请求、随机等待、缓存和雷达冷却；
- `.env`、本地数据库、简历预览和 Chrome Profile 不进入版本控制。

## 4. 当前发布门禁

每次准备发布时必须完成：

```bash
make check
make package
```

验收标准：

- Ruff lint 与 format check 通过；
- mypy strict 通过；
- 离线测试全部通过，分支覆盖率不低于 85%；
- wheel 只包含 `jobintel`、Web 静态资源和版本化 seed；
- 全新虚拟环境中 `jobintel --help`、`jobintel seed` 可运行；
- 仓库中不存在 API Key、个人简历、浏览器配置或运行时数据库。

## 5. 下一阶段优先级

### P0：发布与真实用户验收

- 在干净机器上验证安装、初始化、Web 页面和 DeepSeek 分析；
- 用少量真实搜索检查 BOSS 页面变化、登录状态和异常提示；
- 补充 UI 内的新手引导、任务进度和失败恢复入口；
- 建立 10 个匿名化回归案例，覆盖简历、JD、评分和中文输出。

### P1：HR 沟通草稿（人工确认）

- 根据候选人证据和岗位要求生成短版打招呼、自我介绍与追问建议；
- 每条能力陈述必须引用 Candidate Evidence，禁止编造经历；
- 在 Web 页面展示可编辑草稿、证据来源和敏感内容警告；
- V1 只提供复制与人工发送，不自动点击或批量触达 HR。

### P2：受控发送实验

仅在 P1 通过真实用户验收后考虑：

- 发送前逐岗位显式确认，默认关闭；
- 严格日限额、随机冷却、重复联系人去重和完整审计记录；
- 遇到验证、风控或页面不确定状态立即停止；
- 不实现验证码绕过、指纹伪装或无人值守批量沟通。

### P3：多平台扩展

- 抽象并稳定 `JobSourceConnector` 契约；
- 新平台分别实现解析器、诊断、限速和合规说明；
- 通过 canonical URL 与职位指纹完成跨平台去重；
- 不因多平台扩展削弱任何现有证据和安全边界。

## 6. 暂不进入范围

- 无人值守批量投递或批量骚扰招聘者；
- 绕过验证码、登录保护或反自动化系统；
- 将用户简历、Cookie、API Key 或浏览历史上传到项目控制的服务；
- 让模型直接决定发送、评分、推荐或数据库身份；
- 为追求数量取消抓取节奏、缓存或熔断机制。
