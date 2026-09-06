const form = document.querySelector("#auth-form");
const loading = document.querySelector("#auth-loading");
const errorBox = document.querySelector("#auth-error");
const submitButton = document.querySelector("#auth-submit");
const switchButton = document.querySelector("#auth-switch");
const confirmField = document.querySelector("#confirm-password-field");
const displayNameField = document.querySelector("#display-name-field");
const emailField = document.querySelector("#email-field");
let setupRequired = false;
let mode = "login";

function errorMessage(payload, fallback) {
  const detail = payload.detail || payload;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail[0] && detail[0].msg ? detail[0].msg : fallback;
  return detail.message || fallback;
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorMessage(payload, `请求失败 (${response.status})`));
  return payload;
}

function setMode(nextMode) {
  mode = nextMode;
  form.dataset.mode = mode;
  const isSetup = mode === "setup";
  const isRegister = mode === "register";
  const needsNewPassword = isSetup || isRegister;
  document.querySelector("#auth-eyebrow").textContent = isSetup ? "首次设置" : isRegister ? "候选人注册" : "账户登录";
  document.querySelector("#auth-title").textContent = isSetup ? "创建管理员账户" : isRegister ? "创建候选人账号" : "登录 JobIntel";
  document.querySelector("#auth-copy").textContent = isSetup
    ? "当前实例尚无账户。请先创建项目管理员。"
    : isRegister ? "注册后系统会自动建立你的独立数据空间。" : "请输入你的账户信息。";
  displayNameField.hidden = !isRegister;
  emailField.hidden = !isRegister;
  displayNameField.querySelector("input").required = isRegister;
  emailField.querySelector("input").required = isRegister;
  confirmField.hidden = !needsNewPassword;
  confirmField.querySelector("input").required = needsNewPassword;
  form.elements.password.autocomplete = needsNewPassword ? "new-password" : "current-password";
  submitButton.textContent = isSetup ? "创建管理员并进入" : isRegister ? "注册并进入" : "登录";
  switchButton.hidden = isSetup;
  switchButton.textContent = isRegister ? "已有账号？返回登录" : "没有账号？注册";
  errorBox.textContent = "";
}

function showForm(status) {
  setupRequired = status.setup_required;
  loading.hidden = true;
  form.hidden = false;
  setMode(setupRequired ? "setup" : "login");
  form.elements.username.focus();
}

switchButton.addEventListener("click", () => {
  setMode(mode === "register" ? "login" : "register");
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  errorBox.textContent = "";
  const data = new FormData(form);
  const username = data.get("username");
  const password = data.get("password");
  if ((mode === "setup" || mode === "register") && password !== data.get("confirm_password")) {
    errorBox.textContent = "两次输入的密码不一致";
    return;
  }
  submitButton.disabled = true;
  submitButton.textContent = mode === "login" ? "正在登录…" : "正在创建…";
  try {
    const path = mode === "setup" ? "/api/auth/bootstrap" : mode === "register" ? "/api/auth/register" : "/api/auth/login";
    const payload = mode === "register"
      ? { username, password, display_name: data.get("display_name"), email: data.get("email") }
      : { username, password };
    await request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    window.location.replace("/");
  } catch (error) {
    errorBox.textContent = error.message;
    submitButton.disabled = false;
    submitButton.textContent = mode === "setup" ? "创建管理员并进入" : mode === "register" ? "注册并进入" : "登录";
  }
});

request("/api/auth/status")
  .then(status => {
    if (status.authenticated) window.location.replace("/");
    else showForm(status);
  })
  .catch(error => {
    loading.textContent = error.message;
    loading.classList.add("error");
  });
