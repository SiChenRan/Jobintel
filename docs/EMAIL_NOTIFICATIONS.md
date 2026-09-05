# 职位邮件通知

JobIntel 可以把一个已保存搜索批次中的职位摘要和原始链接，发送到该批次所属候选人的接收邮箱。邮件发送使用 Python 标准库 SMTP，不依赖第三方邮件 SDK。

## 配置

在本机 `.env` 中填写：

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TRANSPORT=starttls
SMTP_USERNAME=your-account@example.com
SMTP_PASSWORD=your-smtp-password-or-app-code
SMTP_FROM_ADDRESS=your-account@example.com
SMTP_TIMEOUT_SECONDS=15
```

`SMTP_TRANSPORT` 支持：

- `starttls`：连接后升级为 TLS，通常使用 587 端口；
- `ssl`：建立 TLS 连接，通常使用 465 端口；
- `plain`：仅适用于无需认证的可信本地 SMTP relay，禁止携带用户名和密码。

具体服务器地址、端口和授权码获取方式以邮箱服务商的 SMTP 文档为准。不要把普通网页登录密码提交到 Git；`.env` 已被忽略。

以上配置是服务器统一使用的发件账号，不包含用户的接收邮箱。

## 设置接收邮箱

打开 Web 工作台的“候选人档案”页面，在“设置接收邮箱”中选择候选人并保存邮箱。每个 `candidate_id` 都有独立设置；修改档案版本不会丢失该设置。

Web 只返回脱敏地址，例如 `o***@example.com`。发送某个搜索批次时，后端会根据批次中的 `candidate_id` 读取收件人，发送请求不能临时指定或覆盖地址。

## 使用

Web 页面完成职位搜索后，点击结果上方的“发送邮件”。该候选人未设置邮箱或服务器 SMTP 未配置时，按钮不可用。

CLI 可以发送任意已保存批次：

```bash
uv run jobintel notify discovery <搜索批次ID>
uv run jobintel notify discovery <搜索批次ID> --limit 100 --json
```

CLI 同样根据搜索批次所属候选人读取已保存的邮箱，不能通过命令参数覆盖。邮件只包含职位名称、公司、地点、薪资、经验、学历和原始职位链接，不包含简历、候选人证据、Cookie 或 API Key。

数据库需要保存候选人的完整接收邮箱，以便后续发送；每次发送记录仅保存状态、职位数量、掩码地址、时间和主题 SHA-256，不保存邮件正文或 SMTP 凭据。

当前 Web 尚未提供登录和访问控制，因此这是候选人档案级的数据隔离，适合个人或受信任的内部部署。开放给互不信任的公网用户前，必须增加身份认证，并限制用户只能查看和修改自己名下的候选人档案。
