const state = {
  dashboard: null, health: null, previewId: null, currentDiscovery: null,
  currentAnalysis: null, currentOutreach: null, csrfToken: null, user: null,
  environment: null,
};
const titles = {
  dashboard: ["概览", "求职工作台"], profiles: ["档案", "候选人档案"],
  discovery: ["职位", "发现职位"], analyses: ["分析", "深入分析"],
  radar: ["雷达", "职位雷达"], account: ["账户", "账户管理"],
  "admin-users": ["平台运营", "用户管理"],
  "admin-environment": ["系统中心", "运行配置"],
};
const labels = {
  strong_apply: "强烈建议申请", apply: "建议申请", low_priority: "低优先级", skip: "暂不建议申请",
  matched: "匹配", partial: "部分匹配", missing: "缺失证据",
  new: "新增", changed: "信息变化", unchanged: "未变化", closed: "疑似下线 / 本次未见",
  draft: "待审核", approved: "已批准", sent_confirmed: "已发送", dismissed: "已放弃",
  concise: "简洁", professional: "专业", technical: "技术",
};
const companySizeLabels = {
  micro: "0-20 人", small: "20-99 人", medium: "100-499 人",
  large: "500-999 人", very_large: "1000-9999 人", enterprise: "10000 人以上",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
const formatDate = value => value ? new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";
const splitValues = value => value.split(/[,，]/).map(item => item.trim()).filter(Boolean);
const numberOrNull = value => value === "" ? null : Number(value);
const safeExternalUrl = value => {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
};
const normalizeEyebrows = root => {
  const translations = {
    "ANALYSIS DETAIL": "分析详情", "HR OUTREACH": "HR 沟通", "LATEST CHECK": "本次检查",
  };
  $$(".eyebrow", root).forEach(item => { item.textContent = translations[item.textContent] || item.textContent; });
};
const secureExternalLinks = root => {
  $$('a[target="_blank"]', root).forEach(link => {
    const safe = safeExternalUrl(link.getAttribute("href"));
    if (safe) link.setAttribute("href", safe);
    else {
      link.removeAttribute("href");
      link.setAttribute("aria-disabled", "true");
    }
  });
};

async function api(path, options = {}) {
  const requestOptions = { ...options };
  const method = (requestOptions.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && state.csrfToken) {
    const headers = new Headers(requestOptions.headers || {});
    headers.set("X-CSRF-Token", state.csrfToken);
    requestOptions.headers = headers;
  }
  const response = await fetch(path, requestOptions);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      window.location.replace("/login");
    }
    const detail = payload.detail || payload;
    const error = new Error(detail.message || `请求失败 (${response.status})`);
    error.code = detail.code;
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function loadAuthentication() {
  const status = await api("/api/auth/status");
  if (!status.authenticated) {
    window.location.replace("/login");
    throw new Error("登录状态已失效");
  }
  state.csrfToken = status.csrf_token;
  state.user = status.user;
  document.body.classList.add(status.user.role === "admin" ? "role-admin" : "role-candidate");
  const roleLabel = status.user.role === "admin" ? "平台管理员" : "候选人账户";
  const avatarText = (status.user.display_name || status.user.username || "U").trim().slice(0, 1);
  $("#current-user").textContent = status.user.display_name;
  $("#current-role").textContent = roleLabel;
  $("#sidebar-user-name").textContent = status.user.display_name;
  $("#sidebar-user-role").textContent = roleLabel;
  $("#sidebar-avatar").textContent = avatarText;
  $("#top-avatar").textContent = avatarText;
  $("#account-summary").innerHTML = `<strong>${escapeHtml(status.user.display_name)}</strong><span>用户名：${escapeHtml(status.user.username)}</span><span>${status.user.role === "admin" ? "项目管理员" : `候选人编号：${escapeHtml(status.user.candidate_id)}`}</span>`;
  const profileForm = $("#account-profile-form");
  profileForm.elements.display_name.value = status.user.display_name;
  profileForm.elements.email.value = status.user.email || "";
  $("#admin-user-panel").hidden = status.user.role !== "admin";
  $$('[data-role="candidate"]').forEach(element => { element.hidden = status.user.role !== "candidate"; });
  if (status.user.role === "candidate") {
    const candidateInput = $("#profile-form").elements.candidate_id;
    candidateInput.value = status.user.candidate_id;
    candidateInput.readOnly = true;
  }
}

async function logout() {
  const button = $("#logout-button");
  button.disabled = true;
  try {
    await api("/api/auth/logout", { method: "POST" });
    state.csrfToken = null;
    window.location.replace("/login");
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
}

let activeTaskCount = 0;
function busy(active, text = "正在处理…") {
  activeTaskCount = Math.max(0, activeTaskCount + (active ? 1 : -1));
  $("#loading-count").textContent = activeTaskCount;
  $("#loading-text").textContent = active
    ? text
    : activeTaskCount > 0 ? `${activeTaskCount} 个任务仍在后台运行` : "任务已完成";
  $("#loading").classList.toggle("active", activeTaskCount > 0);
}

function setActionPending(button, pending, pendingText = "处理中…") {
  if (!button) return;
  if (pending) {
    button.dataset.idleText = button.textContent;
    button.textContent = pendingText;
  } else if (button.dataset.idleText) {
    button.textContent = button.dataset.idleText;
    delete button.dataset.idleText;
  }
  button.disabled = pending;
}

let toastTimer;
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.className = "toast", 3500);
}

function showView(name) {
  $$(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === name));
  $("#view-eyebrow").textContent = titles[name][0];
  $("#view-title").textContent = titles[name][1];
  $(".sidebar").classList.remove("open");
  if (name === "analyses") loadAnalyses();
  if (name === "radar") loadRadar();
  if (name === "admin-users") loadAdminUsers();
  if (name === "admin-environment") loadEnvironment();
}

async function loadDashboard() {
  try {
    const [dashboard, health] = await Promise.all([api("/api/dashboard"), api("/api/health")]);
    state.dashboard = dashboard;
    state.health = health;
    renderDashboard();
    renderProfiles();
    fillSelectors();
  } catch (error) { toast(error.message, true); }
}

function renderDashboard() {
  const counts = state.dashboard.counts;
  const metrics = [["候选人档案", counts.candidates], ["真实职位", counts.jobs], ["搜索批次", counts.discoveries], ["深入分析", counts.analyses], ["沟通草稿", counts.outreach_drafts], ["雷达检查", counts.radar_checks]];
  $("#metrics").innerHTML = metrics.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
  $("#provider-name").textContent = state.health.provider;
  const ready = state.health.boss_browser_ready;
  $("#browser-dot").className = `status-dot ${ready ? "ready" : "error"}`;
  $("#browser-status").textContent = ready ? "浏览器连接正常" : "浏览器尚未连接";
  const recent = state.dashboard.discoveries;
  const historyMarkup = recent.length ? recent.map(run => `<button class="compact-item history-item" data-discovery-run="${escapeHtml(run.run_id)}"><span class="mini-icon">⌕</span><div><strong>${escapeHtml(run.query)} · ${escapeHtml(run.city || "全国")}</strong><small>${escapeHtml(run.candidate_id)} · ${run.hit_count} 个结果</small></div><time>${formatDate(run.created_at)}</time></button>`).join("") : "<div class='empty-state'>暂无搜索记录</div>";
  $("#recent-discoveries").innerHTML = historyMarkup;
  $("#discovery-history").innerHTML = historyMarkup;
  $$('[data-discovery-run]').forEach(button => button.addEventListener("click", () => openDiscoveryRun(button.dataset.discoveryRun)));
}

async function openDiscoveryRun(runId) {
  busy(true, "正在读取历史搜索…");
  try {
    const run = await api(`/api/discoveries/${encodeURIComponent(runId)}`);
    renderDiscovery(run);
    showView("discovery");
    toast("已打开历史搜索快照");
  } catch (error) { toast(error.message, true); } finally { busy(false); }
}

function renderProfiles() {
  const profiles = (state.dashboard && state.dashboard.profiles) || [];
  $("#profile-list").innerHTML = profiles.length ? profiles.map(item => `<article class="profile-card"><header><h4>${escapeHtml(item.candidate_id)}</h4><span class="version">v${item.profile_version}</span></header><p>${escapeHtml(item.summary || "尚无摘要")}</p><div class="tags"><span class="tag">${item.evidence_count} 条证据</span><span class="tag">${formatDate(item.created_at)}</span><span class="tag">${item.email_notification_configured ? `邮箱 ${escapeHtml(item.recipient_masked)}` : "未设置通知邮箱"}</span></div></article>`).join("") : "<div class='empty-state'>暂无候选人档案</div>";
}

function renderNotificationEmailSetting() {
  const candidateId = $("#notification-candidate-select").value;
  const profile = ((state.dashboard && state.dashboard.profiles) || []).find(item => item.candidate_id === candidateId);
  const status = $("#notification-email-status");
  status.textContent = profile && profile.email_notification_configured ? "已设置" : "未设置";
  $("#notification-email-help").textContent = profile && profile.email_notification_configured
    ? `当前接收邮箱：${profile.recipient_masked}`
    : "填写后，该候选人的职位搜索结果将发送到此邮箱。";
}

function fillSelectors() {
  const profiles = (state.dashboard && state.dashboard.profiles) || [];
  const ownCandidate = state.user && state.user.role === "candidate" ? state.user.candidate_id : null;
  const options = profiles.length
    ? profiles.map(item => `<option value="${escapeHtml(item.candidate_id)}">${escapeHtml(item.candidate_id)} · v${item.profile_version}</option>`).join("")
    : ownCandidate ? `<option value="${escapeHtml(ownCandidate)}">${escapeHtml(ownCandidate)} · 尚未建立档案</option>` : "";
  $("#candidate-select").innerHTML = options || "<option value=''>请先创建档案</option>";
  const notificationSelect = $("#notification-candidate-select");
  const selectedCandidate = notificationSelect.value;
  notificationSelect.innerHTML = options || "<option value=''>请先创建档案</option>";
  if (profiles.some(item => item.candidate_id === selectedCandidate)) notificationSelect.value = selectedCandidate;
  renderNotificationEmailSetting();
  const discoveries = (state.dashboard && state.dashboard.discoveries) || [];
  const radar = (state.dashboard && state.dashboard.radar_checks) || [];
  const seen = new Set();
  const baselines = [...radar.map(item => ({ run_id: item.run_id, query: "最新雷达结果", city: "" })), ...discoveries].filter(item => !seen.has(item.run_id) && seen.add(item.run_id));
  $("#radar-baseline").innerHTML = baselines.map(item => `<option value="${escapeHtml(item.run_id)}">${escapeHtml(item.query || "雷达检查")} · ${escapeHtml(item.city || "全部地区")} · ${item.run_id.slice(-8)}</option>`).join("") || "<option value=''>请先完成一次职位搜索</option>";
}

async function loadAdminUsers() {
  try {
    const users = await api("/api/admin/users");
    const admins = users.filter(user => user.role === "admin");
    const candidates = users.filter(user => user.role === "candidate");
    const active = candidates.filter(user => user.is_active).length;
    const profiles = candidates.filter(user => user.profile_exists).length;
    $("#admin-user-summary").textContent = `${users.length} 个注册用户 · ${active} 个正常候选人 · ${candidates.length - active} 个已停用`;
    const adminMetricValues = [users.length, active, candidates.length - active, profiles];
    $$("#admin-metrics article strong").forEach((element, index) => { element.textContent = adminMetricValues[index]; });
    const renderUser = user => {
      const isAdmin = user.role === "admin";
      const status = user.account_status === "active" ? "正常" : "已停用";
      const profile = user.profile_exists ? `档案 v${user.profile_version}` : "尚未建立档案";
      const detail = isAdmin ? `项目管理员 · 用户名 ${escapeHtml(user.username)} · 最近登录 ${formatDate(user.last_login_at)}` : `用户名 ${escapeHtml(user.username)} · ${profile} · 注册于 ${formatDate(user.created_at)} · 最近登录 ${formatDate(user.last_login_at)}`;
      const searchText = escapeHtml(`${user.display_name} ${user.username} ${user.email || ""}`.toLocaleLowerCase("zh-CN"));
      const initial = escapeHtml((user.display_name || user.username).trim().slice(0, 1));
      const identity = `<div class="user-identity"><span class="user-avatar ${isAdmin ? "admin" : ""}">${initial}</span><div><strong>${escapeHtml(user.display_name)} <span class="status-chip ${user.is_active ? "" : "disabled"}">${status}</span></strong><small>${detail}</small><small>${escapeHtml(user.email || "未设置邮箱")}</small></div></div>`;
      if (isAdmin) return `<div class="user-row admin-user-row" data-user-search-text="${searchText}">${identity}<span class="role-chip">管理员</span></div>`;
      return `<div class="user-row candidate-user-row" data-user-row="${escapeHtml(user.user_id)}" data-user-search-text="${searchText}">${identity}<div class="user-edit-fields"><label>姓名<input data-user-display-name="${escapeHtml(user.user_id)}" value="${escapeHtml(user.display_name)}" maxlength="80"></label><label>邮箱<input data-user-email="${escapeHtml(user.user_id)}" value="${escapeHtml(user.email || "")}" type="email" maxlength="320"></label><button class="button secondary" data-user-save="${escapeHtml(user.user_id)}">保存资料</button></div><div class="user-actions"><button class="button ${user.is_active ? "danger-outline" : "success-outline"}" data-user-status="${escapeHtml(user.user_id)}" data-active="${user.is_active}">${user.is_active ? "停用账户" : "启用账户"}</button><label class="password-reset-field">临时密码<input type="password" minlength="10" maxlength="128" autocomplete="new-password" placeholder="至少 10 个字符" data-user-password="${escapeHtml(user.user_id)}"></label><button class="button secondary" data-user-reset="${escapeHtml(user.user_id)}">重置密码</button></div></div>`;
    };
    const groups = [
      ["管理员账号", admins],
      ["候选人", candidates],
    ].filter(([, items]) => items.length);
    $("#admin-user-list").innerHTML = groups.map(([title, items]) => `<div class="user-group-title">${title}<span>${items.length}</span></div>${items.map(renderUser).join("")}`).join("") || "<div class='empty-state'>暂无项目成员</div>";
    $$('[data-user-save]').forEach(button => button.addEventListener("click", () => saveAdminUser(button.dataset.userSave)));
    $$('[data-user-status]').forEach(button => button.addEventListener("click", () => setUserStatus(button.dataset.userStatus, button.dataset.active !== "true")));
    $$('[data-user-reset]').forEach(button => button.addEventListener("click", () => resetUserPassword(button.dataset.userReset)));
  } catch (error) { toast(error.message, true); }
}

async function loadEnvironment() {
  try {
    const config = await api("/api/admin/environment");
    state.environment = config;
    const form = $("#environment-form");
    const fields = [
      "llm_provider", "deepseek_model", "deepseek_base_url", "openai_model",
      "anthropic_model", "smtp_host", "smtp_port", "smtp_transport", "smtp_username",
      "smtp_from_address", "smtp_timeout_seconds", "discovery_cdp_port",
      "discovery_search_min_delay_seconds", "discovery_search_max_delay_seconds",
      "discovery_detail_min_delay_seconds", "discovery_detail_max_delay_seconds",
      "discovery_detail_cache_hours", "radar_min_interval_hours",
    ];
    fields.forEach(name => { form.elements[name].value = config[name] === null || config[name] === undefined ? "" : config[name]; });
    ["deepseek_api_key", "openai_api_key", "anthropic_api_key", "smtp_password"].forEach(name => { form.elements[name].value = ""; });
    $("#llm-config-status").textContent = `DeepSeek Key ${config.deepseek_api_key_configured ? "已配置" : "未配置"} · OpenAI Key ${config.openai_api_key_configured ? "已配置" : "未配置"} · Anthropic Key ${config.anthropic_api_key_configured ? "已配置" : "未配置"}`;
    $("#smtp-config-status").textContent = `${config.smtp_notification_ready ? "邮件服务已就绪" : "邮件服务未就绪"} · SMTP 授权码 ${config.smtp_password_configured ? "已配置" : "未配置"}`;
    $("#boss-config-status").textContent = config.boss_browser_ready ? "Chrome 调试连接正常" : "Chrome 调试连接未就绪";
    const setConfigBadge = (selector, ready, readyText = "已就绪", warningText = "待配置") => {
      const badge = $(selector);
      badge.textContent = ready ? readyText : warningText;
      badge.className = `config-state ${ready ? "ready" : "warning"}`;
    };
    setConfigBadge("#llm-config-badge", config.deepseek_api_key_configured || config.openai_api_key_configured || config.anthropic_api_key_configured);
    setConfigBadge("#smtp-config-badge", config.smtp_notification_ready);
    setConfigBadge("#boss-config-badge", config.boss_browser_ready, "连接正常", "未连接");
  } catch (error) { toast(error.message, true); }
}

function environmentPayload(form) {
  const data = new FormData(form);
  const numberFields = new Set([
    "smtp_port", "smtp_timeout_seconds", "discovery_cdp_port",
    "discovery_search_min_delay_seconds", "discovery_search_max_delay_seconds",
    "discovery_detail_min_delay_seconds", "discovery_detail_max_delay_seconds",
    "discovery_detail_cache_hours", "radar_min_interval_hours",
  ]);
  const payload = {};
  data.forEach((value, key) => {
    if (["deepseek_api_key", "openai_api_key", "anthropic_api_key", "smtp_password"].includes(key) && value === "") return;
    payload[key] = numberFields.has(key) ? Number(value) : value;
  });
  return payload;
}

async function saveAdminUser(userId) {
  const nameInput = $$('[data-user-display-name]').find(item => item.dataset.userDisplayName === userId);
  const emailInput = $$('[data-user-email]').find(item => item.dataset.userEmail === userId);
  busy(true, "正在保存用户资料…");
  try {
    await api(`/api/admin/users/${encodeURIComponent(userId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: nameInput.value, email: emailInput.value }),
    });
    await loadAdminUsers();
    toast("用户资料已更新");
  } catch (error) { toast(error.message, true); } finally { busy(false); }
}

async function setUserStatus(userId, isActive) {
  busy(true, isActive ? "正在启用账户…" : "正在停用账户…");
  try {
    await api(`/api/admin/users/${encodeURIComponent(userId)}/status`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: isActive }),
    });
    await loadAdminUsers();
    toast(isActive ? "账户已启用" : "账户已停用");
  } catch (error) { toast(error.message, true); } finally { busy(false); }
}

async function resetUserPassword(userId) {
  const input = $$('[data-user-password]').find(item => item.dataset.userPassword === userId);
  const password = input ? input.value : "";
  if (password.length < 10) { toast("密码至少需要 10 个字符", true); return; }
  busy(true, "正在重置密码…");
  try {
    await api(`/api/admin/users/${encodeURIComponent(userId)}/reset-password`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    input.value = "";
    toast("密码已重置，该用户的现有会话已注销");
  } catch (error) { toast(error.message, true); } finally { busy(false); }
}

function filterAdminUsers(query) {
  const normalized = query.trim().toLocaleLowerCase("zh-CN");
  $$('[data-user-search-text]').forEach(row => {
    row.hidden = normalized !== "" && !row.dataset.userSearchText.includes(normalized);
  });
}

function renderProfilePreview(preview) {
  const evidence = preview.evidence.map((item, index) => `<article class="evidence"><header><strong>${index + 1}. ${escapeHtml(item.title)}</strong><span>${escapeHtml(item.evidence_type)}</span></header><p>${escapeHtml(item.content)}</p><div class="tags">${item.skills.map(skill => `<span class="tag">${escapeHtml(skill)}</span>`).join("")}</div></article>`).join("");
  $("#profile-preview").className = "preview-content";
  $("#profile-preview").innerHTML = `<div class="preview-summary"><strong>${escapeHtml(preview.candidate_id)} · 将创建 v${preview.profile_version}</strong><p>${escapeHtml(preview.summary)}</p></div><div class="evidence-list">${evidence}</div><button class="button primary full" id="confirm-profile">确认内容并创建新版本</button>`;
  $("#confirm-profile").addEventListener("click", confirmProfile);
}

async function confirmProfile() {
  if (!state.previewId || !confirm("确认后将创建不可变的候选人档案新版本，是否继续？")) return;
  const actionButton = $("#confirm-profile");
  setActionPending(actionButton, true, "正在创建…");
  busy(true, "正在保存候选人档案…");
  try {
    const profile = await api("/api/profiles/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ preview_id: state.previewId }) });
    toast(`已创建 ${profile.candidate_id} v${profile.profile_version}`);
    state.previewId = null;
    await loadDashboard();
    renderProfilePreview({ ...profile, evidence: profile.evidence });
    $("#confirm-profile").remove();
  } catch (error) { toast(error.message, true); } finally {
    busy(false);
    setActionPending(actionButton, false);
  }
}

function discoveryPayload(form) {
  const data = new FormData(form);
  const employment = data.get("employment_type");
  const companySize = data.get("company_size");
  return {
    candidate_id: data.get("candidate_id"), query: data.get("query"), city: data.get("city"),
    salary_min_k: numberOrNull(data.get("salary_min_k")), salary_max_k: numberOrNull(data.get("salary_max_k")),
    daily_salary_min_yuan: numberOrNull(data.get("daily_salary_min_yuan")), daily_salary_max_yuan: numberOrNull(data.get("daily_salary_max_yuan")),
    employment_types: employment ? [employment] : [], company_sizes: companySize ? [companySize] : [], education_requirements: splitValues(data.get("education")),
    experience_requirements: splitValues(data.get("experience")), exclusions: splitValues(data.get("exclusions")),
    exclude_outsourcing: data.has("exclude_outsourcing"), exclude_training: data.has("exclude_training"), exclude_agency: data.has("exclude_agency"), smart_expand: data.has("smart_expand"),
    discovery_mode: data.get("discovery_mode"), prefer_new: data.has("prefer_new"), only_new: data.has("only_new"),
    strict_salary: data.has("strict_salary"), limit: Number(data.get("limit")), detail_top: Number(data.get("detail_top")),
  };
}

function describeDiscoveryFilters(preference) {
  const filters = [];
  const employmentLabels = { internship: "实习", full_time: "全职", part_time: "兼职", other: "其他" };
  const modeLabels = { search: "关键词搜索", recommendation: "首页推荐", hybrid: "混合发现" };
  filters.push(`采集：${modeLabels[preference.discovery_mode] || "关键词搜索"}`);
  if (preference.city) filters.push(`城市：${preference.city}`);
  (preference.employment_types || []).forEach(value => filters.push(`职位类型：${employmentLabels[value] || value}`));
  (preference.company_sizes || []).forEach(value => filters.push(`公司规模：${companySizeLabels[value] || value}`));
  if (preference.salary_min_k !== null) filters.push(`月薪不低于 ${preference.salary_min_k}K`);
  if (preference.salary_max_k !== null) filters.push(`月薪不高于 ${preference.salary_max_k}K`);
  if (preference.daily_salary_min_yuan !== null) filters.push(`日薪不低于 ${preference.daily_salary_min_yuan} 元`);
  if (preference.daily_salary_max_yuan !== null) filters.push(`日薪不高于 ${preference.daily_salary_max_yuan} 元`);
  (preference.education_requirements || []).forEach(value => filters.push(`学历：${value}`));
  (preference.experience_requirements || []).forEach(value => filters.push(`经验：${value}`));
  (preference.exclusions || []).forEach(value => filters.push(`排除：${value}`));
  if (preference.smart_expand) filters.push(`档案扩展：${(preference.expanded_queries || []).join("、") || "已启用"}`);
  if (!preference.include_undisclosed_salary) filters.push("排除薪资面议");
  if (preference.only_new) filters.push("仅显示未见岗位");
  else if (preference.prefer_new) filters.push("新岗位优先");
  return filters;
}

function renderRankExplanation(hit) {
  const breakdown = hit.rank_breakdown;
  if (!breakdown) {
    return `<div class="tags">${(hit.matched_terms || []).slice(0, 8).map(term => `<span class="tag">${escapeHtml(term)}</span>`).join("")}</div>`;
  }
  const queryTags = (hit.matched_query_terms || []).slice(0, 6).map(term => `<span class="tag query-tag">目标 · ${escapeHtml(term)}</span>`).join("");
  const profileTags = (hit.matched_profile_skills || []).slice(0, 8).map(term => `<span class="tag profile-tag">档案 · ${escapeHtml(term)}</span>`).join("");
  const evidence = (hit.matched_evidence || []).slice(0, 3).map(item => `${escapeHtml(item.title)}（${item.matched_terms.map(escapeHtml).join("、")}）`).join(" · ");
  return `<div class="rank-breakdown"><span>目标相关 ${breakdown.target_relevance}/40</span><span>档案技能 ${breakdown.profile_skills}/30</span><span>档案证据 ${breakdown.profile_evidence}/10</span><span>筛选契合 ${breakdown.preference_fit}/10</span><span>信息质量 ${breakdown.information_quality}/10</span></div><div class="tags">${queryTags}${profileTags}</div><p class="profile-evidence">${evidence ? `命中证据：${evidence}` : "当前档案没有命中可引用证据"}</p>`;
}

function renderDiscovery(run) {
  state.currentDiscovery = run;
  const failures = run.source_attempts.filter(item => item.status !== "success");
  const activeFilters = describeDiscoveryFilters(run.preference);
  const profile = run.profile_snapshot;
  const profileSummary = profile ? `<strong>档案 ${escapeHtml(profile.candidate_id)} · v${profile.profile_version} 已参与排序</strong><span>${profile.evidence_count} 条证据 · ${profile.skill_count} 项技能${profile.expansion_queries.length ? ` · 扩展搜索：${profile.expansion_queries.map(escapeHtml).join("、")}` : ""}</span>` : `<strong>档案 ${escapeHtml(run.preference.candidate_id)} 的旧搜索记录</strong>`;
  const channelLabels = { search: "关键词搜索", recommendation: "首页推荐" };
  const cards = run.hits.map((hit, index) => {
    const job = hit.job;
    const link = job.source_links[0];
    const size = companySizeLabels[job.company_size] || "规模未披露";
    const channels = (job.acquisition_channels || ["search"]).map(channel => `<span class="source-tag ${channel}">${channelLabels[channel] || channel}</span>`).join("");
    const freshness = hit.is_new_to_candidate ? '<span class="freshness-tag">首次发现</span>' : '<span class="freshness-tag seen">历史已见</span>';
    return `<article class="job-card"><div class="rank-score" title="候选人档案与目标岗位的可解释粗排分">${hit.rank_score}</div><div><div class="job-badges">${freshness}${channels}</div><h4>${index + 1}. ${escapeHtml(job.title)}</h4><p class="company">${escapeHtml(job.company_name)}</p><div class="job-meta"><span>${escapeHtml(job.location || "地点未披露")}</span><span>${escapeHtml(size)}</span><span>${escapeHtml(job.salary_text || "薪资面议")}</span><span>${escapeHtml(job.experience || "经验不限")}</span><span>${escapeHtml(job.education || "学历不限")}</span></div>${renderRankExplanation(hit)}</div><a class="job-link" href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">打开 BOSS ↗</a></article>`;
  }).join("");
  const newCount = run.hits.filter(hit => hit.is_new_to_candidate).length;
  $("#discovery-result").innerHTML = `<div class="result-header"><div><h3>${run.hits.length} 个匹配职位</h3><p>新增 ${newCount} · 历史已见 ${run.hits.length - newCount} · 抓取 ${run.total_discovered} · 去重 ${run.duplicates_removed} · 过滤 ${run.filtered_out} · 批次 ${run.run_id.slice(-8)}</p></div><button class="button primary" id="analyze-current" ${run.hits.length ? "" : "disabled"}>深入分析前 3 个</button></div><div class="filter-summary profile-summary">${profileSummary}<span>${activeFilters.length ? `当前筛选：${activeFilters.map(escapeHtml).join(" · ")}` : "未启用额外筛选"}</span></div>${failures.length ? `<div class="preview-summary">来源异常：${escapeHtml(failures.map(item => item.message || item.status).join("；"))}</div>` : ""}<div class="job-list">${cards || "<div class='empty-state tall'>没有满足当前条件的职位</div>"}</div>`;
  secureExternalLinks($("#discovery-result"));
  const analyzeButton = $("#analyze-current");
  if (analyzeButton) analyzeButton.addEventListener("click", () => analyzeRun(run.run_id, 3));
  const emailButton = document.createElement("button");
  emailButton.className = "button secondary";
  const candidateProfile = state.dashboard.profiles.find(item => item.candidate_id === run.preference.candidate_id);
  const smtpReady = state.health.smtp_notification_ready;
  const recipientReady = candidateProfile && candidateProfile.email_notification_configured;
  emailButton.disabled = !run.hits.length || !smtpReady || !recipientReady;
  if (!run.hits.length) {
    emailButton.textContent = "无职位可发送";
    emailButton.title = "当前搜索批次没有通过筛选的职位";
  } else if (!smtpReady) {
    emailButton.textContent = "邮件服务未就绪";
    emailButton.title = "邮件发送服务尚未配置";
  } else if (!recipientReady) {
    emailButton.textContent = "请先设置接收邮箱";
    emailButton.title = "请先为该候选人设置接收邮箱";
  } else emailButton.textContent = "发送邮件";
  emailButton.addEventListener("click", () => emailDiscovery(run, emailButton));
  $(".result-header", $("#discovery-result")).append(emailButton);
}

async function analyzeRun(runId, top) {
  const actionButton = $("#analyze-current");
  setActionPending(actionButton, true, "分析正在后台运行…");
  busy(true, `正在深入分析前 ${top} 个职位…`);
  try {
    const result = await api(`/api/discoveries/${encodeURIComponent(runId)}/analyze`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ top }) });
    const succeeded = result.analyses.filter(item => item.analysis).length;
    toast(`深入分析完成 ${succeeded}/${result.analyses.length}`);
    await loadDashboard();
    showView("analyses");
  } catch (error) { toast(error.message, true); } finally {
    busy(false);
    setActionPending(actionButton, false);
  }
}

async function emailDiscovery(run, actionButton = null) {
  setActionPending(actionButton, true, "正在发送…");
  busy(true, "正在发送职位通知…");
  try {
    const receipt = await api(`/api/discoveries/${encodeURIComponent(run.run_id)}/notifications/email`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ limit: run.hits.length }) });
    toast(`已发送 ${receipt.job_count} 个职位到 ${receipt.recipient_masked}`);
  } catch (error) { toast(error.message, true); } finally {
    busy(false);
    setActionPending(actionButton, false);
  }
}

async function loadAnalyses() {
  try {
    const analyses = await api("/api/analyses?limit=50");
    $("#analysis-list").innerHTML = analyses.length ? analyses.map(item => `<article class="analysis-card" data-analysis="${escapeHtml(item.analysis_id)}"><div class="score-ring" style="--score:${item.score}%" data-score="${item.score}"></div><h3>${escapeHtml(item.job.title)}</h3><p>${escapeHtml(item.job.company_name)} · ${escapeHtml(item.candidate_id)} · ${formatDate(item.created_at)}</p><span class="recommendation">${labels[item.recommendation] || item.recommendation}</span></article>`).join("") : "<div class='empty-state tall'>暂无深入分析结果。请先从职位搜索批次发起分析。</div>";
    $$("[data-analysis]").forEach(card => card.addEventListener("click", () => showAnalysis(card.dataset.analysis)));
  } catch (error) { toast(error.message, true); }
}

async function showAnalysis(id) {
  busy(true, "正在读取完整分析…");
  try {
    const [item, drafts] = await Promise.all([
      api(`/api/analyses/${encodeURIComponent(id)}`),
      api(`/api/outreach-drafts?analysis_id=${encodeURIComponent(id)}&limit=1`),
    ]);
    state.currentAnalysis = item;
    state.currentOutreach = drafts[0] || null;
    renderAnalysisDialog();
    $("#detail-dialog").showModal();
  } catch (error) { toast(error.message, true); } finally { busy(false); }
}

function analysisSummary(item) {
  const requirementText = Object.fromEntries(item.job.requirements.map(req => [req.requirement_id, req.text]));
  const hardScoring = item.scoring_version.includes("hard-requirements");
  const scoredIds = new Set(item.score_breakdown.scored_requirement_ids || []);
  const excludedIds = new Set(item.score_breakdown.excluded_requirement_ids || []);
  const matches = item.requirement_matches.map(match => { const scope = hardScoring ? (excludedIds.has(match.requirement_id) ? "不计分 · 定性参考" : "计入评分") : "旧版评分"; return `<div class="match-row"><header><strong>${escapeHtml(requirementText[match.requirement_id] || match.requirement_id)}</strong><span class="match-status-group"><span class="status-chip ${excludedIds.has(match.requirement_id) ? "muted" : ""}">${scope}</span><span class="recommendation">${labels[match.status] || match.status}</span></span></header><p>${escapeHtml(match.reason)}</p><div class="tags">${match.evidence_ids.map(evidence => `<span class="tag">证据 ${escapeHtml(evidence)}</span>`).join("") || "<span class='tag'>无引用证据</span>"}</div></div>`; }).join("");
  const list = (title, values, field = "text") => values.length ? `<h3>${title}</h3><ul>${values.map(value => `<li>${escapeHtml(value[field])}</li>`).join("")}</ul>` : "";
  const scoringScope = hardScoring ? `<p class="scoring-scope">本次分数仅计算 ${scoredIds.size} 项硬性要求；${excludedIds.size} 项职责或定性描述不计分。</p>` : `<p class="scoring-scope legacy">这是旧版评分，尚未区分硬性要求与定性描述。</p>`;
  return `<section class="analysis-summary"><p class="eyebrow">ANALYSIS DETAIL</p><h2>${escapeHtml(item.job.title)} · ${item.score}/100</h2><p>${escapeHtml(item.job.company_name)} · ${escapeHtml(item.candidate_id)}@${item.profile_version}</p><span class="recommendation">${labels[item.recommendation] || item.recommendation}</span>${scoringScope}<h3>岗位要求逐项判断</h3>${matches}${list("有证据支持的优势", item.strengths)}${list("缺失能力", item.missing_skills, "skill")}${list("简历修改建议", item.resume_suggestions)}${list("面试准备重点", item.interview_topics)}<h3>建议的下一步</h3><p>${escapeHtml(item.next_action)}</p></section>`;
}

function outreachGenerator(item) {
  const requirementText = Object.fromEntries(item.job.requirements.map(req => [req.requirement_id, req.text]));
  const eligible = item.requirement_matches.filter(match => ["matched", "partial"].includes(match.status) && match.evidence_ids.length);
  const focus = eligible.map(match => `<label class="focus-option"><input type="checkbox" name="focus_requirement_ids" value="${escapeHtml(match.requirement_id)}"><span><strong>${escapeHtml(requirementText[match.requirement_id] || match.requirement_id)}</strong><small>${labels[match.status]} · ${match.evidence_ids.length} 条证据</small></span></label>`).join("");
  const warning = item.recommendation === "skip" ? `<div class="inline-warning"><strong>匹配度较低</strong><span>建议先核对缺失能力，再决定是否联系。</span></div>` : "";
  return `<section class="outreach-workspace"><div class="outreach-heading"><div><p class="eyebrow">HR OUTREACH</p><h3>生成沟通草稿</h3></div></div>${warning}<form id="outreach-generate-form"><label>表达风格<select name="tone"><option value="professional">专业</option><option value="concise">简洁</option><option value="technical">技术</option></select></label><fieldset><legend>沟通重点（可选，最多 3 项）</legend><div class="focus-list">${focus || "<p class='muted-copy'>当前分析没有可引用的正向匹配证据。</p>"}</div></fieldset><button class="button primary" type="submit" ${eligible.length ? "" : "disabled"}>生成草稿</button></form></section>`;
}

function outreachCitations(payload) {
  return payload.citations.map(citation => `<details class="citation"><summary>${escapeHtml(citation.text)}</summary><div><h4>对应岗位要求</h4>${citation.requirements.map(item => `<p><span class="status-chip">${labels[item.match_status]}</span>${escapeHtml(item.text)}</p>`).join("")}<h4>简历证据</h4>${citation.evidence.map(item => `<article><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.content)}</p></article>`).join("")}</div></details>`).join("");
}

function outreachEditor(payload) {
  const draft = payload.outreach;
  const status = labels[draft.status] || draft.status;
  const terminal = ["sent_confirmed", "dismissed"].includes(draft.status);
  const canApprove = draft.status === "draft";
  const approved = draft.status === "approved";
  const sourceUrl = safeExternalUrl(payload.job.source_url);
  const events = (payload.events || []).map(item => `<span>${labels[item.event_type] || item.event_type} · ${formatDate(item.created_at)}</span>`).join("");
  return `<section class="outreach-workspace"><div class="outreach-heading"><div><p class="eyebrow">HR OUTREACH</p><h3>沟通草稿</h3></div><span class="outreach-status ${escapeHtml(draft.status)}">${status}</span></div><div class="draft-meta"><span>版本 ${draft.revision}</span><span>${labels[draft.tone] || draft.tone}</span><span>${draft.character_count}/500 字</span>${draft.is_user_edited ? "<span>人工修改</span>" : "<span>证据校验通过</span>"}</div><label class="message-editor">完整文案<textarea id="outreach-message" maxlength="500" ${terminal ? "readonly" : ""}>${escapeHtml(draft.effective_message)}</textarea><small><b id="outreach-char-count">${draft.character_count}</b>/500</small></label>${canApprove ? `<label class="truth-check"><input id="truth-confirm" type="checkbox">我已核对文案中的经历和能力陈述</label>` : ""}<div class="outreach-actions">${terminal ? "" : '<button class="button secondary" id="save-outreach">保存修改</button>'}${canApprove ? '<button class="button primary" id="approve-outreach" disabled>批准草稿</button><button class="text-button danger" id="dismiss-outreach">放弃</button>' : ""}${approved ? '<button class="button primary" id="copy-outreach">复制文案</button>' : ""}${approved && sourceUrl ? '<button class="button secondary" id="open-outreach-job">打开岗位 ↗</button>' : ""}${approved ? '<button class="button secondary" id="sent-outreach">标记已发送</button><button class="text-button danger" id="dismiss-outreach">放弃</button>' : ""}</div><div class="citation-list"><h4>引用依据</h4>${outreachCitations(payload)}</div>${events ? `<div class="event-history"><h4>操作记录</h4>${events}</div>` : ""}</section>`;
}

function renderAnalysisDialog() {
  const item = state.currentAnalysis;
  if (!item) return;
  $("#dialog-content").innerHTML = `<div class="dialog-body">${analysisSummary(item)}${state.currentOutreach ? outreachEditor(state.currentOutreach) : outreachGenerator(item)}</div>`;
  normalizeEyebrows($("#dialog-content"));
  bindOutreachActions();
}

async function refreshOutreach() {
  const draft = state.currentOutreach && state.currentOutreach.outreach;
  if (!draft) return;
  state.currentOutreach = await api(`/api/outreach-drafts/${encodeURIComponent(draft.outreach_id)}`);
  renderAnalysisDialog();
}

async function updateOutreach(path, body, message) {
  busy(true, "正在更新沟通草稿…");
  try {
    state.currentOutreach = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    renderAnalysisDialog();
    toast(message);
  } catch (error) {
    if (error.status === 409) {
      await refreshOutreach();
      toast("草稿已更新，请基于最新版本继续操作", true);
    } else toast(error.message, true);
  } finally { busy(false); }
}

function bindOutreachActions() {
  const generateForm = $("#outreach-generate-form");
  if (generateForm) generateForm.addEventListener("submit", async event => {
    event.preventDefault();
    const actionButton = event.submitter;
    const form = new FormData(event.currentTarget);
    const focus = form.getAll("focus_requirement_ids");
    if (focus.length > 3) { toast("沟通重点最多选择 3 项", true); return; }
    setActionPending(actionButton, true, "正在生成…");
    busy(true, "正在生成并校验沟通草稿…");
    try {
      state.currentOutreach = await api(`/api/analyses/${encodeURIComponent(state.currentAnalysis.analysis_id)}/outreach-drafts`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tone: form.get("tone"), focus_requirement_ids: focus }) });
      renderAnalysisDialog();
      toast("沟通草稿已生成");
    } catch (error) { toast(error.message, true); } finally {
      busy(false);
      setActionPending(actionButton, false);
    }
  });

  const editor = $("#outreach-message");
  if (editor) editor.addEventListener("input", () => { $("#outreach-char-count").textContent = editor.value.length; });
  const truth = $("#truth-confirm");
  if (truth) truth.addEventListener("change", () => { $("#approve-outreach").disabled = !truth.checked; });
  const draft = state.currentOutreach && state.currentOutreach.outreach;
  if (!draft) return;
  const base = `/api/outreach-drafts/${encodeURIComponent(draft.outreach_id)}`;
  const revision = draft.revision;
  const save = $("#save-outreach");
  if (save) save.addEventListener("click", () => {
    if (editor.value === draft.effective_message) { toast("文案没有修改"); return; }
    updateOutreach(`${base}/revisions`, { revision, message: editor.value }, "修改已保存为新版本");
  });
  const approve = $("#approve-outreach");
  if (approve) approve.addEventListener("click", () => updateOutreach(`${base}/approve`, { revision }, "草稿已批准"));
  const dismiss = $("#dismiss-outreach");
  if (dismiss) dismiss.addEventListener("click", () => updateOutreach(`${base}/dismiss`, { revision }, "草稿已放弃"));
  const copy = $("#copy-outreach");
  if (copy) copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(editor.value);
      await updateOutreach(`${base}/events/copied`, { revision }, "文案已复制");
    } catch (error) { toast(`复制失败：${error.message}`, true); }
  });
  const open = $("#open-outreach-job");
  if (open) open.addEventListener("click", () => {
    const url = safeExternalUrl(state.currentOutreach.job.source_url);
    if (!url) { toast("岗位链接不可用", true); return; }
    window.open(url, "_blank", "noopener,noreferrer");
    updateOutreach(`${base}/events/opened`, { revision }, "已打开岗位页面");
  });
  const sent = $("#sent-outreach");
  if (sent) sent.addEventListener("click", () => {
    if (!confirm("确认已经在招聘平台手动发送这份文案？")) return;
    updateOutreach(`${base}/events/sent-confirmed`, { revision }, "已记录为人工发送");
  });
}

async function loadRadar() {
  try {
    const checks = await api("/api/radar/checks?limit=20");
    $("#radar-list").innerHTML = checks.length ? checks.map(item => { const changed = item.events.filter(event => event.status !== "unchanged").length; return `<button class="compact-item text-button" data-radar="${escapeHtml(item.run_id)}"><span class="mini-icon">◉</span><div><strong>${changed} 项需要关注</strong><small>${item.run_id.slice(-10)}</small></div><time>${formatDate(item.created_at)}</time></button>`; }).join("") : "<div class='empty-state'>暂无雷达检查</div>";
    $$("[data-radar]").forEach(button => button.addEventListener("click", () => showRadar(button.dataset.radar)));
  } catch (error) { toast(error.message, true); }
}

function renderRadar(check) {
  const counts = Object.fromEntries(["new", "changed", "unchanged", "closed"].map(status => [status, check.events.filter(event => event.status === status).length]));
  const events = check.events.filter(event => event.status !== "unchanged").map(event => `<div class="radar-event"><span class="event-status ${event.status}">${labels[event.status]}</span><div><strong>${escapeHtml(event.job.title)}</strong><small>${escapeHtml(event.job.company_name)} · ${escapeHtml(event.job.salary_text || "薪资面议")}</small></div><a class="job-link" href="${escapeHtml(event.job.source_links[0].url)}" target="_blank" rel="noopener noreferrer">查看 ↗</a></div>`).join("");
  $("#radar-result").innerHTML = `<article class="panel"><div class="panel-heading"><div><p class="eyebrow">LATEST CHECK</p><h3>新增 ${counts.new} · 变化 ${counts.changed} · 疑似下线 ${counts.closed} · 未变化 ${counts.unchanged}</h3></div></div>${events || "<div class='empty-state'>本次没有需要关注的变化</div>"}</article>`;
  normalizeEyebrows($("#radar-result"));
  secureExternalLinks($("#radar-result"));
}
async function showRadar(id) { try { renderRadar(await api(`/api/radar/checks/${encodeURIComponent(id)}`)); } catch (error) { toast(error.message, true); } }

document.addEventListener("DOMContentLoaded", async () => {
  try {
    await loadAuthentication();
  } catch {
    return;
  }
  $$("[data-view]").forEach(item => item.addEventListener("click", () => showView(item.dataset.view)));
  $$("[data-go]").forEach(item => item.addEventListener("click", () => showView(item.dataset.go)));
  $("#menu-button").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
  $("#refresh-button").addEventListener("click", async () => {
    const activeElement = $(".view.active");
    const activeView = activeElement ? activeElement.id.replace("view-", "") : "";
    if (activeView === "admin-users") await loadAdminUsers();
    else if (activeView === "admin-environment") await loadEnvironment();
    else if (state.user.role === "candidate") await loadDashboard();
    toast("数据已刷新");
  });
  $("#logout-button").addEventListener("click", logout);
  $("#reload-analyses").addEventListener("click", loadAnalyses);
  $("#admin-user-search").addEventListener("input", event => filterAdminUsers(event.currentTarget.value));
  $("#admin-users-refresh").addEventListener("click", async () => {
    await loadAdminUsers();
    toast("用户数据已刷新");
  });
  $(".dialog-close").addEventListener("click", () => $("#detail-dialog").close());

  $("#change-password-form").addEventListener("submit", async event => {
    event.preventDefault();
    const actionButton = event.submitter;
    const form = event.currentTarget;
    const data = new FormData(form);
    if (data.get("new_password") !== data.get("confirm_password")) {
      toast("两次输入的新密码不一致", true);
      return;
    }
    setActionPending(actionButton, true, "正在更新…");
    busy(true, "正在更新密码…");
    try {
      const status = await api("/api/auth/change-password", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: data.get("current_password"), new_password: data.get("new_password") }),
      });
      state.csrfToken = status.csrf_token;
      form.reset();
      toast("密码已更新，其他设备已退出登录");
    } catch (error) { toast(error.message, true); } finally {
      busy(false);
      setActionPending(actionButton, false);
    }
  });

  $("#account-profile-form").addEventListener("submit", async event => {
    event.preventDefault();
    const actionButton = event.submitter;
    const form = event.currentTarget;
    const data = new FormData(form);
    setActionPending(actionButton, true, "正在保存…");
    busy(true, "正在保存个人资料…");
    try {
      const user = await api("/api/auth/profile", {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: data.get("display_name"), email: data.get("email") }),
      });
      state.user = user;
      const avatarText = (user.display_name || user.username || "U").trim().slice(0, 1);
      $("#current-user").textContent = user.display_name;
      $("#sidebar-user-name").textContent = user.display_name;
      $("#sidebar-avatar").textContent = avatarText;
      $("#top-avatar").textContent = avatarText;
      $("#account-summary").querySelector("strong").textContent = user.display_name;
      if (user.role === "admin") await loadAdminUsers();
      toast("个人资料已保存");
    } catch (error) { toast(error.message, true); } finally {
      busy(false);
      setActionPending(actionButton, false);
    }
  });

  $("#environment-form").addEventListener("submit", async event => {
    event.preventDefault();
    const actionButton = event.submitter;
    setActionPending(actionButton, true, "正在应用…");
    busy(true, "正在保存项目配置…");
    try {
      await api("/api/admin/environment", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(environmentPayload(event.currentTarget)),
      });
      await loadEnvironment();
      toast("项目配置已保存并应用");
    } catch (error) { toast(error.message, true); } finally {
      busy(false);
      setActionPending(actionButton, false);
    }
  });

  $("#profile-form").addEventListener("submit", async event => {
    event.preventDefault();
    const actionButton = event.submitter;
    setActionPending(actionButton, true, "正在解析…");
    busy(true, "正在从简历提取可引用证据…");
    try { const result = await api("/api/profiles/preview", { method: "POST", body: new FormData(event.currentTarget) }); state.previewId = result.preview_id; renderProfilePreview(result.preview); toast("预览已生成，请审核后确认"); }
    catch (error) { toast(error.message, true); } finally {
      busy(false);
      setActionPending(actionButton, false);
    }
  });
  $("#notification-candidate-select").addEventListener("change", renderNotificationEmailSetting);
  $("#notification-email-form").addEventListener("submit", async event => {
    event.preventDefault();
    const actionButton = event.submitter;
    const form = event.currentTarget;
    const data = new FormData(form);
    const candidateId = data.get("candidate_id");
    setActionPending(actionButton, true, "正在保存…");
    busy(true, "正在保存通知邮箱…");
    try {
      const result = await api(`/api/profiles/${encodeURIComponent(candidateId)}/notification-email`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recipient_email: data.get("recipient_email") }),
      });
      form.elements.recipient_email.value = "";
      await loadDashboard();
      toast(`已为 ${result.candidate_id} 保存邮箱 ${result.recipient_masked}`);
    } catch (error) { toast(error.message, true); } finally {
      busy(false);
      setActionPending(actionButton, false);
    }
  });
  $("#discovery-form").addEventListener("submit", async event => {
    event.preventDefault();
    const actionButton = event.submitter;
    setActionPending(actionButton, true, "正在后台发现…");
    busy(true, "正在发现 BOSS 职位，可继续使用其他功能");
    try { const result = await api("/api/discoveries", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(discoveryPayload(event.currentTarget)) }); renderDiscovery(result.discovery); await loadDashboard(); toast(`找到 ${result.discovery.hits.length} 个匹配职位`); }
    catch (error) { toast(error.message, true); } finally {
      busy(false);
      setActionPending(actionButton, false);
    }
  });
  $("#radar-form").addEventListener("submit", async event => {
    event.preventDefault();
    const actionButton = event.submitter;
    const data = new FormData(event.currentTarget);
    setActionPending(actionButton, true, "正在后台检查…");
    busy(true, "正在检查职位变化，可继续使用其他功能");
    try { const check = await api("/api/radar/checks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ baseline_run_id: data.get("baseline_run_id"), detail_top: Number(data.get("detail_top")) }) }); renderRadar(check); await loadDashboard(); loadRadar(); toast("雷达检查已完成"); }
    catch (error) { toast(error.message, true); } finally {
      busy(false);
      setActionPending(actionButton, false);
    }
  });
  if (state.user.role === "admin") {
    showView("admin-users");
  } else {
    loadDashboard();
  }
});
