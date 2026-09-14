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
import re
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
BG = "#0f1115"            # 窗口主背景（深邃近黑）
FG = "#e8e8e8"            # 正文浅色
FIELD_BG = "#1d2028"      # 输入框/按钮背景
SYS_COLOR = "#7b8494"     # 系统提示（灰）
USER_BUBBLE = "#2f7fd9"   # 你说话的气泡（亮蓝）
AI_BUBBLE = "#22252d"     # 助理说话的气泡（暖深灰）
TEXT_ON_USER = "#ffffff"  # 气泡里的文字（蓝色底上）
TEXT_ON_AI = "#eceff4"    # 气泡里的文字（深灰底上）
HEADER_BG = "#15181f"     # 顶部头部栏背景
ACCENT = "#2f7fd9"        # 主色（发送按钮/强调）
ACCENT_HI = "#3b8ce6"     # 主色高亮
TS_COLOR = "#5a6270"      # 消息时间戳
ONLINE = "#34d399"        # 在线状态点

def _circle(size: int, emoji: str, fill: str, bg: str = "") -> tk.Canvas:
    """画一个圆形头像（Canvas 圆 + 居中 emoji）。"""
    c = tk.Canvas(bg=bg or BG, width=size, height=size, highlightthickness=0)
    c.create_oval(0, 0, size, size, fill=fill, outline="")
    c.create_text(size / 2, size / 2 + 1, text=emoji, fill="#ffffff",
                  font=("Segoe UI Emoji", int(size * 0.44)))
    return c

root = tk.Tk()
root.title("我的私人助理")
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

# ---- 本地偏好（关闭时写 runtime/gui_config.json，下次还原：窗口大小 / 朗读音色语速）----
_CFG = os.path.join(PROJECT_ROOT, "runtime", "gui_config.json")

def _load_cfg() -> dict:
    try:
        with open(_CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cfg(**updates) -> None:
    try:
        os.makedirs(os.path.dirname(_CFG), exist_ok=True)
        d = _load_cfg()
        d.update(updates)
        with open(_CFG, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass

def _load_geom() -> tuple:
    d = _load_cfg()
    return int(d.get("w", 0)) or 0, int(d.get("h", 0)) or 0

_w, _h = _load_geom()
root.geometry(f"{_w}x{_h}" if _w and _h else "760x640")
root.minsize(520, 420)

# ---- 顶部头部：圆形头像 + 名称/在线状态 + 语音状态 ----
header = tk.Frame(root, bg=HEADER_BG)
header.pack(side="top", fill="x")
_left = tk.Frame(header, bg=HEADER_BG)
_left.pack(side="left", padx=12, pady=7)
_circle(34, "🤵", "#2f6fae", HEADER_BG).pack(side="left")
_titles = tk.Frame(_left, bg=HEADER_BG)
_titles.pack(side="left", padx=(10, 0))
tk.Label(_titles, text="我的私人助理", bg=HEADER_BG, fg="#f2f4f8",
         font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
tk.Label(_titles, text=f"● 在线 · {pa.MODEL_SOURCE}", bg=HEADER_BG, fg=ONLINE,
         font=("Microsoft YaHei UI", 9)).pack(anchor="w")
tk.Label(header, text=f"🎙 语音{' 可用' if sp_ok else ' 未安装'}",
         bg=HEADER_BG, fg=SYS_COLOR, font=("Microsoft YaHei UI", 9)).pack(side="right", padx=14)
tk.Frame(header, bg="#232733", height=1).pack(side="bottom", fill="x")   # 细分隔线

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
    带圆形头像与发送时间戳；返回 StringVar 供打字机流式追加。"""
    outer = tk.Frame(_box, bg=BG)
    outer.pack(fill="x", pady=(3, 9))
    row = tk.Frame(outer, bg=BG)
    if side == "r":                       # 你的话：靠右，头像在右
        row.pack(anchor="e")
        _circle(28, "🙂", "#2f6fae").pack(side="right")
        var = tk.StringVar(value=text)
        tk.Label(row, textvariable=var, bg=USER_BUBBLE, fg=TEXT_ON_USER,
                 font=FONT, wraplength=480, justify="left",
                 padx=14, pady=10).pack(side="right", padx=(0, 10))
        tk.Label(outer, text=datetime.datetime.now().strftime("%H:%M"),
                 bg=BG, fg=TS_COLOR, font=("Microsoft YaHei UI", 8)).pack(anchor="e", padx=12)
    else:                                 # 助理的话：靠左，头像在左
        row.pack(anchor="w")
        _circle(28, "🤖", "#3a4152").pack(side="left", padx=(0, 10))
        var = tk.StringVar(value=text)
        tk.Label(row, textvariable=var, bg=AI_BUBBLE, fg=TEXT_ON_AI,
                 font=FONT, wraplength=480, justify="left",
                 padx=14, pady=10).pack(side="left")
        tk.Label(outer, text=datetime.datetime.now().strftime("%H:%M"),
                 bg=BG, fg=TS_COLOR, font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=12)
    _auto_see()
    return var

# ---- 底部输入区（三行）：录音音量条 / 输入行 / 设置行 ----
bottom = tk.Frame(root, bg=BG)
bottom.pack(side="bottom", fill="x", padx=10, pady=(4, 10))

vol = tk.Canvas(bottom, bg=BG, height=6, highlightthickness=0)     # 录音时的实时音量条
vol.pack(side="top", fill="x", pady=(0, 2))
vol.create_rectangle(0, 0, 100000, 6, fill="#1a1d24", outline="")   # 轨道（暗色底）
_vol_rect = vol.create_rectangle(0, 0, 0, 6, fill="#3b8ce6", outline="")

row1 = tk.Frame(bottom, bg=BG)
row1.pack(side="top", fill="x")
entry = tk.Entry(row1, font=FONT, bg=FIELD_BG, fg=FG, insertbackground=FG,
                 relief="flat", highlightthickness=1, highlightbackground="#3a3d46",
                 highlightcolor="#4a7fb5")
entry.pack(side="left", fill="x", expand=True, ipady=7)
send_btn = tk.Button(row1, text="发送", width=8, command=lambda: send(),
                     font=FONT, bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HI,
                     activeforeground="#ffffff", relief="flat", cursor="hand2")
send_btn.pack(side="left", padx=(6, 0), ipady=6)

# ---- 语音：🎤 录音（说话→识别成文字） + 🔊 朗读开关 ----
_mic_stop = {"ev": None}          # 非空 = 正在录音/识别；主线程负责置空

mic_btn = tk.Button(row1, text="🎤 说话", width=7, command=lambda: toggle_mic(),
                    font=FONT, bg=FIELD_BG, fg=FG, activebackground="#3a3d46",
                    activeforeground=FG, relief="flat", cursor="hand2")
mic_btn.pack(side="left", padx=(6, 0), ipady=6)

voice_on = tk.BooleanVar(value=True)          # 默认开启：回答文字出现的同时自动朗读；可手动关
_voice_enabled = True                          # 朗读总开关（普通 bool：主线程写、朗读线程只读，跨线程安全）
voice_check = tk.Checkbutton(row1, text="🔊 朗读", variable=voice_on,
                             command=lambda: _on_voice_toggle(),
                             font=FONT, bg=BG, fg=FG, selectcolor=BG,
                             activebackground=BG, activeforeground=FG,
                             highlightthickness=0, bd=0, cursor="hand2")
voice_check.pack(side="left", padx=(6, 0))
if not sp_ok:
    mic_btn.config(state="disabled")
    voice_check.config(state="disabled")

def _on_voice_toggle() -> None:
    """勾选状态变化：关掉朗读时立即停声并清空排队句子，防止继续读。"""
    global _voice_enabled, _speak_buf
    _voice_enabled = bool(voice_on.get())      # 同步给朗读线程
    if not _voice_enabled:
        sp.stop_speak()
        _clear_speech_queue()
    _speak_buf = ""

# ---- 设置行：⚙ 朗读设置（音色/语速） + 自动发送 ----
# 音色/语速从本地配置恢复（上次设置的记住，重启不丢）；确定后立刻落盘
_cfg0 = _load_cfg()
_voice_set = {"voice": _cfg0.get("voice", sp.DEFAULT_VOICE),
              "rate": _cfg0.get("rate", sp.DEFAULT_RATE)}

auto_send = tk.BooleanVar(value=False)          # 识别完直接发送（默认关：填进输入框确认后发）
auto_check = tk.Checkbutton(bottom, text="自动发送", variable=auto_send,
                            font=FONT, bg=BG, fg=FG, selectcolor=BG,
                            activebackground=BG, activeforeground=FG,
                            highlightthickness=0, bd=0, cursor="hand2")
auto_check.pack(side="left", pady=(4, 0))

set_btn = tk.Button(bottom, text="⚙ 朗读设置", width=9, command=lambda: open_voice_settings(),
                    font=FONT, bg=BG, fg=SYS_COLOR, activebackground="#3a3d46",
                    activeforeground=FG, relief="flat", cursor="hand2")
set_btn.pack(side="right", pady=(4, 0))
if not sp_ok:
    set_btn.config(state="disabled")

def open_voice_settings() -> None:
    """朗读设置小窗：音色下拉 + 语速滑块，确定后生效并记住（写本地配置）。"""
    win = tk.Toplevel(root)
    win.title("⚙ 朗读设置")
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(root)
    # 回显当前设置
    cur_name = [n for n, v in sp.VOICES.items() if v == _voice_set["voice"]]
    cur_name = cur_name[0] if cur_name else "晓晓（温柔女声）"
    try:
        cur_rate = int(_voice_set["rate"].rstrip("%")) or 0
    except Exception:
        cur_rate = 0
    tk.Label(win, text="音色", bg=BG, fg=FG, font=FONT).grid(
        row=0, column=0, padx=(14, 6), pady=10, sticky="w")
    var = tk.StringVar(value=cur_name)
    opt = tk.OptionMenu(win, var, *sp.VOICES.keys())
    opt.config(bg=FIELD_BG, fg=FG, activebackground="#3a3d46",
               activeforeground=FG, highlightthickness=0, relief="flat")
    opt.grid(row=0, column=1, padx=(6, 14), pady=10)
    tk.Label(win, text="语速", bg=BG, fg=FG, font=FONT).grid(
        row=1, column=0, padx=(14, 6), pady=10, sticky="w")
    rate = tk.IntVar(value=cur_rate)
    tk.Scale(win, from_=-50, to=50, resolution=10, orient="horizontal", variable=rate,
             bg=BG, fg=FG, troughcolor=FIELD_BG, highlightthickness=0,
             length=200).grid(row=1, column=1, padx=(6, 14), pady=10)
    tk.Label(win, text="（选好点确定，下次启动自动记住）", bg=BG, fg=SYS_COLOR,
             font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, columnspan=2, pady=(0, 4))

    def _ok() -> None:
        _voice_set["voice"] = sp.VOICES.get(var.get(), sp.DEFAULT_VOICE)
        _voice_set["rate"] = f"{rate.get():+d}%"
        _save_cfg(voice=_voice_set["voice"], rate=_voice_set["rate"])   # 记住设置
        win.destroy()
    tk.Button(win, text="确定", command=_ok, width=10, bg=FIELD_BG, fg=FG,
              activebackground="#3a3d46", activeforeground=FG, relief="flat",
              cursor="hand2").grid(row=3, column=0, columnspan=2, pady=(6, 12))
    win.grab_set()
    win.focus_set()


def _vol_tick() -> None:
    """录音时按实时音量画电平条，停止后归零（100ms 一帧，开销极小）。"""
    try:
        if _mic_stop["ev"] is not None:
            w = max(2, int(vol.winfo_width() * max(0.0, min(1.0, sp.rec_level()))))
            vol.coords(_vol_rect, 0, 0, w, 6)
        else:
            vol.coords(_vol_rect, 0, 0, 0, 6)
    except Exception:
        pass
    root.after(100, _vol_tick)


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


def type_text(tag: str, text: str, chunk: int = 3, ms: int = 25) -> None:
    """打字机流式展示一条完整消息（主动汇报/晨报等非聊天轮次）；效果与聊天回答一致。"""
    text = (text or "").strip()
    if not text:
        return
    var = _bubble("l" if tag == "ai" else "r")

    def go(i: int = 0) -> None:
        i2 = min(i + chunk, len(text))
        var.set(text[:i2])
        _auto_see()
        if i2 < len(text):
            root.after(ms, lambda: go(i2))

    go()


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
    # 新问题：立即打断还在读的旧回答，并清掉排队没读的句子
    sp.stop_speak()
    _clear_speech_queue()
    global _speak_buf
    _speak_buf = ""
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
        if wav:
            # 只有首次识别才需要下载/加载模型，之后复用单例秒识别 —— 提示也对应区分
            if sp.model_ready():
                _q.put(("vlog", "", "🧠 正在识别…"))
            else:
                _q.put(("vlog", "", "🧠 首次识别，正在加载语音模型（稍等片刻）…"))
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


# ---------------- 分句即时朗读 ----------------
# 回答边打字边说：主线程按句子把文本送进朗读队列，后台 worker 用当前音色/语速顺序朗读
# （voice_on 开着才提交；新问题/关朗读时清空队列并立即停声）
_spk_q: queue.Queue = queue.Queue()
_speak_buf = ""                        # 主线程累积待切句的流式文本
_SENT_END = re.compile(r"[。！？!?…\n；;]")

def _take_sentence(buf: str):
    """从累积文本里取第一个完整句子（含结尾标点）；没有则 (空, 原文)。"""
    m = _SENT_END.search(buf)
    if m:
        idx = m.end()
        return buf[:idx].strip(), buf[idx:]
    return "", buf

def _clear_speech_queue() -> None:
    """丢弃还没读的句子（换新问题/关朗读时用）。"""
    while True:
        try:
            _spk_q.get_nowait()
        except queue.Empty:
            break

def _speaker_worker() -> None:
    """后台顺序朗读：一句接一句，读到一半被 stop 也不崩。"""
    while True:
        seg = _spk_q.get()
        try:
            if seg and _voice_enabled:
                r = sp.speak(seg, _voice_set["voice"], _voice_set["rate"])
                if r:                                 # 朗读失败提示只发一次
                    _q.put(("vlog", "", r))
        except Exception:
            pass

def _feed_speech(payload: str) -> None:
    """收到流式文字 → 按句把完整句交给朗读队列（主线程调用）。"""
    global _speak_buf
    if not _voice_enabled:
        return
    _speak_buf += payload
    while True:
        seg, _speak_buf = _take_sentence(_speak_buf)
        if not seg:
            break
        _spk_q.put(seg)

def _flush_speech() -> None:
    """回答结束：把没到句尾的尾巴也交出去读。"""
    global _speak_buf
    if _voice_enabled and _speak_buf.strip():
        _spk_q.put(_speak_buf.strip())
    _speak_buf = ""

threading.Thread(target=_speaker_worker, daemon=True).start()


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
                _feed_speech(payload)                     # 边说边读：完整句即时进朗读队列
            _auto_see()
        elif kind == "status":
            put("sys", f"🛠 {payload}")
        elif kind == "mem":
            put("sys", f"🧠 已摘记：{payload}")
        elif kind == "aend":
            if payload:
                _history.extend([HumanMessage(content=text), AIMessage(content=payload)])
                del _history[:-MAX_HISTORY]
                _flush_speech()       # 回答结束：把没到句尾的尾巴也交给朗读队列
            _ai_bubble["var"] = None
        elif kind == "vlog":
            put("sys", payload)
        elif kind == "vtext":
            entry.delete(0, tk.END)     # 识别结果：填进输入框；自动发送开则直接发出
            entry.insert(0, payload)
            if auto_send.get():
                _mic_stop["ev"] = None  # 识别已结束，放行发送（vidle 随后只复位按钮）
                send()
            else:
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
    rep = pa.morning_report_due()      # 每日晨报：到点自动推一次进聊天区（打字机流式）
    if rep:
        type_text("ai", f"📰 每日晨报\n{rep}")
    root.after(2000, check_reminders)


def on_close() -> None:
    _save_cfg(w=root.winfo_width(), h=root.winfo_height())   # 记窗口大小，下次还原
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
type_text("ai", "助理（主动汇报）：" + pa.make_startup_briefing())   # 主动汇报打字机流式

root.after(150, poll)
root.after(2000, check_reminders)
root.after(100, _vol_tick)
entry.focus_set()
root.mainloop()