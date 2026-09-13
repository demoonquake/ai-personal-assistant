"""
私人助理 · 浏览器版（FastAPI 后端）
================================================
把终端版/窗口版的私人助理搬进浏览器：
  · POST /api/chat           聊天（复用同一个 team，功能全部保留）
  · GET  /api/startup        主动汇报（启动时浏览器拉取）
  · GET  /api/reminders/due  到点提醒（浏览器每 2 秒轮询，到点自动弹）
  · GET  /                   极简前端页面（static/ 下的 html+css+js）
运行：python server.py → 浏览器打开 http://127.0.0.1:8000
"""

import os
import datetime
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from langchain_core.messages import AIMessage, HumanMessage


def _ensure_key() -> None:
    """朋友电脑上首次运行：没有 Key 就在终端引导输入，存到 deepseek_key.txt。
    （顺序：环境变量 → 注册表 → key 文件 → 引导输入）"""
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
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek_key.txt")
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

BASE = os.path.dirname(os.path.abspath(__file__))
MAX_HISTORY = 12

app = FastAPI(title="私人助理 · 浏览器版")
_lock = threading.Lock()
HISTORY: list = []          # 服务端会话历史（单机单用户够用，重启即清）


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
def chat(body: ChatIn):
    global HISTORY
    text = body.message.strip()
    if not text:
        return {"reply": "（空的，说点什么吧）"}
    if text.lower() in ("exit", "quit", "退出"):
        return {"reply": "（浏览器版直接关页面即可，再见～）"}
    with _lock:
        msgs = _to_messages(body.history) or list(HISTORY)
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
        HISTORY = (msgs + [HumanMessage(content=text), AIMessage(content=final)])[-MAX_HISTORY:]
    # 回忆类命令额外附上原始记忆（沿用"让用户看到真相"的老规矩）
    raw = None
    if any(w in text for w in pa.RECALL_HINTS) or any(q in text for q in pa.ASK_INFO_HINTS):
        raw = str(pa.recall_all.invoke({}))
    return {
        "reply": final or "（我有点绕晕了，换个说法再问我一次？）",
        "from": pa.get_assistant_name() or "助理",
        "memorized": result.get("memorized", ""),
        "tidied": result.get("tidied", ""),
        "raw_memory": raw,
    }


@app.get("/api/startup")
def startup():
    return {
        "briefing": pa.make_startup_briefing(),
        "model": pa.MODEL_SOURCE,
        "assistant_name": pa.get_assistant_name() or "助理",
    }


@app.get("/api/reminders/due")
def reminders_due():
    """浏览器轮询：返回到点的提醒并从列表移除。"""
    now = datetime.datetime.now()
    with pa._remind_lock:
        items = pa.load_reminders()
        due = [it for it in items if it["at"] <= now]
        if due:
            pa.save_reminders([it for it in items if it["at"] > now])
    out = []
    for it in due:
        with pa._remind_lock:
            pa.FIRE_LOG.append(f"已在 {it['at'].strftime('%H:%M')} 提醒过用户：{it['text']}")
            del pa.FIRE_LOG[:-5]
        out.append({"text": it["text"], "time": it["at"].strftime("%H:%M")})
    return {"due": out}


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("=" * 52)
    print("🌐 私人助理（浏览器版）已启动")
    print("   请在浏览器打开：http://127.0.0.1:8000")
    print("   按 Ctrl+C 停止")
    print("=" * 52)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")