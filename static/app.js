// 私人助理 · 浏览器版（极简前端逻辑）
const chat = document.getElementById('chat');
const msg = document.getElementById('msg');
const model = document.getElementById('model');
const HISTORY = [];   // 浏览器内存里的会话历史

function addMsg(cls, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function send() {
  const text = msg.value.trim();
  if (!text) return;
  msg.value = '';
  addMsg('user', '你：' + text);
  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: HISTORY }),
    });
    const data = await r.json();
    if (data.raw_memory) addMsg('sys', '📒 原始记忆：' + data.raw_memory.replace(/\n/g, '；'));
    if (data.reply) {
      addMsg('assistant', (data.from || '助理') + '：' + data.reply);
      HISTORY.push({ role: 'user', content: text });
      HISTORY.push({ role: 'assistant', content: data.reply });
      if (HISTORY.length > 12) HISTORY.splice(0, HISTORY.length - 12);
    }
    if (data.memorized) addMsg('sys', '🧠 已摘记：' + data.memorized);
    if (data.tidied) addMsg('sys', '🧹 已整理：' + data.tidied);
  } catch (e) {
    addMsg('sys', '⚠ 网络出错：' + e);
  }
}

async function init() {
  try {
    const r = await fetch('/api/startup');
    const s = await r.json();
    model.textContent = '🧠 ' + s.model;
    addMsg('sys', '📋 主动汇报');
    addMsg('assistant', (s.assistant_name || '助理') + '：' + s.briefing);
    addMsg('sys', '（回车即可发送）');
  } catch (e) {
    addMsg('sys', '⚠ 连不上后端，请确认已运行 python server.py');
  }
}

async function pollReminders() {
  try {
    const r = await fetch('/api/reminders/due');
    const d = await r.json();
    for (const it of d.due || []) addMsg('sys', '⏰ 提醒时间到：' + it.text + '（' + it.time + '）');
  } catch (e) { /* 静默 */ }
}

document.getElementById('send').onclick = send;
msg.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
setInterval(pollReminders, 2000);
init();