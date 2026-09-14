"""
我的私人助理 · 桌面小界面（Tkinter，零依赖）
================================================
把终端版的私人助理装进一个气泡聊天窗口：
  · 消息气泡：你说的=右对齐浅蓝，助理=左对齐深灰，系统提示=居中灰字
  · 底部：输入框 + 发送按钮 + 🎤 语音（说话→识别） + 🔊 朗读开关
  · 提醒到点 ⏰ 会直接出现在聊天区；启动时同样有『主动汇报』
  · 关闭窗口时记忆上次大小，下次打开自动还原
技术：界面用 Python 自带的 tkinter（Canvas 气泡流）；脑子复用 personal_assistant.py 的 team，
      后台线程跑推理，界面永不卡死。
运行：python src/assistant_gui.py
     python src/assistant_gui.py --selftest   （无窗口冒烟测试）
"""

import sys
import os
import json
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
from tkinter import simpledialog

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
import speech as sp

# 语音能力探测（语音是可选增强：没装依赖按钮自动置灰，聊天照常用）
sp_ok, sp_reason = sp.speech_available()

MAX_HISTORY = 12
_history: list = []
_q: queue.Queue = queue.Queue()

# ---- 外观（想换风格只改这里）----
FONT = ("Microsoft YaHei UI", 12)
BG = "#17181c"            # 窗口主背景（近黑）
FG = "#e8e8e8"            # 正文浅色
FIELD_BG = "#26272c"      # 输入框/按钮背景
SYS_COLOR = "#8a93a0"     # 系统提示（灰）
USER_BUBBLE = "#1f6fb2"   # 你说话的气泡（蓝）
AI_BUBBLE = "#2c2e35"     # 助理说话的气泡（深灰）
TEXT_ON_USER = "#f0f6ff"  # 气泡里的文字（蓝色底上）
TEXT_ON_AI = "#eef0f4"    # 气泡里的文字（深灰底上）

root = tk.Tk()
root.title("🤵 我的私人助理")
root.configure(bg=BG)

# 应用图标：窗口标题栏 + Windows 任务栏都用 packaging/assistant.ico
def _res(name: str) -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
        return os.path.join(base, name)
    return os.path.join(PROJECT_ROOT, "packaging", name)

try:
    root.iconbitmap(_res("assistant.ico"))
except Exception:
    pass
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PersonalAssistant.1.0")
    except Exception:
        pass

# ---- 窗口大小记忆（关闭时写 runtime/gui_config.json，下次还原）----
_CFG = os.path.join(PROJECT_ROOT, "runtime", "gui_config.json")

def _load_geom() -> tuple:
    try:
        with open(_CFG, encoding="utf-8") as f:
            d = json.load(f)
        return int(d.get("w", 0)) or 0, int(d.get("h", 0)) or 0
    except Exception:
        return 0, 0

def _save_geom() -> None:
    try:
        os.makedirs(os.path.dirname(_CFG), exist_ok=True)
        with open(_CFG, "w", encoding="utf-8") as f:
            json.dump({"w": root.winfo_width(), "h": root.winfo_height()}, f)
    except Exception:
        pass

_w, _h = _load_geom()
root.geometry(f"{_w}x{_h}" if _w and _h else "760x640")
root.minsize(520, 420)

# ---- 顶部状态栏（一行，替代原来的启动刷屏）----
bar = tk.Label(root, bg=BG, fg=SYS_COLOR,
               text=f"🤵 私人助理 · 大脑：{pa.MODEL_SOURCE} · 语音：{'可用' if sp_ok else '未安装'}",
               font=("Microsoft YaHei UI", 9), pady=6)
bar.pack(side="top", fill="x")

# ---- 聊天气泡流（Canvas 承载 + Frame 自动高度，滚轮滚动）----
chat = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0)
chat.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 6))
_box = tk.Frame(chat, bg=BG)
_box_id = chat.create_window((0, 0), window=_box, anchor="nw")
chat.bind("<Configure>", lambda e: chat.itemconfigure(_box_id, width=e.width))
chat.bind_all("<MouseWheel>", lambda e: chat.yview_scroll(int(-e.delta / 120), "units"))

_ai_bubble = {"var": None}          # 正在打字机流出的助理气泡

def _auto_see() -> None:
    """更新滚动范围并滚到底（新消息/流式追加都要调用）。"""
    try:
        chat.update_idletasks()
        chat.configure(scrollregion=(0, 0, 0, _box.winfo_reqheight()))
        chat.yview_moveto(1.0)
    except Exception:
        pass

def _sys_line(text: str) -> None:
    """居中灰色小字（系统提示、状态）。"""
    lab = tk.Label(_box, text=text, bg=BG, fg=SYS_COLOR, font=("Microsoft YaHei UI", 9),
                   wraplength=600, justify="center")
    lab.pack(fill="x", pady=(1, 3))
    _auto_see()

def _bubble(side: str, text: str = "") -> tk.StringVar:
    """新建一条聊天气泡。side='r' 用户（右对齐蓝）/'l' 助理（左对齐深灰）。
    返回 StringVar 供打字机流式追加。"""
    outer = tk.Frame(_box, bg=BG)
    outer.pack(fill="x", pady=(3, 7))
    row = tk.Frame(outer, bg=BG)
    if side == "r":                       # 你的话：靠右
        row.pack(anchor="e")
        av = tk.Label(row, text="🙂", bg=BG, font=("Segoe UI Emoji", 13), fg=SYS_COLOR)
        av.pack(side="right", padx=(6, 0))
        var = tk.StringVar(value=text)
        lab = tk.Label(row, textvariable=var, bg=USER_BUBBLE, fg=TEXT_ON_USER,
                       font=FONT, wraplength=470, justify="left", padx=12, pady=8)
        lab.pack(side="right")
    else:                                 # 助理的话：靠左
        row.pack(anchor="w")
        av = tk.Label(row, text="🤖", bg=BG, font=("Segoe UI Emoji", 13), fg=SYS_COLOR)
        av.pack(side="left", padx=(0, 6))
        var = tk.StringVar(value=text)
        lab = tk.Label(row, textvariable=var, bg=AI_BUBBLE, fg=TEXT_ON_AI,
                       font=FONT, wraplength=470, justify="left", padx=12, pady=8)
        lab.pack(side="left")
    _auto_see()
    return var

# ---- 底部输入区：输入框 + 发送 + 🎤 语音 + 🔊 朗读 ----
bottom = tk.Frame(root, bg=BG)
bottom.pack(side="bottom", fill="x", padx=10, pady=(2, 10))
entry = tk.Entry(bottom, font=FONT, bg=FIELD_BG, fg=FG, insertbackground=FG,
                 relief="flat", highlightthickness=1, highlightbackground="#3a3d46",
                 highlightcolor="#4a7fb5")
entry.pack(side="left", fill="x", expand=True, ipady=7)
send_btn = tk.Button(bottom, text="发送", width=8, command=lambda: send(),
                     font=FONT, bg=FIELD_BG, fg=FG, activebackground="#3a3d46",
                     activeforeground=FG, relief="flat", cursor="hand2")
send_btn.pack(side="left", padx=(6, 0), ipady=6)

# ---- 语音：🎤 录音（说话→识别成文字） + 🔊 朗读开关（点开后才朗读助理的回答）----
_mic_stop = {"ev": None}          # 非空 = 正在录音/识别；主线程负责置空

mic_btn = tk.Button(bottom, text="🎤 说话", width=7, command=lambda: toggle_mic(),
                    font=FONT, bg=FIELD_BG, fg=FG, activebackground="#3a3d46",
                    activeforeground=FG, relief="flat", cursor="hand2")
mic_btn.pack(side="left", padx=(6, 0), ipady=6)

voice_on = tk.BooleanVar(value=False)
voice_check = tk.Checkbutton(bottom, text="🔊 朗读", variable=voice_on,
                             font=FONT, bg=BG, fg=FG, selectcolor=BG,
                             activebackground=BG, activeforeground=FG,
                             highlightthickness=0, bd=0, cursor="hand2")
voice_check.pack(side="left", padx=(6, 0))
if not sp_ok:
    mic_btn.config(state="disabled")
    voice_check.config(state="disabled")


def put(tag: str, text: str) -> None:
    """渲染一条完整消息（非流式）。tag: user / ai / sys。"""
    text = (text or "").strip()
    if not text:
        return
    if tag == "sys":
        _sys_line(text)
    elif tag == "user":
        _bubble("r", text)
    else:
        _bubble("l", text)


def send() -> None:
    if _mic_stop["ev"] is not None:
        put("sys", "（正在录音/识别…先点🛑停止，再发送）")
        return
    text = entry.get().strip()
    if not text:
        return
    if text.lower() in ("exit", "quit", "退出"):
        root.destroy()
        return
    entry.delete(0, tk.END)
    put("user", text)

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


def toggle_mic() -> None:
    """🎤 按钮：没在录 → 开录；在录 → 停并识别。识别结果回主线程填进输入框（不自动发送）。"""
    if not sp_ok:
        return
    if _mic_stop["ev"] is not None:            # 正在录音/识别 → 点停（顺带停掉可能在响的朗读）
        sp.stop_speak()
        _mic_stop["ev"].set()
        return
    ev = threading.Event()
    _mic_stop["ev"] = ev
    mic_btn.config(text="🛑 停止", bg="#c62828")
    put("sys", "🎙 正在听…（再说一遍之前点🛑停止）")
    threading.Thread(target=_voice_worker, args=(ev,), daemon=True).start()


def _voice_worker(ev: threading.Event) -> None:
    """后台线程：录音（阻塞到停）→ 本地识别 → 结果经 _q 队列回主线程。UI 一律不直接碰。"""
    try:
        wav = sp.record_audio(ev)
        _q.put(("vlog", "", "🧠 正在识别…（首次会加载语音模型，稍等片刻）"))
        if wav:
            text = sp.transcribe(wav)
            try:
                os.remove(wav)
            except Exception:
                pass
            if text:
                _q.put(("vtext", text, text))
            else:
                _q.put(("vlog", "", "（没听清，再说一遍试试～）"))
    except Exception as e:
        _q.put(("vlog", "", f"🎙 语音出错了：{e}"))
    finally:
        _q.put(("vidle", "", ""))


def poll() -> None:
    """主线程定时取后台流式事件并渲染（Tkinter 只在主线程改 UI）。"""
    try:
        kind, text, payload = _q.get_nowait()
        if kind == "error":
            put("sys", f"⚠ 出错了：{payload}")
        elif kind == "astart":
            _ai_bubble["var"] = _bubble("l")              # 开一个助理气泡
        elif kind == "tok":
            var = _ai_bubble["var"]
            if var is not None:
                var.set(var.get() + payload)              # 打字机逐字追加
            _auto_see()
        elif kind == "status":
            put("sys", f"🛠 {payload}")
        elif kind == "mem":
            put("sys", f"🧠 已摘记：{payload}")
        elif kind == "aend":
            if payload:
                _history.extend([HumanMessage(content=text), AIMessage(content=payload)])
                del _history[:-MAX_HISTORY]
                if voice_on.get():      # 🔊 朗读开关：后台生成并播放，不卡界面
                    threading.Thread(target=lambda: _q.put(("vlog", "", sp.speak(payload))),
                                     daemon=True).start()
            _ai_bubble["var"] = None
        elif kind == "vlog":
            put("sys", payload)
        elif kind == "vtext":
            entry.delete(0, tk.END)     # 识别结果：填进输入框等用户确认（回车即发）
            entry.insert(0, payload)
            entry.focus_set()
        elif kind == "vidle":
            _mic_stop["ev"] = None      # 录音/识别结束，按钮复位
            mic_btn.config(text="🎤 说话", bg=FIELD_BG)
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


def on_close() -> None:
    _save_geom()                       # 记窗口大小，下次还原
    try:
        import speech as _sp2
        _sp2.stop_speak()
        root.unbind_all("<MouseWheel>")
    except Exception:
        pass
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)
root.bind("<Return>", lambda e: send())
put("sys", "🤵 私人助理已就位：直接输入，或点 🎤 说话（回车发送；输入 退出 结束）")
put("ai", "助理（主动汇报）：" + pa.make_startup_briefing())

root.after(150, poll)
root.after(2000, check_reminders)
entry.focus_set()
root.mainloop()