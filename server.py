"""
私人助理 · 浏览器版 v0.2（FastAPI 后端 · 多用户隔离）
================================================
在 v0.1 基础上新增多用户：
  · POST /api/auth              注册/登录（用户名 + 可选密码），返回 token
  · 其余接口带上 Authorization: Bearer <token>，
    每个用户有自己独立的记忆/待办/提醒/日程（存 data/users/<name>/assistant_memory/）
  · 密码用 SHA-256 加盐哈希存 users.json；密码可留空（适合单人自用）
运行：python server.py → 浏览器打开 http://127.0.0.1:8000
"""

import os
import json
import hashlib
import secrets
import datetime
import threading

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from langchain_core.messages import AIMessage, HumanMessage

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
MAX_HISTORY = 12


def _ensure_key() -> None:
    """首次运行：没有 Key 就在终端引导输入，存到 deepseek_key.txt。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            val, _ = winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")
            if val:
                os.environ["DEEPSEEK_API_KEY"] = str(val).strip()
                return
    except Exception:
        pass
    key_file = os.path.join(BASE, "deepseek_key.txt")
    if os.path.exists(key_file):
        saved = open(key_file, encoding="utf-8").read().strip()
        if saved:
            os.environ["DEEPSEEK_API_KEY"] = saved
            return
    print("首次使用需要 DeepSeek API Key（免费注册：https://platform.deepseek.com）")
    key = input("请粘贴你的 Key 后回车：").strip()
    if not key:
        raise SystemExit("没填 Key，程序退出。")
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(key)
    os.environ["DEEPSEEK_API_KEY"] = key


_ensure_key()
import personal_assistant as pa

app = FastAPI(title="私人助理 · 浏览器版 v0.2（多用户）")
_users_lock = threading.Lock()
_sessions: dict[str, str] = {}          # token -> username（内存态，重启需重新登录）
_histories: dict[str, list] = {}        # username -> 会话历史


# ---------------- 用户管理 ----------------
def _load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        return json.load(open(USERS_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save_users(users: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def _hash_pwd(salt: str, pwd: str) -> str:
    return hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()


def _user_data_dir(username: str) -> str:
    return os.path.join(DATA_DIR, "users", username, "assistant_memory")


class AuthIn(BaseModel):
    username: str
    password: str = ""


@app.post("/api/auth")
def auth(body: AuthIn):
    name = body.username.strip()
    if not (name and name.isalnum()):
        raise HTTPException(400, "用户名只能由字母/数字组成，不能为空")
    with _users_lock:
        users = _load_users()
        if name not in users:
            salt = secrets.token_hex(8)
            users[name] = {"salt": salt, "hash": _hash_pwd(salt, body.password),
                           "created": datetime.datetime.now().isoformat(timespec="seconds")}
            _save_users(users)
            os.makedirs(_user_data_dir(name), exist_ok=True)
        else:
            salt = users[name]["salt"]
            if _hash_pwd(salt, body.password) != users[name]["hash"]:
                raise HTTPException(401, "密码不对")
    token = secrets.token_hex(16)
    with _users_lock:
        _sessions[token] = name
    return {"token": token, "username": name}


def _current_user(request: Request) -> str:
    """校验 Bearer token，并把这个线程的数据目录切到该用户目录。"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    token = auth_header[len("Bearer "):].strip()
    with _users_lock:
        name = _sessions.get(token)
    if not name:
        raise HTTPException(401, "登录已过期，请重新登录")
    pa.switch_user_dir(_user_data_dir(name))
    return name


# ---------------- 业务接口 ----------------
class ChatIn(BaseModel):
    message: str
    history: list = []      # [{"role": "user"/"assistant", "content": "..."}]


def _to_messages(history: list) -> list:
    msgs = []
    for h in history[-MAX_HISTORY:]:
        role, content = h.get("role"), h.get("content", "")
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
    return msgs


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    name = _current_user(request)
    text = body.message.strip()
    if not text:
        return {"reply": "（空的，说点什么吧）"}
    if text.lower() in ("exit", "quit", "退出"):
        return {"reply": "（浏览器版直接关页面即可，再见～）"}
    msgs = _to_messages(body.history) or list(_histories.get(name, []))
    result = pa.team.invoke({
        "messages": [HumanMessage(content=text)],
        "history": msgs,
        "memorized": "",
        "tidied": "",
    })
    final = ""
    for m in reversed(result["messages"]):
        if m.type == "ai" and not m.tool_calls and m.content:
            final = m.content
            break
    with _users_lock:
        _histories[name] = (msgs + [HumanMessage(content=text), AIMessage(content=final)])[-MAX_HISTORY:]
    raw = None
    if any(w in text for w in pa.RECALL_HINTS) or any(q in text for q in pa.ASK_INFO_HINTS):
        raw = str(pa.recall_all.invoke({}))
    return {
        "reply": final or "（我有点绕晕了，换个说法再问我一次？）",
        "from": pa.get_assistant_name() or "助理",
        "memorized": result.get("memorized", ""),
        "tidied": result.get("tidied", ""),
        "raw_memory": raw,
        "username": name,
    }


@app.get("/api/startup")
def startup(request: Request):
    name = _current_user(request)
    # 迁移照顾：老版本单用户数据在根目录 assistant_memory/（只在当前用户首次登录时提示迁移不强制）
    return {
        "briefing": pa.make_startup_briefing(),
        "model": pa.MODEL_SOURCE,
        "assistant_name": pa.get_assistant_name() or "助理",
        "username": name,
    }


@app.get("/api/reminders/due")
def reminders_due(request: Request):
    name = _current_user(request)
    now = datetime.datetime.now()
    with pa._remind_lock:
        items = pa.load_reminders()
        due = [it for it in items if it["at"] <= now]
        if due:
            pa.save_reminders([it for it in items if it["at"] > now])
    out = []
    for it in due:
        with pa._remind_lock:
            pa.fire_log().append(f"已在 {it['at'].strftime('%H:%M')} 提醒过用户：{it['text']}")
            del pa.fire_log()[:-5]
        out.append({"text": it["text"], "time": it["at"].strftime("%H:%M")})
    return {"due": out}


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("=" * 52)
    print("🌐 私人助理（浏览器版 v0.2 · 多用户）已启动")
    print("   请在浏览器打开：http://127.0.0.1:8000")
    print("   按 Ctrl+C 停止")
    print("=" * 52)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")