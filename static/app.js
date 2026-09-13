// 私人助理 · 浏览器版 v0.2（多用户：登录 + token + 数据隔离）
const chat = document.getElementById('chat');
const msg = document.getElementById('msg');
const model = document.getElementById('model');
const loginBox = document.getElementById('login');
const HISTORY = [];   // 当前会话历史（随登录用户重置）
let TOKEN = localStorage.getItem('pa_token') || '';

function addMsg(cls, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function authHeaders() {
  return { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN };
}

async function api(path, options = {}) {
  const r = await fetch(path, { ...options, headers: { ...(options.headers || {}), ...authHeaders() } });
  if (r.status === 401) {
    logout(true);
    throw new Error('请先登录');
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.status);
  return data;
}

function showLogin() {
  loginBox.classList.remove('hidden');
  document.getElementById('loginname').focus();
}

async function doLogin() {
  const username = document.getElementById('loginname').value.trim();
  const password = document.getElementById('loginpass').value;
  const err = document.getElementById('loginerr');
  err.textContent = '';
  if (!username) { err.textContent = '用户名不能为空'; return; }
  try {
    const r = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await r.json();
    if (!r.ok) { err.textContent = data.detail || '登录失败'; return; }
    TOKEN = data.token;
    localStorage.setItem('pa_token', TOKEN);
    HISTORY.length = 0;
    document.getElementById('loginpass').value = '';
    loginBox.classList.add('hidden');
    document.getElementById('logoutbtn').classList.remove('hidden');
    await init();
  } catch (e) {
    err.textContent = '网络错误：' + e;
  }
}

function logout(expired = false) {
  TOKEN = '';
  localStorage.removeItem('pa_token');
  HISTORY.length = 0;
  document.getElementById('logoutbtn').classList.add('hidden');
  if (expired) addMsg('sys', '⚠ 登录已过期，请重新登录');
  showLogin();
}

async function send() {
  const text = msg.value.trim();
  if (!text) return;
  msg.value = '';
  addMsg('user', '你：' + text);
  const bubble = addMsg('assistant', '助理：');
  let full = '';
  try {
    const r = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ message: text, history: HISTORY }),
    });
    if (r.status === 401) { logout(true); return; }
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || r.statusText);
    }
    // SSE 流式解析：data: {"kind":"...","text":"..."}
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        for (const line of raw.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          let evt;
          try { evt = JSON.parse(line.slice(6)); } catch (e2) { continue; }
          if (evt.kind === 'token') {
            full += evt.text;
            bubble.textContent += evt.text;
            chat.scrollTop = chat.scrollHeight;
          } else if (evt.kind === 'status') {
            addMsg('sys', '🛠 ' + evt.text);
          } else if (evt.kind === 'memorized') {
            addMsg('sys', '🧠 已摘记：' + evt.text);
          }
        }
      }
    }
    if (full) {
      HISTORY.push({ role: 'user', content: text });
      HISTORY.push({ role: 'assistant', content: full });
      if (HISTORY.length > 12) HISTORY.splice(0, HISTORY.length - 12);
    }
  } catch (e) {
    if (!String(e).includes('请先登录')) addMsg('sys', '⚠ ' + e);
  }
}

async function init() {
  chat.innerHTML = '';
  try {
    const s = await api('/api/startup');
    model.textContent = '🧠 ' + s.model + ' ｜ ' + s.username;
    addMsg('sys', '📋 主动汇报');
    addMsg('assistant', (s.assistant_name || '助理') + '：' + s.briefing);
    addMsg('sys', '（回车即可发送）');
    loginBox.classList.add('hidden');
  } catch (e) {
    showLogin();
  }
}

async function pollReminders() {
  if (!TOKEN) return;
  try {
    const d = await api('/api/reminders/due');
    for (const it of d.due || []) {
      if (it.report) addMsg('assistant', '📰 每日晨报\n' + it.report);
      else addMsg('sys', '⏰ 提醒时间到：' + it.text + '（' + it.time + '）');
    }
  } catch (e) { /* 静默 */ }
}

document.getElementById('send').onclick = send;
document.getElementById('loginbtn').onclick = doLogin;
document.getElementById('logoutbtn').onclick = () => logout(false);
msg.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
document.getElementById('loginname').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
document.getElementById('loginpass').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
setInterval(pollReminders, 2000);
if (TOKEN) init();
else showLogin();