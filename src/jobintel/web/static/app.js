const state = { dashboard: null, health: null, previewId: null, currentDiscovery: null };
const titles = {
  dashboard: ["OVERVIEW", "求职工作台"], profiles: ["CANDIDATE", "候选人档案"],
  discovery: ["DISCOVERY", "发现职位"], analyses: ["ANALYSIS", "深入分析"],
  radar: ["RADAR", "职位雷达"],
};
const labels = {
  strong_apply: "强烈建议申请", apply: "建议申请", low_priority: "低优先级", skip: "暂不建议申请",
  matched: "匹配", partial: "部分匹配", missing: "缺失证据",
  new: "新增", changed: "信息变化", unchanged: "未变化", closed: "疑似下线 / 本次未见",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
const formatDate = value => value ? new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";
const splitValues = value => value.split(/[,，]/).map(item => item.trim()).filter(Boolean);
const numberOrNull = value => value === "" ? null : Number(value);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || payload;
    throw new Error(detail.message || `请求失败 (${response.status})`);
  }
  return payload;
}

function busy(active, text = "正在处理…") {
  $("#loading-text").textContent = text;
  $("#loading").classList.toggle("active", active);
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
  const metrics = [["候选人", counts.candidates], ["真实职位", counts.jobs], ["搜索批次", counts.discoveries], ["深入分析", counts.analyses], ["雷达检查", counts.radar_checks]];
  $("#metrics").innerHTML = metrics.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
  $("#provider-name").textContent = state.health.provider;
  const ready = state.health.boss_browser_ready;
  $("#browser-dot").className = `status-dot ${ready ? "ready" : "error"}`;
  $("#browser-status").textContent = ready ? "浏览器连接正常" : "浏览器尚未连接";
  const recent = state.dashboard.discoveries;
  $("#recent-discoveries").innerHTML = recent.length ? recent.map(run => `<div class="compact-item"><span class="mini-icon">⌕</span><div><strong>${escapeHtml(run.query)} · ${escapeHtml(run.city || "全国")}</strong><small>${escapeHtml(run.candidate_id)} · ${run.hit_count} 个结果</small></div><time>${formatDate(run.created_at)}</time></div>`).join("") : "<div class='empty-state'>暂无搜索记录</div>";
}

function renderProfiles() {
  const profiles = (state.dashboard && state.dashboard.profiles) || [];
  $("#profile-list").innerHTML = profiles.length ? profiles.map(item => `<article class="profile-card"><header><h4>${escapeHtml(item.candidate_id)}</h4><span class="version">v${item.profile_version}</span></header><p>${escapeHtml(item.summary || "尚无摘要")}</p><div class="tags"><span class="tag">${item.evidence_count} 条证据</span><span class="tag">${formatDate(item.created_at)}</span></div></article>`).join("") : "<div class='empty-state'>暂无候选人档案</div>";
}

function fillSelectors() {
  const profiles = (state.dashboard && state.dashboard.profiles) || [];
  const options = profiles.map(item => `<option value="${escapeHtml(item.candidate_id)}">${escapeHtml(item.candidate_id)} · v${item.profile_version}</option>`).join("");
  $("#candidate-select").innerHTML = options || "<option value=''>请先创建档案</option>";
  const discoveries = (state.dashboard && state.dashboard.discoveries) || [];
  const radar = (state.dashboard && state.dashboard.radar_checks) || [];
  const seen = new Set();
  const baselines = [...radar.map(item => ({ run_id: item.run_id, query: "最新雷达结果", city: "" })), ...discoveries].filter(item => !seen.has(item.run_id) && seen.add(item.run_id));
  $("#radar-baseline").innerHTML = baselines.map(item => `<option value="${escapeHtml(item.run_id)}">${escapeHtml(item.query || "雷达检查")} · ${escapeHtml(item.city || "全部地区")} · ${item.run_id.slice(-8)}</option>`).join("") || "<option value=''>请先完成一次职位搜索</option>";
}

function renderProfilePreview(preview) {
  const evidence = preview.evidence.map((item, index) => `<article class="evidence"><header><strong>${index + 1}. ${escapeHtml(item.title)}</strong><span>${escapeHtml(item.evidence_type)}</span></header><p>${escapeHtml(item.content)}</p><div class="tags">${item.skills.map(skill => `<span class="tag">${escapeHtml(skill)}</span>`).join("")}</div></article>`).join("");
  $("#profile-preview").className = "preview-content";
  $("#profile-preview").innerHTML = `<div class="preview-summary"><strong>${escapeHtml(preview.candidate_id)} · 将创建 v${preview.profile_version}</strong><p>${escapeHtml(preview.summary)}</p></div><div class="evidence-list">${evidence}</div><button class="button primary full" id="confirm-profile">确认内容并创建新版本</button>`;
  $("#confirm-profile").addEventListener("click", confirmProfile);
}

async function confirmProfile() {
  if (!state.previewId || !confirm("确认后将创建不可变的候选人档案新版本，是否继续？")) return;
  busy(true, "正在保存候选人档案…");
  try {
    const profile = await api("/api/profiles/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ preview_id: state.previewId }) });
    toast(`已创建 ${profile.candidate_id} v${profile.profile_version}`);
    state.previewId = null;
    await loadDashboard();
    renderProfilePreview({ ...profile, evidence: profile.evidence });
    $("#confirm-profile").remove();
  } catch (error) { toast(error.message, true); } finally { busy(false); }
}

function discoveryPayload(form) {
  const data = new FormData(form);
  const employment = data.get("employment_type");
  return {
    candidate_id: data.get("candidate_id"), query: data.get("query"), city: data.get("city"),
    salary_min_k: numberOrNull(data.get("salary_min_k")), salary_max_k: numberOrNull(data.get("salary_max_k")),
    daily_salary_min_yuan: numberOrNull(data.get("daily_salary_min_yuan")), daily_salary_max_yuan: numberOrNull(data.get("daily_salary_max_yuan")),
    employment_types: employment ? [employment] : [], education_requirements: splitValues(data.get("education")),
    experience_requirements: splitValues(data.get("experience")), exclusions: splitValues(data.get("exclusions")),
    exclude_outsourcing: data.has("exclude_outsourcing"), exclude_training: data.has("exclude_training"), exclude_agency: data.has("exclude_agency"),
    strict_salary: data.has("strict_salary"), limit: Number(data.get("limit")), detail_top: Number(data.get("detail_top")),
  };
}

function renderDiscovery(run) {
  state.currentDiscovery = run;
  const failures = run.source_attempts.filter(item => item.status !== "success");
  const cards = run.hits.map((hit, index) => { const job = hit.job; const link = job.source_links[0]; return `<article class="job-card"><div class="rank-score">${hit.rank_score}</div><div><h4>${index + 1}. ${escapeHtml(job.title)}</h4><p class="company">${escapeHtml(job.company_name)}</p><div class="job-meta"><span>${escapeHtml(job.location || "地点未披露")}</span><span>${escapeHtml(job.salary_text || "薪资面议")}</span><span>${escapeHtml(job.experience || "经验不限")}</span><span>${escapeHtml(job.education || "学历不限")}</span></div><div class="tags">${hit.matched_terms.slice(0, 8).map(term => `<span class="tag">${escapeHtml(term)}</span>`).join("")}</div></div><a class="job-link" href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">打开 BOSS ↗</a></article>`; }).join("");
  $("#discovery-result").innerHTML = `<div class="result-header"><div><h3>${run.hits.length} 个匹配职位</h3><p>抓取 ${run.total_discovered} · 去重 ${run.duplicates_removed} · 过滤 ${run.filtered_out} · 批次 ${run.run_id.slice(-8)}</p></div><button class="button primary" id="analyze-current" ${run.hits.length ? "" : "disabled"}>深入分析前 3 个</button></div>${failures.length ? `<div class="preview-summary">来源异常：${escapeHtml(failures.map(item => item.message || item.status).join("；"))}</div>` : ""}<div class="job-list">${cards || "<div class='empty-state tall'>没有满足当前条件的职位</div>"}</div>`;
  const analyzeButton = $("#analyze-current");
  if (analyzeButton) analyzeButton.addEventListener("click", () => analyzeRun(run.run_id, 3));
}

async function analyzeRun(runId, top) {
  busy(true, `正在深入分析前 ${top} 个职位…`);
  try {
    const result = await api(`/api/discoveries/${encodeURIComponent(runId)}/analyze`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ top }) });
    const succeeded = result.analyses.filter(item => item.analysis).length;
    toast(`深入分析完成 ${succeeded}/${result.analyses.length}`);
    await loadDashboard();
    showView("analyses");
  } catch (error) { toast(error.message, true); } finally { busy(false); }
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
    const item = await api(`/api/analyses/${encodeURIComponent(id)}`);
    const requirementText = Object.fromEntries(item.job.requirements.map(req => [req.requirement_id, req.text]));
    const matches = item.requirement_matches.map(match => `<div class="match-row"><header><strong>${escapeHtml(requirementText[match.requirement_id] || match.requirement_id)}</strong><span class="recommendation">${labels[match.status] || match.status}</span></header><p>${escapeHtml(match.reason)}</p><div class="tags">${match.evidence_ids.map(evidence => `<span class="tag">证据 ${escapeHtml(evidence)}</span>`).join("") || "<span class='tag'>无引用证据</span>"}</div></div>`).join("");
    const list = (title, values, field = "text") => values.length ? `<h3>${title}</h3><ul>${values.map(value => `<li>${escapeHtml(value[field])}</li>`).join("")}</ul>` : "";
    $("#dialog-content").innerHTML = `<div class="dialog-body"><p class="eyebrow">ANALYSIS DETAIL</p><h2>${escapeHtml(item.job.title)} · ${item.score}/100</h2><p>${escapeHtml(item.job.company_name)} · ${escapeHtml(item.candidate_id)}@${item.profile_version}</p><span class="recommendation">${labels[item.recommendation] || item.recommendation}</span><h3>岗位要求逐项判断</h3>${matches}${list("有证据支持的优势", item.strengths)}${list("缺失能力", item.missing_skills, "skill")}${list("简历修改建议", item.resume_suggestions)}${list("面试准备重点", item.interview_topics)}<h3>建议的下一步</h3><p>${escapeHtml(item.next_action)}</p></div>`;
    $("#detail-dialog").showModal();
  } catch (error) { toast(error.message, true); } finally { busy(false); }
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
}
async function showRadar(id) { try { renderRadar(await api(`/api/radar/checks/${encodeURIComponent(id)}`)); } catch (error) { toast(error.message, true); } }

document.addEventListener("DOMContentLoaded", () => {
  $$("[data-view]").forEach(item => item.addEventListener("click", () => showView(item.dataset.view)));
  $$("[data-go]").forEach(item => item.addEventListener("click", () => showView(item.dataset.go)));
  $("#menu-button").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
  $("#refresh-button").addEventListener("click", () => loadDashboard().then(() => toast("数据已刷新")));
  $("#reload-analyses").addEventListener("click", loadAnalyses);
  $(".dialog-close").addEventListener("click", () => $("#detail-dialog").close());

  $("#profile-form").addEventListener("submit", async event => {
    event.preventDefault(); busy(true, "正在从简历提取可引用证据…");
    try { const result = await api("/api/profiles/preview", { method: "POST", body: new FormData(event.currentTarget) }); state.previewId = result.preview_id; renderProfilePreview(result.preview); toast("预览已生成，请审核后确认"); }
    catch (error) { toast(error.message, true); } finally { busy(false); }
  });
  $("#discovery-form").addEventListener("submit", async event => {
    event.preventDefault(); busy(true, "正在安全地搜索 BOSS，请勿重复提交…");
    try { const result = await api("/api/discoveries", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(discoveryPayload(event.currentTarget)) }); renderDiscovery(result.discovery); await loadDashboard(); toast(`找到 ${result.discovery.hits.length} 个匹配职位`); }
    catch (error) { toast(error.message, true); } finally { busy(false); }
  });
  $("#radar-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.currentTarget); busy(true, "正在执行低频雷达检查…");
    try { const check = await api("/api/radar/checks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ baseline_run_id: data.get("baseline_run_id"), detail_top: Number(data.get("detail_top")) }) }); renderRadar(check); await loadDashboard(); loadRadar(); toast("雷达检查已完成"); }
    catch (error) { toast(error.message, true); } finally { busy(false); }
  });
  loadDashboard();
});
