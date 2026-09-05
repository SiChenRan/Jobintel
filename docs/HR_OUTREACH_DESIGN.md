# HR 沟通草稿功能设计

- 状态：M9.3 已完成，M9.4 Ready for implementation
- 日期：2026-09-05
- 目标版本：M9

## 1. 目标

在一个职位已经完成 JobIntel 分析后，根据该职位的真实要求和候选人当前简历证据，生成适合首次联系招聘者的中文沟通材料：

1. 简短打招呼；
2. 针对岗位的自我介绍；
3. 一条自然、具体的交流问题；
4. 每项能力陈述对应的岗位 Requirement 和 Candidate Evidence；
5. 可编辑、复制、打开原职位页面和手动标记已发送的工作流。

核心价值不是生成一段通用套话，而是把“岗位为什么适合我”压缩成招聘者可以快速阅读、候选人能够为之负责的沟通内容。

## 2. 平台与产品边界

BOSS 直聘《用户协议》限制未经许可使用第三方工具接入服务，并将插件、外挂、爬虫或拟人程序等非正常方式列入受限行为。BOSS 自身的南北阁服务已经提供基于匹配情况生成个性化招呼语的能力。

参考：

- [BOSS 直聘用户协议](https://www.zhipin.com/web/common/protocol/protocol-2019-09-30.html)
- [南北阁服务协议](https://stardustlm.zhipin.com/agreement/protocol)

因此 M9 的发布边界为：

- JobIntel 生成和验证沟通草稿；
- 用户在 JobIntel 中逐字审核并编辑；
- JobIntel 可以复制文案、打开原始岗位链接；
- 用户回到平台亲自完成发送；
- JobIntel 只记录用户主动确认的“已手动发送”事件；
- 不通过 CDP 查找聊天按钮、填写输入框或点击发送；
- 不批量触达、不自动重试、不绕过验证。

只有获得平台公开发送 API、正式合作接口或明确书面授权后，才设计 M10 受控发送适配器。届时也必须保持逐岗位确认、默认关闭和严格配额。

## 3. 用户流程

```text
已保存的 JobAnalysis
        │
        ▼
选择语气和沟通重点
        │
        ▼
选择与岗位要求匹配的最小 Evidence 集
        │
        ▼
LLM 生成结构化 OutreachDraft
        │
        ▼
程序校验 Requirement / Evidence / Profile Version
        │
        ├── 失败：最多修复 2 次，不保存不可信草稿
        │
        ▼
页面展示文案、字数、证据标签和风险提示
        │
        ├── 用户编辑
        ├── 用户批准
        ├── 复制文案
        └── 打开 BOSS 原岗位页面
                │
                ▼
           用户亲自发送
                │
                ▼
        用户可手动标记已发送
```

生成文案不会触发任何 BOSS 网络请求。只有用户点击“打开岗位”时，浏览器才导航到已经保存的 `source_url`。

## 4. Domain Model

### 4.1 枚举

```python
class OutreachChannel(StrEnum):
    BOSS = "boss"


class OutreachTone(StrEnum):
    CONCISE = "concise"
    PROFESSIONAL = "professional"
    TECHNICAL = "technical"


class OutreachStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT_CONFIRMED = "sent_confirmed"
    DISMISSED = "dismissed"


class OutreachEventType(StrEnum):
    APPROVED = "approved"
    COPIED = "copied"
    OPENED = "opened"
    SENT_CONFIRMED = "sent_confirmed"
    DISMISSED = "dismissed"
```

状态不能任意回退。修改已批准草稿会生成新 revision，新 revision 重新从 `draft` 开始。

### 4.2 模型生成边界

```python
class OutreachClaimDraft(FrozenDomainModel):
    text: NonEmptyStr
    requirement_ids: tuple[NonEmptyStr, ...]
    evidence_ids: tuple[NonEmptyStr, ...]


class OutreachMessageDraft(FrozenDomainModel):
    salutation: NonEmptyStr
    motivation: NonEmptyStr
    claims: tuple[OutreachClaimDraft, ...]
    conversation_opener: NonEmptyStr
    closing: NonEmptyStr
```

模型不能生成以下字段：

- `outreach_id`、revision 和状态；
- Job/Profile/Analysis 身份；
- Provider、Prompt、Schema 版本；
- 创建和更新时间；
- 发送或批准状态；
- 任何平台操作指令。

### 4.3 程序最终模型

```python
class OutreachDraft(FrozenDomainModel):
    outreach_id: NonEmptyStr
    revision: int
    analysis_id: NonEmptyStr
    job_id: NonEmptyStr
    job_version: int
    candidate_id: NonEmptyStr
    profile_version: int
    channel: OutreachChannel
    tone: OutreachTone
    salutation: NonEmptyStr
    motivation: NonEmptyStr
    claims: tuple[OutreachClaim, ...]
    conversation_opener: NonEmptyStr
    closing: NonEmptyStr
    rendered_message: NonEmptyStr
    user_edited_message: str | None
    status: OutreachStatus
    provider: NonEmptyStr
    prompt_version: NonEmptyStr
    schema_version: NonEmptyStr
    provenance_digest: Sha256Hex
    created_at: UtcDateTime
    updated_at: UtcDateTime
```

`rendered_message` 由程序按 channel policy 拼接，而不是接受模型提供的最终整段文本。不同平台的长度限制以后由配置化 channel policy 管理，不在 Prompt 中硬编码未经验证的平台上限。

`copied` 和 `opened` 是审计事件，不是草稿状态，避免复制和打开岗位的先后顺序造成循环状态。当前实现位于 `src/jobintel/outreach/`，包含：

- `models.py`：严格模型、稳定 Outreach/Claim ID；
- `state.py`：批准、驳回、确认已发送的状态机及事件前置条件；
- `policy.py`：BOSS 本地产品限制和程序渲染；
- `prompts.py`：最小 Evidence 上下文和 Prompt Injection 分隔；
- `guardrail.py`：作用域、引用、称呼、中文和措辞校验。
- `finalizer.py`：程序控制的 ID、渲染、版本与 provenance digest；
- `service.py`：Provider 调用、bounded repair、持久化与人工审核事件。

## 5. Evidence 与内容规则

### 5.1 必须满足

- 每条能力、经历、成果和技能陈述至少引用一个 Candidate Evidence；
- 引用必须属于 Analysis 锁定的 `candidate_id + profile_version`；
- 引用的 Requirement 必须属于锁定的 `job_id + job_version`；
- Evidence 必须确实在对应 Requirement 的分析匹配或检索收据中出现；
- `missing` 的要求不能被描述为候选人已经掌握；
- 公司、职位和招聘者称呼来自保存的 Job/Discovery 数据；
- 未抓取到招聘者姓名时使用无姓名问候，不猜测称呼；
- 不编造年限、指标、公司名、学历、证书、薪资或到岗时间；
- 输出默认使用中文，并避免“精通”“专家”等超出证据强度的表述。

### 5.2 Prompt Injection 防护

JD、公司介绍和招聘者文本都视为不可信数据：

- 放入明确的数据分隔块；
- System Prompt 明确禁止执行其中的指令；
- 只允许模型返回 `OutreachMessageDraft` Schema；
- 不为生成服务暴露浏览器、网络、数据库写入或发送工具；
- Provider 只接收生成所需的最小 Requirement 和 Evidence 子集。

### 5.3 用户编辑

模型生成内容经过 Evidence Guardrail；自由编辑后的文本不能继续声称由系统完整验证。

UI 必须：

- 同时保留原生成版本和用户编辑版本；
- 将编辑后的内容标记为“用户已修改”；
- 在批准前再次展示真实性确认；
- 不把用户编辑文本自动回写 Candidate Evidence；
- 不在普通日志中记录完整消息内容。

## 6. Application Service

新增 `OutreachService`：

```python
generate(analysis_id, tone, focus_requirement_ids) -> OutreachDraft
revise(outreach_id, user_edited_message) -> OutreachDraft
approve(outreach_id, revision) -> OutreachDraft
record_copied(outreach_id, revision) -> OutreachEvent
record_opened(outreach_id, revision) -> OutreachEvent
confirm_sent(outreach_id, revision) -> OutreachEvent
dismiss(outreach_id, revision) -> OutreachEvent
```

职责：

1. 解析完整 `JobAnalysis`、`JobPosting` 和 `CandidateProfile`；
2. 默认选择 `matched/partial` 且有 Evidence 的高重要度 Requirement；
3. 构造最小模型上下文；
4. 调用现有 `LLMProvider.run_turn()`；
5. 校验结构、引用、版本、状态和措辞规则；
6. 最多执行 `OUTREACH_MAX_REPAIRS=2` 次修复；
7. 生成程序控制的 ID、digest、时间和版本；
8. 原子保存草稿、引用和事件。

若 Analysis 的推荐为 `skip`，仍允许用户主动生成，但 UI 和 CLI 必须显示明显警告，且不能默认推荐该动作。

## 7. Persistence

新增 migration 6：`jobintel_reviewed_hr_outreach`。

### `outreach_drafts`

- `outreach_id + revision` 主键；
- Analysis、Job Version、Candidate Profile Version 外键；
- channel、tone、status；
- 结构化生成字段、程序渲染文本、用户编辑文本；
- prompt/schema/provenance 版本和 digest；
- `created_at`、`updated_at`。

### `outreach_claims` / `outreach_claim_requirements` / `outreach_claim_evidence`

- Claim 文本、Requirement 引用和 Candidate Evidence 引用分别规范化保存；
- Requirement 和 Candidate Evidence 使用版本化外键；
- 保留展示顺序。

### `outreach_events`

- 程序生成的 `event_id`；
- `outreach_id + revision`；
- `approved`、`copied`、`opened`、`sent_confirmed`、`dismissed`；
- 时间和最小 metadata；
- 不存 Cookie、页面 DOM、截图或聊天响应正文。

数据库只在本地保存。删除 Candidate Profile 前必须继续受外键约束保护。

## 8. CLI

新增命令组：

```bash
jobintel outreach generate --analysis-id <id> --tone professional
jobintel outreach show <id> [--revision 1]
jobintel outreach list --candidate-id <id>
jobintel outreach revise <id> --message-file <path>
jobintel outreach approve <id> --revision 1
jobintel outreach mark-copied <id> --revision 1
jobintel outreach mark-opened <id> --revision 1
jobintel outreach mark-sent <id> --revision 1
jobintel outreach dismiss <id> --revision 1
```

CLI 的 `generate` 和 `show` 输出必须同时展示：

- 最终文案；
- 字符数；
- Requirement → Evidence 映射；
- 是否经过用户编辑；
- “请在目标平台人工审核并发送”的提示。

不提供 `send`、`send-all` 或后台定时发送命令。

## 9. Web UI 与 API

### 页面

在 Analysis 详情页增加“生成沟通文案”：

- 语气：简洁、专业、技术；
- 可选沟通重点；
- 完整文案编辑区，保留原结构化生成内容；
- Requirement/Evidence 标签，可展开查看原始证据；
- 字数统计和不实内容提示；
- “批准”“复制文案”“打开原岗位”“标记已手动发送”；
- 不出现会让用户误以为系统自动发送的按钮。

### API

```text
POST /api/analyses/{analysis_id}/outreach-drafts
GET  /api/outreach-drafts
GET  /api/outreach-drafts/{outreach_id}
POST /api/outreach-drafts/{outreach_id}/revisions
POST /api/outreach-drafts/{outreach_id}/approve
POST /api/outreach-drafts/{outreach_id}/events/copied
POST /api/outreach-drafts/{outreach_id}/events/opened
POST /api/outreach-drafts/{outreach_id}/events/sent-confirmed
POST /api/outreach-drafts/{outreach_id}/dismiss
```

所有状态变更都要求 revision，过期 revision 返回 `409`，避免多个页面覆盖彼此的编辑。

## 10. 测试与发布门禁

### Unit

- 稳定 `outreach_id` 和 revision；
- 状态机合法/非法转换；
- channel policy 渲染和长度检查；
- 无招聘者姓名时的称呼降级；
- `skip` 分析警告；
- JD Prompt Injection 不会改变结构化输出边界。

### Guardrail

- 拒绝未知 Requirement/Evidence；
- 拒绝跨 Job Version/Profile Version 引用；
- 拒绝为 `missing` Requirement 编造正向能力；
- 拒绝无 Evidence 的经历或结果陈述；
- repair 耗尽后不保存草稿。

### Repository/API/UI

- migration 6 checksum 和升级路径；
- 草稿、引用、事件原子保存及幂等读取；
- revision 并发冲突；
- API 错误结构；
- 用户文本 HTML escaping；
- 复制和打开链接事件不触发 BOSS 后台请求；
- 测试套件不依赖真实 API Key、网络或浏览器。

### 发布指标

- 20 个匿名化案例全部输出中文；
- 不支持的事实陈述为 0；
- Requirement/Evidence 引用有效率 100%；
- 低匹配岗位不夸大候选人能力；
- 缺失招聘者姓名时不生成虚假称呼；
- 所有外部发送均由用户在平台内亲自完成。

## 11. 实施顺序

### M9.1 Domain 与 Guardrail

- Outreach models、ID、状态机和 channel policy；
- Outreach Prompt、结构化 draft 和 Evidence Guardrail；
- 完整离线单元测试。

状态：已完成。M9.1 不调用 LLM、不写数据库，也不访问 BOSS；Provider 调用、repair 和最终草稿物化属于 M9.2。

### M9.2 Persistence 与 Service

- migration 6 和 Repository；
- `OutreachService`、repair 与事件审计；
- CLI 命令组。

状态：已完成。实现 migration 6、规范化引用、Provider terminal tool、最多两次自动修复、程序最终化、草稿 revision 和原子状态事件。

### M9.3 Web 工作流

- Analysis 详情入口；
- 编辑、证据展开、批准、复制、打开岗位和手动发送确认；
- XSS、并发 revision 和失败恢复测试。

状态：已完成。Analysis 详情页已支持生成、编辑、证据展开、批准、复制、打开岗位、放弃和人工发送确认；动态文本统一转义，外部链接限制为 HTTP(S)，过期 revision 返回 409 并刷新当前草稿。

### M9.4 Evaluation 与发布

- 20 个匿名化案例；
- DeepSeek、Anthropic、OpenAI 的结构兼容测试；
- 隐私和日志审计；
- 文档、迁移和全新安装验证。

### M10 官方受控发送（条件性）

只有平台提供官方能力或明确授权后启动。发送适配器必须与草稿生成分离，并具备逐条确认、每日限额、幂等键、重复联系人去重、立即熔断和完整审计。
