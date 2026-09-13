"""
我的私人助理 · 桌面小界面（Tkinter，零依赖）
================================================
把终端版的私人助理装进一个聊天窗口：
  · 上区：聊天记录（你=蓝，助理=黑，系统提示=灰）
  · 下区：输入框 + 发送按钮（回车也能发）
  · 提醒到点 ⏰ 会直接出现在聊天区
启动时同样有『主动汇报』。
技术：界面用 Python 自带的 tkinter；脑子复用 personal_assistant.py 的 team，
      后台线程跑推理，界面永不卡死。
运行：python src/assistant_gui.py
     python src/assistant_gui.py --selftest   （无窗口冒烟测试）
"""

import sys
import os
import queue
import threading
import datetime

# src/ 不在默认搜索路径：脚本方式运行（python src/assistant_gui.py）时 Python 会自动带上，
# 但以模块方式加载（python -m src.assistant_gui / import src.assistant_gui）时找不到
# personal_assistant，这里统一把本文件所在目录加进搜索路径，两种运行方式都能用。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --selftest：不弹窗口，验证 team 调用链路后退出
if "--selftest" in sys.argv:
    import personal_assistant as pa
    print("模型：", pa.MODEL_SOURCE)
    print("汇报：\n", pa.make_startup_briefing())
    r = pa.team.invoke({"messages": [{"type": "human", "content": "你好，简单打声招呼"}],
                        "history": [], "memorized": "", "tidied": ""})
    for m in reversed(r["messages"]):
        if m.type == "ai" and m.content:
            print("聊天席回答：", m.content)
            break
    print("Selftest OK")
    sys.exit(0)

from langchain_core.messages import AIMessage, HumanMessage
import tkinter as tk
from tkinter import scrolledtext, simpledialog


def ensure_key() -> None:
    """确保有 DeepSeek Key（import 内核前调用）：
    环境变量 → 注册表 → 程序旁 deepseek_key.txt；都没有就弹窗引导，保存后再继续。"""
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if k:
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as kh:
            val, _ = winreg.QueryValueEx(kh, "DEEPSEEK_API_KEY")
            if val:
                os.environ["DEEPSEEK_API_KEY"] = str(val).strip()
                return
    except Exception:
        pass
    base = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    key_file = os.path.join(base, "deepseek_key.txt")
    if os.path.exists(key_file):
        saved = open(key_file, encoding="utf-8").read().strip()
        if saved:
            os.environ["DEEPSEEK_API_KEY"] = saved
            return
    # 都没有 → 首次启动弹窗
    temp = tk.Tk()
    temp.withdraw()
    temp.attributes("-topmost", True)
    key = simpledialog.askstring(
        "首次使用 · 需要 DeepSeek Key",
        "请输入你的 DeepSeek API Key：\n（到 platform.deepseek.com 免费注册获取；\n输入后会保存到程序旁边，下次不用再填）",
        parent=temp, show="*")
    temp.destroy()
    if not key or not key.strip():
        sys.exit("未提供 API Key，程序退出。")
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(key.strip())
    os.environ["DEEPSEEK_API_KEY"] = key.strip()


ensure_key()
import personal_assistant as pa

MAX_HISTORY = 12
_history: list = []
_q: queue.Queue = queue.Queue()

# ---- 外观（想换风格只改这里）----
FONT = ("Microsoft YaHei UI", 12)
BG = "#1e1e1e"          # 窗口主背景（深色）
FG = "#e0e0e0"          # 正文浅色
FIELD_BG = "#2b2b2b"    # 输入框/按钮背景
USER_COLOR = "#4fc3f7"  # 你说的话（浅蓝）
AI_COLOR = "#f0f0f0"    # 助理的话（近白）
SYS_COLOR = "#9aa0a6"   # 系统提示（灰）

root = tk.Tk()
root.title("🤵 我的私人助理")
root.geometry("680x560")
root.configure(bg=BG)

# 应用图标：窗口标题栏 + Windows 任务栏都用 packaging/assistant.ico
# 打包成 exe 后资源在 _MEIPASS 临时解包目录里，所以要做兼容
def _res(name: str) -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
        return os.path.join(base, name)
    # 开发模式下图标位于整理后的 packaging/ 目录。
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "packaging", name)

_ICO = _res("assistant.ico")
try:
    root.iconbitmap(_ICO)
except Exception:
    pass
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PersonalAssistant.1.0")
    except Exception:
        pass

chat = scrolledtext.ScrolledText(root, wrap="word", font=FONT, state="disabled",
                                 bg=BG, fg=FG, insertbackground=FG,
                                 relief="flat", highlightthickness=0)
chat.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 4))
chat.tag_configure("user", foreground=USER_COLOR)
chat.tag_configure("ai", foreground=AI_COLOR)
chat.tag_configure("sys", foreground=SYS_COLOR)

bottom = tk.Frame(root, bg=BG)
bottom.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
entry = tk.Entry(bottom, font=FONT, bg=FIELD_BG, fg=FG, insertbackground=FG,
                 relief="flat", highlightthickness=0)
entry.pack(side="left", fill="x", expand=True, ipady=6)
send_btn = tk.Button(bottom, text="发送", width=8, command=lambda: send(),
                     font=FONT, bg=FIELD_BG, fg=FG, activebackground="#3d3d3d",
                     activeforeground=FG, relief="flat", cursor="hand2")
send_btn.pack(side="left", padx=(6, 0), ipady=4)


def put(tag: str, text: str) -> None:
    chat.configure(state="normal")
    chat.insert(tk.END, text + "\n", tag)
    chat.configure(state="disabled")
    chat.see(tk.END)


def send() -> None:
    text = entry.get().strip()
    if not text:
        return
    if text.lower() in ("exit", "quit", "退出"):
        root.destroy()
        return
    entry.delete(0, tk.END)
    put("user", f"你：{text}")


    def run():  # 包一层保证异常不炸线程；流式逐字发送
        try:
            full: list = []
            started = False
            for kind, payload in pa.stream_reply(text, list(_history)):
                if kind == "token":
                    if not started:
                        _q.put(("astart", text, ""))
                        started = True
                    full.append(payload)
                    _q.put(("tok", text, payload))
                elif kind == "status":
                    _q.put(("status", text, payload))
                elif kind == "memorized":
                    _q.put(("mem", text, payload))
            _q.put(("aend", text, "".join(full)))
        except Exception as e:
            _q.put(("error", text, str(e)))
    threading.Thread(target=run, daemon=True).start()
    # 记忆/回忆类命令直接先把原始记忆贴出来（沿用终端版的透明做法）
    if any(w in text for w in pa.RECALL_HINTS) or any(q in text for q in pa.ASK_INFO_HINTS):
        put("sys", "📒 原始记忆：" + str(pa.recall_all.invoke({})).replace("\n", "；"))


def poll() -> None:
    """主线程定时取后台流式事件并渲染（Tkinter 只在主线程改 UI）。"""
    try:
        kind, text, payload = _q.get_nowait()
        if kind == "error":
            put("sys", f"⚠ 出错了：{payload}")
        elif kind == "astart":
            put("ai", "助理：")
            chat.mark_set("ai_end", "end-1c lineend")   # 气泡插入点（打字机追加用）
        elif kind == "tok":
            chat.configure(state="normal")
            chat.insert("ai_end", payload, "ai")        # 在同一行气泡末尾逐字追加
            chat.configure(state="disabled")
            chat.see(tk.END)
        elif kind == "status":
            put("sys", f"🛠 {payload}")
        elif kind == "mem":
            put("sys", f"🧠 已摘记：{payload}")
        elif kind == "aend":
            if payload:
                _history.extend([HumanMessage(content=text), AIMessage(content=payload)])
                del _history[:-MAX_HISTORY]
    except queue.Empty:
        pass
    root.after(150, poll)


def check_reminders() -> None:
    """到点的提醒直接弹进聊天区（复用提醒存储，与终端版同一份数据）。"""
    now = datetime.datetime.now()
    due = []
    with pa._remind_lock:
        items = pa.load_reminders()
        due = [it for it in items if it["at"] <= now]
        if due:
            pa.save_reminders([it for it in items if it["at"] > now])
    for it in due:
        with pa._remind_lock:
            pa.FIRE_LOG.append(f"已在 {it['at'].strftime('%H:%M')} 提醒过用户：{it['text']}")
            del pa.FIRE_LOG[:-5]
        put("sys", f"⏰ 提醒时间到：{it['text']}（{it['at'].strftime('%H:%M')}）")
    rep = pa.morning_report_due()      # 每日晨报：到点自动推一次进聊天区
    if rep:
        put("ai", f"📰 每日晨报\n{rep}")
    root.after(2000, check_reminders)


root.bind("<Return>", lambda e: send())
put("sys", "=" * 40)
put("sys", "🤵 你的私人助理已就位")
put("sys", f"🧠 当前用脑：{pa.MODEL_SOURCE}")
put("sys", "=" * 40)
put("ai", "助理（主动汇报）：" + pa.make_startup_briefing())
put("sys", "（输入 退出 结束；回车即发送）")

root.after(150, poll)
root.after(2000, check_reminders)
entry.focus_set()
root.mainloop()
