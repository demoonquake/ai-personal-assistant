"""
我的私人助理（多Agent团队版）
================================================
产品使用方式：交互式聊天。
核心能力：记忆与笔记——记住你的话、回忆你记过的东西、帮你遗忘。
会记住：名字、爱好、待办、随手记…… 存在 assistant_memory/ 文件夹，重启不忘。

团队架构（前台分流 + 聊天座席 + 记忆管理流水线）：
  你说话 → [前台Agent] 听懂你要"记 / 问 / 忘 / 聊 / 整理"
        ├─ 聊天 → [聊天席] 带上跨轮对话历史和长线记忆，陪你自然聊
        │         → [记忆秘书] 聊完自动摘记值得记的用户信息，存盘
        ├─ 整理 → [记忆整理员] 合并重复、清理过时（定期也会自动做）
        └─ 记忆 → [办事Agent] 必须调用工具（save_note / recall_all / forget / update_note）
                → [秘书Agent] 把结果组织成一句温和的答话 → 你
  每条笔记自动带『记录时间』；旧文件没有时间戳也不影响读取。

技术栈：LangChain(create_agent) 搭单Agent，
        LangGraph(StateGraph) 串流水线 + 条件边（聊天/整理/记忆 三岔），
        长线记忆 = 笔记文件持久化（重启后读回来，每条带记录时间），
        短线对话 = 程序内存里的 history 列表（跨轮携带）。
================================================
运行：python personal_assistant.py
"""

import os
import sys
import re
import ast
import json
import html
import time
import threading
import datetime
import urllib.request
import urllib.parse

os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")

from langgraph.graph import StateGraph, START, END
from typing import TypedDict
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain.agents import create_agent

# 打包成 exe 后 __file__ 会指向解包临时目录：那里存不了数据，
# 所以 frozen 时改用『exe 所在目录』——数据就落在 exe 旁边的 assistant_memory/
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_DIR = os.path.join(BASE_DIR, "assistant_memory")
os.makedirs(MEMORY_DIR, exist_ok=True)

# ---- 多用户支持（v0.2）----
# 每个线程可以指向不同的『用户数据目录』；终端版/窗口版不切换，
# 用的就是默认 MEMORY_DIR，行为与之前完全一致。
_USER_CTX = threading.local()

def user_dir() -> str:
    """当前线程的用户数据目录（默认全局 MEMORY_DIR）。"""
    return getattr(_USER_CTX, "dir", None) or MEMORY_DIR

def switch_user_dir(d: str) -> None:
    """把当前线程切到指定用户的数据目录（不存在则创建）。"""
    os.makedirs(d, exist_ok=True)
    _USER_CTX.dir = d

def fire_log() -> list:
    """当前线程的『提醒已响』记录（多用户隔离；未初始化时给空列表）。"""
    if not hasattr(_USER_CTX, "fire_log"):
        _USER_CTX.fire_log = []
    return _USER_CTX.fire_log

# 这三份是"数据表"（待办/提醒/日程），外加助理自己的名字——不算用户记忆：回忆、长期记忆、整理员都不得读改它们
NON_MEMORY_FNS = {"todo.md", "reminders.md", "schedule.md", "助理名字.md"}


# ---------------------------------------------------------------
# 1) 私人助理的"本事"：记忆工具（每条笔记自动带"记录时间"）
# ---------------------------------------------------------------
TIME_TAG = "<!-- 记录时间："

def pack_note(content: str) -> str:
    """笔记落盘格式：正文 + 一条时间戳注释（显示时由 unpack_note 剥掉）。"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"{content}\n\n{TIME_TAG}{ts} -->"

def unpack_note(raw: str) -> tuple[str, str]:
    """把笔记文件拆成 (纯净正文, 记录时间)；老文件没时间戳就返回空串。"""
    ts = ""
    m = re.search(re.escape(TIME_TAG) + r"\s*(.+?)\s*-->", raw)
    if m:
        ts = m.group(1).strip()
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith(TIME_TAG)]
    return "\n".join(lines).strip(), ts

@tool
def save_note(title: str, content: str) -> str:
    """记住一条关于用户的事实/喜好/待办，存成笔记文件（自动带记录时间）。用户明确说"记住/记一下/存个"时用。"""
    path = os.path.join(user_dir(), f"{title}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(pack_note(content))
    return f"已记住: {title}"

@tool
def recall_all() -> str:
    """回忆：列出当前记住的所有笔记（逐个读文件，含记录时间）。用户问"你记得我什么/我以前说过什么"时用。"""
    lines = []
    for fn in sorted(os.listdir(user_dir())):
        if fn.endswith(".md") and fn not in NON_MEMORY_FNS:
            title = fn[:-3]
            with open(os.path.join(user_dir(), fn), encoding="utf-8") as f:
                content, ts = unpack_note(f.read())
            note = f"- {title}：{content}"
            if ts:
                note += f"（记于 {ts}）"
            lines.append(note)
    return "\n".join(lines) if lines else "（暂时没有记住任何内容）"

@tool
def forget(title: str) -> str:
    """遗忘：删除标题为 title 的笔记文件。用户说"忘掉X/删掉X"时用。"""
    path = os.path.join(user_dir(), f"{title}.md")
    if os.path.exists(path):
        os.remove(path)
        return f"已忘记: {title}"
    return f"没有找到名为 {title} 的笔记。"

@tool
def update_note(title: str, content: str) -> str:
    """更新：把标题为 title 的笔记内容改成 content（覆盖写，记录时间刷新为当下）。标题不存在时自动新建。
    用户说"改成/改为/换成/更新/改主意了/修正"时用。"""
    path = os.path.join(user_dir(), f"{title}.md")
    existed = os.path.exists(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(pack_note(content))
    return f"已更新: {title}" if existed else f"已新建: {title}"

# ---------------------------------------------------------------
# 1.5) 两个"真工具"：算数 和 查天气（确定性执行，模型不负责计算）
# ---------------------------------------------------------------
WEATHER_CODE = {   # WMO 天气代码 → 中文天气现象
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "小毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}

@tool
def calculate(expression: str) -> str:
    """计算数学表达式，如 '3+5*2'、'(10-4)/3'，支持加减乘除、括号和取余。"""
    expr = expression.replace("×", "*").replace("÷", "/").replace("x", "*").replace("X", "*")
    expr = re.sub(r"[^\d+\-*/().%\s]", "", expr)   # 只留数字和运算符（硬防护）
    if not expr:
        return "（没看懂要算什么）"
    try:
        tree = ast.parse(expr, mode="eval")
        allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.USub,
                   ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Constant)
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                return "（只支持加减乘除和括号）"
        result = eval(compile(tree, "", "eval"), {"__builtins__": {}}, {})
        return format(float(result), ".10g") if isinstance(result, float) else str(result)
    except Exception:
        return "（算式有点问题，检查一下？）"

@tool
def get_weather(city: str = "", day: str = "今天") -> str:
    """查询一座城市某天的天气。
    city 是城市名，用户没指定时传空字符串（自动查用户的默认城市，没有默认城市就用武汉）。
    day 取值『今天』『明天』『后天』，用户没明确说哪天时传『今天』。
    今天能查到当前实况（天气/气温/体感/湿度/风速），明天后天能查到预报（天气/最高最低温/降水概率）。"""
    try:
        if not city:
            city = get_default_city() or "武汉"
        day = str(day or "今天")
        if "明" in day:
            offset = 1
        elif "后" in day:
            offset = 2
        elif day.isdigit():
            offset = int(day) % 3
        else:
            offset = 0
        # 第 1 步：城市名 → 经纬度（Open-Meteo 地理编码，免费无需 key）
        geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
                   f"name={urllib.parse.quote(city)}&count=1&language=zh&format=json")
        with urllib.request.urlopen(geo_url, timeout=8) as r:
            geo = json.load(r)
        if not geo.get("results"):
            return f"（没找到城市：{city}）"
        loc = geo["results"][0]
        place = loc.get("name", city)
        if loc.get("admin1"):
            place += f"，{loc['admin1']}"
        # 第 2 步：经纬度 → 当前实况 + 三天预报（覆盖今天/明天/后天）
        w_url = (f"https://api.open-meteo.com/v1/forecast?"
                 f"latitude={loc['latitude']}&longitude={loc['longitude']}"
                 f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
                 f"&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_mean"
                 f"&forecast_days=3&timezone=auto")
        with urllib.request.urlopen(w_url, timeout=8) as r:
            w = json.load(r)
        d = w["daily"]
        # 今天：报当前实况（更详细）
        if offset == 0:
            cur = w["current"]
            desc = WEATHER_CODE.get(cur.get("weather_code"), "未知天气")
            return (f"{place}今天：{desc}，气温 {cur['temperature_2m']}℃（体感 {cur['apparent_temperature']}℃），"
                    f"湿度 {cur['relative_humidity_2m']}%，风速 {cur['wind_speed_10m']} km/h，"
                    f"今天最高 {d['temperature_2m_max'][0]}℃ / 最低 {d['temperature_2m_min'][0]}℃")
        # 明天/后天：报预报
        day_cn = ("明天", "后天")[offset - 1]
        desc = WEATHER_CODE.get(d["weather_code"][offset], "未知天气")
        rain = d["precipitation_probability_mean"][offset]
        return (f"{place}{day_cn}：{desc}，最高 {d['temperature_2m_max'][offset]}℃ / 最低 {d['temperature_2m_min'][offset]}℃，"
                f"降水概率 {rain}%")
    except Exception as e:
        return f"（天气暂时查不到：{e}）"

@tool
def get_current_time() -> str:
    """获取当前的日期和时间（含星期几）。用户问现在几点/今天几号/星期几时用。"""
    now = datetime.datetime.now()
    week = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[now.weekday()]
    return f"{now.strftime('%Y年%m月%d日')} {week} {now.strftime('%H:%M')}"

def _bing_search(query: str, count: int = 4) -> list[str]:
    """爬必应国内版搜索结果页：标题+摘要（免 key、国内直连可达）。"""
    url = ("https://cn.bing.com/search?q=" + urllib.parse.quote(query)
           + "&setlang=zh-cn&ensearch=0")
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        page = r.read().decode("utf-8", errors="ignore")
    items = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', page, re.S):
        block = m.group(0)
        t = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not t:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", t.group(2))).strip()
        snip = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        desc = html.unescape(re.sub(r"<[^>]+>", "", snip.group(1))).strip() if snip else ""
        if title:
            items.append(f"[必应] {title}：{desc}（{t.group(1)}）")
        if len(items) >= count:
            break
    return items

@tool
def search_web(query: str) -> str:
    """联网搜索：查实时信息/新闻/概念/百科。query 是要搜的关键词，尽量简短。
    返回搜到的片段（可能为空）；没搜到就沿用返回的提示，不要自己编造结果。"""
    results = []
    # 1) 必应国内版（主要渠道，国内直连稳定）
    try:
        results.extend(_bing_search(query))
    except Exception:
        pass
    # 2) DuckDuckGo 即答 API：百科摘要/即时答案（免 key；需网络可达）
    try:
        url = ("https://api.duckduckgo.com/?format=json&no_html=1&skip_disambig=1&q="
               + urllib.parse.quote(query))
        with urllib.request.urlopen(url, timeout=6) as r:
            d = json.load(r)
        if d.get("Answer"):
            results.append(f"[即答] {d['Answer']}")
        if d.get("AbstractText"):
            src = d.get("AbstractURL") or "duckduckgo.com"
            results.append(f"[摘要] {d['AbstractText']}（来源：{src}）")
        for t in (d.get("RelatedTopics") or [])[:3]:
            if isinstance(t, dict) and t.get("Text"):
                results.append(f"[相关] {t['Text']}（来源：{t.get('FirstURL')}）")
    except Exception:
        pass
    # 3) 中文维基百科搜索（取第一条结果的简介；需网络可达）
    try:
        wurl = ("https://zh.wikipedia.org/w/api.php?action=query&list=search&srsearch="
                + urllib.parse.quote(query) + "&format=json&utf8=1&srlimit=1")
        with urllib.request.urlopen(wurl, timeout=6) as r:
            w = json.load(r)
        hits = w.get("query", {}).get("search", [])
        if hits:
            title = hits[0]["title"]
            eurl = ("https://zh.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1"
                    "&explaintext=1&format=json&utf8=1&titles=" + urllib.parse.quote(title))
            with urllib.request.urlopen(eurl, timeout=6) as r:
                e = json.load(r)
            for p in e.get("query", {}).get("pages", {}).values():
                snippet = (p.get("extract") or "").strip()
                if snippet:
                    results.append(f"[维基百科·{title}] {snippet[:400]}")
                    break
    except Exception:
        pass
    if not results:
        return "（没搜到相关内容。可以换个关键词再让我搜一次，或者把问题说得更具体些。）"
    return "\n".join(results)

def get_default_city() -> str:
    """读回『默认城市』这条记忆（标题为 默认城市 的笔记），没有就返回空串。"""
    path = os.path.join(user_dir(), "默认城市.md")
    if os.path.exists(path):
        content, _ = unpack_note(open(path, encoding="utf-8").read())
        return content.strip().strip("，,。：: ")
    return ""

def get_assistant_name() -> str:
    """读回助理自己的名字（用户起的），没有就返回空串。与用户名字分开存，绝不混。"""
    path = os.path.join(user_dir(), "助理名字.md")
    if os.path.exists(path):
        content, _ = unpack_note(open(path, encoding="utf-8").read())
        return content.strip().strip("，,。：: ")
    return ""

def remember_assistant_name(raw: str) -> str:
    """用户给『助理自己』起名时，存进 助理名字.md（不是用户名字槽）。
    命中返回结果文本，没命中返回空串。"""
    m = re.search(r"(?:你的名字(?:是|叫|叫做|就叫)|以后(?:就叫你|叫你)|你就叫|你叫)([\u4e00-\u9fa5A-Za-z0-9]{1,10})", raw)
    if not m:
        return ""
    name = m.group(1).strip().strip("，,。！! ")
    # 误伤防护：像"你叫什么名字"这种问句跑不到这（前面有问句拦截）；这里只认祈使/陈述
    save_note.invoke({"title": "助理名字", "content": name})
    return f"已把『助理自己的名字』设为「{name}」（用户的名字保持原样未动）"


# ---------------------------------------------------------------
# 1.6) 待办清单：全部存在 todo.md 一个文件里（勾选列表，确定性函数执行）
# ---------------------------------------------------------------
TODO_FN = "todo.md"
def todo_file() -> str:
    return os.path.join(user_dir(), TODO_FN)

def load_todos() -> list[dict]:
    """读回 todo.md，变成 [{item, done, time}]。"""
    items = []
    if os.path.exists(todo_file()):
        for ln in open(todo_file(), encoding="utf-8").read().splitlines():
            ln = ln.strip()
            if not ln.startswith("- ["):
                continue
            done = ln.startswith("- [x]")
            body = ln[6:].strip()                      # 去掉 "- [ ] " 前缀
            m = re.search(r"（记于 (.+?)）$", body)
            time = m.group(1) if m else ""
            item = re.sub(r"（记于 .+?）$", "", body).strip()
            items.append({"item": item, "done": done, "time": time})
    return items

def save_todos(items: list[dict]) -> None:
    with open(todo_file(), "w", encoding="utf-8") as f:
        for it in items:
            mark = "x" if it["done"] else " "
            ts = f"（记于 {it['time']}）" if it.get("time") else ""
            f.write(f"- [{mark}] {it['item']}{ts}\n")

def add_todo(item: str) -> str:
    items = load_todos()
    items.append({"item": item, "done": False,
                  "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})
    save_todos(items)
    return f"已加进待办：{item}（还剩 {sum(1 for i in items if not i['done'])} 件没做完）"

def list_todos() -> str:
    items = load_todos()
    if not items:
        return "（待办清单是空的）"
    lines = []
    for i, it in enumerate(items, 1):
        mark = "☑" if it["done"] else "☐"
        tail = f"（记于 {it['time']}）" if it.get("time") else ""
        done_tag = " —— 已完成" if it["done"] else ""
        lines.append(f"{i}. {mark} {it['item']}{tail}{done_tag}")
    return "\n".join(lines)

@tool
def list_todos_tool() -> str:
    """查看用户的待办清单（含已完成和未完成）。用户问有哪些待办/要做的事时用。"""
    return list_todos()

def done_todo(keyword: str) -> str:
    items = load_todos()
    for it in items:
        if not it["done"] and keyword and keyword in it["item"]:
            it["done"] = True
            save_todos(items)
            return f"已完成：{it['item']}"
    return f"（没找到没完成的待办里有「{keyword}」的）"

def remove_todo(keyword: str) -> str:
    old_len = len(load_todos())
    keep = [it for it in load_todos() if not (keyword and keyword in it["item"])]
    if len(keep) == old_len:
        return f"（没找到待办里有「{keyword}」的）"
    save_todos(keep)
    return f"已从待办里删除含「{keyword}」的待办"

def pick_todo_kw(raw: str) -> str:
    """从一句话里抠出要定位的那个待办关键词（把命令口吻词剥掉）。"""
    kw = raw
    for w in ("待办", "提醒", "别忘了", "完成", "搞定了", "做完了", "做掉了", "删掉", "取消",
              "移除", "删除", "帮我", "把", "那个", "这个", "一下", "了", "的", "我",
              "看看", "我的", "先"):
        kw = kw.replace(w, "")
    return kw.strip().strip("，,。?! ")


# ---------------------------------------------------------------
# 1.7) 定时提醒：存在 reminders.md，后台线程到点弹提醒（确定性解析时间）
# ---------------------------------------------------------------
REMIND_FN = "reminders.md"
def remind_file() -> str:
    return os.path.join(user_dir(), REMIND_FN)
_remind_lock = threading.Lock()

def load_reminders() -> list[dict]:
    """读回 reminders.md → [{at: datetime, text}]。"""
    items = []
    if os.path.exists(remind_file()):
        for ln in open(remind_file(), encoding="utf-8").read().splitlines():
            ln = ln.strip()
            if ln.startswith("- ") and "：" in ln:
                ts, text = ln[2:].split("：", 1)
                try:
                    at = datetime.datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M")
                    items.append({"at": at, "text": text.strip()})
                except ValueError:
                    pass
    return items

def save_reminders(items: list[dict]) -> None:
    with open(remind_file(), "w", encoding="utf-8") as f:
        for it in sorted(items, key=lambda x: x["at"]):
            f.write(f"- {it['at'].strftime('%Y-%m-%d %H:%M')}：{it['text']}\n")

def add_reminder(at: datetime.datetime, text: str) -> str:
    with _remind_lock:
        items = load_reminders()
        items.append({"at": at, "text": text})
        save_reminders(items)
    return f"已设提醒：{at.strftime('%m月%d日 %H:%M')} {text}"

def list_reminders() -> str:
    items = load_reminders()
    if not items:
        return "（当前没有设置任何提醒）"
    return "\n".join(f"- {it['at'].strftime('%m月%d日 %H:%M')}：{it['text']}" for it in items)

def cancel_reminder(keyword: str) -> str:
    with _remind_lock:
        old = load_reminders()
        keep = [it for it in old if not (keyword and keyword in it["text"])]
        if len(keep) == len(old):
            return f"（没找到提醒里有「{keyword}」的）"
        save_reminders(keep)
        return f"已取消提醒：{keyword}"

def _cn2int(s: str) -> int:
    """汉字数字 → 整数，支持 一~九、十、X十、十X、X十X（最大 99）。解析不了返回 -1。"""
    digits = "零一二两三四五六七八九"
    if "十" in s:
        parts = s.split("十")
        left = digits.index(parts[0]) if parts[0] and parts[0] in digits else 1    # 光一个"十"= 10
        right = digits.index(parts[1]) if len(parts) > 1 and parts[1] in digits else 0
        return left * 10 + right
    if len(s) == 1 and s in digits:
        return digits.index(s)
    return -1

def parse_remind_time(text: str) -> tuple[datetime.datetime | None, str]:
    """从一句话里抠出 时间 + 提醒内容。
    支持：17:30 / 3分钟后 / 十秒钟之后 / 明天9点 / 下午3点半 / 半小时后。解析不出具体时间就返回 (None, "")。"""
    now = datetime.datetime.now()
    at = None
    # 1) 17:30 这种时刻
    m = re.search(r"(\d{1,2})[:：](\d{2})", text)
    if m:
        at = now.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)), second=0, microsecond=0)
        if at <= now:
            at += datetime.timedelta(days=1)
        text = text.replace(m.group(0), "", 1)
    else:
        # 2) 汉字数字相对时间：十秒钟之后 / 五分钟后 / 一个小时候 / 半小时后
        m_cn = re.match(r"(半小时|[零一二两三四五六七八九十]+)个?(秒钟|秒|分钟|小时)(之?后)", text)
        if m_cn and (m_cn.group(1) == "半小时" or _cn2int(m_cn.group(1)) > 0):
            unit = m_cn.group(2)
            if m_cn.group(1) == "半小时":
                at = now + datetime.timedelta(minutes=30)
            else:
                num = _cn2int(m_cn.group(1))
                if unit in ("秒钟", "秒"):
                    at = now + datetime.timedelta(seconds=num)
                elif unit == "分钟":
                    at = now + datetime.timedelta(minutes=num)
                else:
                    at = now + datetime.timedelta(hours=num)
            text = text[m_cn.end():].strip()
        else:
            # 2.5) "半小时后"——半小时 自带"小时"二字，单独处理
            m_half = re.match(r"半小时(之?后)", text)
            if m_half:
                at = now + datetime.timedelta(minutes=30)
                text = text[m_half.end():].strip()
            else:
                # 3) N秒钟后/N分钟之后/N小时后 这种相对时间（阿拉伯数字）
                m = re.search(r"(\d+)\s*个?(秒钟|秒|分钟|小时)(之?后)", text)
                if m:
                    unit_map = {"秒钟": "seconds", "秒": "seconds", "分钟": "minutes", "小时": "hours"}
                    at = now + datetime.timedelta(**{unit_map[m.group(2)]: int(m.group(1))})
                    text = text.replace(m.group(0), "", 1)
                else:
                    # 4) (今天/明天/后天/周几)(上午|下午|晚上)?N点(半/X分) 这种看钟的话
                    m = re.search(r"((今天|明天|后天|周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天])?(上午|中午|下午|晚上|早上|白天|半夜|凌晨)?(\d{1,2}|[零一二两三四五六七八九十]+)点(半|(\d{1,2}|[零一二两三四五六七八九十]+)分)?)", text)
                    if m:
                        phrase, daywd, hour, half, minute = m.group(1), m.group(2) or "", m.group(4), m.group(5), m.group(6)
                        h = int(hour) if hour and hour.isdigit() else (_cn2int(hour) if hour else 0)
                        if half == "半":
                            mi = 30
                        elif minute:
                            mi = int(minute) if minute.isdigit() else _cn2int(minute)
                        else:
                            mi = 0
                        h = h + 12 if h < 6 else h          # "下午3点/晚上7点"这类没写时段的，先把 0-5 当下午
                        if any(w in phrase for w in ("下午", "晚上", "半夜", "傍晚")) and h < 12:
                            h += 12
                        h %= 24
                        day = now
                        if daywd == "明天":
                            day = now + datetime.timedelta(days=1)
                        elif daywd == "后天":
                            day = now + datetime.timedelta(days=2)
                        elif daywd and any(daywd.startswith(p) for p in ("周", "星期", "礼拜")):
                            # 周X → 未来最近的那个周X（今天就是该周几则算今天）
                            wd = re.search(r"[一二三四五六日天]", daywd).group(0)
                            target = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[wd]
                            diff = (target - now.weekday()) % 7
                            day = now + datetime.timedelta(days=diff)
                        at = day.replace(hour=h, minute=mi, second=0, microsecond=0)
                        if at <= now:
                            at += datetime.timedelta(days=1)
                        if m.start() > 0 and text[m.start() - 1] == "下":
                            # 『下周三』里正则从"周"开始匹配，把前面的"下"一起吃掉
                            text = text[:m.start() - 1] + text[m.end():]
                        else:
                            text = text.replace(m.group(0), "", 1)
    if not at:
        return None, ""
    # 剥掉命令口吻词，剩下的就是提醒内容
    for w in ("提醒我", "提醒", "帮我", "定个提醒", "设个提醒", "设置提醒", "别忘了", "记得", "安排一下"):
        text = text.replace(w, "")
    return at, text.strip().strip("，,。！!：: ")

def _reminder_watcher() -> None:
    """后台线程：每 2 秒扫一遍提醒，到点打印提醒并从列表移除。"""
    while True:
        now = datetime.datetime.now()
        due = []
        with _remind_lock:
            items = load_reminders()
            due = [it for it in items if it["at"] <= now]
            if due:
                save_reminders([it for it in items if it["at"] > now])
        for it in due:
            with _remind_lock:
                fire_log().append(f"已在 {it['at'].strftime('%H:%M')} 提醒过用户：{it['text']}")
                del fire_log()[:-5]      # 只留最近 5 条
            print(f"\n⏰ 提醒时间到：{it['text']}（{it['at'].strftime('%H:%M')}）")
        rep = morning_report_due()       # 每日晨报：到点自动推一次
        if rep:
            print(f"\n📰 每日晨报（{datetime.datetime.now().strftime('%H:%M')}）")
            print(rep)
        time.sleep(2)


# ---------------------------------------------------------------
# 1.8) 日程表：存在 schedule.md（到点不响铃，是"哪天几点做什么"的安排）
# ---------------------------------------------------------------
SCHEDULE_FN = "schedule.md"
def schedule_file() -> str:
    return os.path.join(user_dir(), SCHEDULE_FN)

def load_schedule() -> list[dict]:
    """读回 schedule.md → [{at: datetime, text}]。"""
    items = []
    if os.path.exists(schedule_file()):
        for ln in open(schedule_file(), encoding="utf-8").read().splitlines():
            ln = ln.strip()
            if ln.startswith("- ") and "：" in ln:
                ts, text = ln[2:].split("：", 1)
                try:
                    at = datetime.datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M")
                    items.append({"at": at, "text": text.strip()})
                except ValueError:
                    pass
    return items

def save_schedule(items: list[dict]) -> None:
    with open(schedule_file(), "w", encoding="utf-8") as f:
        for it in sorted(items, key=lambda x: x["at"]):
            f.write(f"- {it['at'].strftime('%Y-%m-%d %H:%M')}：{it['text']}\n")

def _add_schedule(at: datetime.datetime, text: str) -> str:
    items = load_schedule()
    items.append({"at": at, "text": text})
    save_schedule(items)
    return f"已记进日程：{at.strftime('%m月%d日 %H:%M')} {text}"

def list_schedule(day: str = "") -> str:
    """列出日程。day 可选『今天』『明天』『后天』或日期（YYYY-MM-DD），留空列出所有未来日程。"""
    now = datetime.datetime.now()
    target = None
    if day in ("今天", "明天", "后天"):
        target = now.date() + datetime.timedelta(days={"今天": 0, "明天": 1, "后天": 2}[day])
        items = [it for it in load_schedule() if it["at"].date() == target]
    elif day:
        try:
            target = datetime.datetime.strptime(day, "%Y-%m-%d").date()
            items = [it for it in load_schedule() if it["at"].date() == target]
        except ValueError:
            items = [it for it in load_schedule() if day in it["text"]]
    else:
        items = [it for it in load_schedule() if it["at"] >= now]
    items = sorted(items, key=lambda x: x["at"])
    if not items:
        return "（日程表里没有相关安排）"
    return "\n".join(f"- {it['at'].strftime('%m月%d日 %H:%M')}：{it['text']}" for it in items)

@tool
def add_schedule(when: str, text: str) -> str:
    """把一条日程记进日程表（某天什么时间做什么事，不响铃）。
    when 用时间说法，如『明天下午3点』『周三晚上7点』『后天9点』；text 是事项。"""
    at, content = parse_remind_time(f"{when}{text}")
    if not at:
        return f"（时间『{when}』没看懂，请用『明天下午3点』『周三晚上7点』这类说法）"
    return _add_schedule(at, content or text)

@tool
def list_schedule_tool(day: str = "") -> str:
    """查看日程表。day 可选『今天』『明天』『后天』或日期(YYYY-MM-DD)，留空列出所有未来日程。"""
    return list_schedule(day)


def make_startup_briefing() -> str:
    """启动主动汇报：数据确定性收集（绝不编造）→ DeepSeek 组织成自然开场白；
    模型出问题时回退到确定性模板。"""
    now = datetime.datetime.now()
    hour = now.hour
    if 5 <= hour < 9:
        greet = "早上好"
    elif 9 <= hour < 12:
        greet = "上午好"
    elif 12 <= hour < 14:
        greet = "中午好"
    elif 14 <= hour < 18:
        greet = "下午好"
    elif 18 <= hour < 23:
        greet = "晚上好"
    else:
        greet = "夜深了"
    name = "朋友"
    name_path = os.path.join(user_dir(), "名字.md")
    if os.path.exists(name_path):
        n, _ = unpack_note(open(name_path, encoding="utf-8").read())
        if n:
            name = n
    week = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[now.weekday()]
    todos = [t for t in load_todos() if not t["done"]]
    rems = load_reminders()
    nxt = min(rems, key=lambda r: r["at"]) if rems else None
    schs = [it for it in load_schedule() if it["at"] >= now]

    # 确定性收集的事实（模型的"嘴"只能用这些，不许编）
    data_lines = [
        f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M')} {week}",
        f"用户称呼：{name}",
        f"未完成待办：{len(todos)} 件" + (f" —— {'、'.join(t['item'] for t in todos[:3])}{'…' if len(todos) > 3 else ''}" if todos else "，暂无"),
        f"已设提醒：{len(rems)} 条" + (f" —— 最近一条「{nxt['text']}」在 {nxt['at'].strftime('%m月%d日 %H:%M')}" if rems else "，暂无"),
        f"未来日程：{len(schs)} 条" + (f" —— 最近一条「{schs[0]['text']}」在 {schs[0]['at'].strftime('%m月%d日 %H:%M')}" if schs else "，暂无"),
    ]
    # 联动：明天有日程 → 顺手把明天的天气也查好放进汇报（当天没有就不多花这次查询）
    tomorrow_schs = [it for it in schs if it["at"].date() == (now + datetime.timedelta(days=1)).date()]
    if tomorrow_schs:
        try:
            w_report = get_weather.func(day="明天")
            if w_report and not w_report.startswith("（"):
                data_lines.append(f"明天天气（明天有日程，替你预查了）：{w_report}")
        except Exception:
            pass

    # 回退模板（模型失败时用，保证启动永不卡壳）
    fallback = [f"{greet}，{name}。今天是 {now.strftime('%Y年%m月%d日')} {week}。"]
    if todos:
        fallback.append(f"你还有 {len(todos)} 件待办没做完："
                        f"{'；'.join(t['item'] for t in todos[:3])}{'…' if len(todos) > 3 else ''}。")
    else:
        fallback.append("待办清单是空的，一身轻松～")
    fallback.append(f"设了 {len(rems)} 个提醒，最近一条是「{nxt['text']}」，在 {nxt['at'].strftime('%m月%d日 %H:%M')}。"
                    if rems else "目前没有设置提醒。")
    if schs:
        fallback.append(f"最近的日程：「{schs[0]['text']}」在 {schs[0]['at'].strftime('%m月%d日 %H:%M')}。")

    try:
        ai = chat_model.invoke([
            SystemMessage(content=(
                "你是私人助理，刚启动时要主动向用户汇报。下面是确定性的数据。\n"
                "要求：先按时段问好，再用自然、亲切、简短的口吻把这些要点说给用户，2~3 句。\n"
                "只能使用数据里的事实，不许编造数字或事项。输出纯文本，不要任何标记或列表符号。"
            )),
            HumanMessage(content="\n".join(data_lines)),
        ])
        text = (ai.content or "").strip()
        if text:
            return text
    except Exception:
        pass
    return "\n".join(fallback)


def make_morning_report() -> str:
    """每日晨报：确定性汇总（今日日程 / 今明两天天气 / 未完成待办 / 今日提醒）
    → DeepSeek 组织成一段自然晨报；模型异常时回退到确定性模板。"""
    now = datetime.datetime.now()
    week = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[now.weekday()]
    name = "朋友"
    name_path = os.path.join(user_dir(), "名字.md")
    if os.path.exists(name_path):
        n, _ = unpack_note(open(name_path, encoding="utf-8").read())
        if n:
            name = n
    date_cn = now.strftime("%Y年%m月%d日")

    # ---- 确定性收集『今天的料』：全部来自真实数据，模型不许编 ----
    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return ""
    tod_sch = safe(list_schedule, "今天")
    tom_sch = safe(list_schedule, "明天")
    w_today = safe(get_weather.func, "", "今天")
    w_tomorrow = safe(get_weather.func, "", "明天")
    todos = [t for t in load_todos() if not t["done"]]
    today_rems = [it for it in load_reminders() if it["at"].date() == now.date()]
    rec_line = ("、".join(f"{it['text']}（{it['at'].strftime('%H:%M')}）" for it in today_rems)
                if today_rems else "今日无提醒")

    data_lines = [
        f"日期：{date_cn} {week}",
        f"用户称呼：{name}",
        f"今日日程：{tod_sch}",
        f"明日日程：{tom_sch if tom_sch and '没有相关安排' not in tom_sch else '明日暂无安排'}",
        f"今日天气：{w_today}",
        f"明日天气：{w_tomorrow}",
        f"未完成待办：{len(todos)} 件" + (f" —— {'、'.join(t['item'] for t in todos)}" if todos else "，暂无"),
        f"今日提醒：{rec_line}",
    ]

    # 回退模板（模型失败时用，保证晨报永不卡壳）
    fallback = [f"{date_cn} {week}，早安，{name}！"]
    fallback.append(f"今天天气：{w_today}。明日：{w_tomorrow}" if w_today and not w_today.startswith("（")
                    else "今天天气暂时没查到。")
    if "没有相关安排" not in tod_sch:
        fallback.append(f"今天日程：{tod_sch}")
    fallback.append(f"还有 {len(todos)} 件待办没做完：{'、'.join(t['item'] for t in todos)}。"
                    if todos else "待办清单是空的，一身轻松～")
    if today_rems:
        fallback.append("今天设了提醒：" + "、".join(
            f"{it['text']}（{it['at'].strftime('%H:%M')}）" for it in today_rems))
    else:
        fallback.append("今天没有设提醒。")

    try:
        ai = chat_model.invoke([
            SystemMessage(content=(
                "你是私人助理，正在为用户出一份『每日晨报』。数据已由程序确定性收集（真实数据，绝对不许编造）。\n"
                f"要求输出一段自然、有条理、亲切的晨报，按这个顺序组织：\n"
                f"1) 问候（带用户称呼和日期星期）；\n"
                f"2) 今天天气怎么样 + 明天简单预告，温度落差大或有雨就顺带一句提醒建议；\n"
                f"3) 今天的日程安排；\n"
                f"4) 还没做完的待办，先点要紧的；\n"
                f"5) 今天的提醒；\n"
                f"6) 结尾一句简短鼓励。\n"
                "只使用数据里的事实，查不到或没有就说没有。输出纯文本，不要标题和列表符号。"
            )),
            HumanMessage(content="\n".join(data_lines)),
        ])
        text = (ai.content or "").strip()
        if text:
            return text
    except Exception:
        pass
    return "\n".join(fallback)

@tool
def morning_report() -> str:
    """生成『每日晨报』：汇总今天的日程、今明两天天气、未完成待办和今日提醒，
    让用户一眼看清今天要做什么。用户说『每日晨报』『今日晨报』『看晨报』『汇报今天安排』时调用。"""
    return make_morning_report()


# 自动推送晨报：每天到点后首次轮询时推送一次（终端/GUI/网页三入口共用）。
# 判断靠用户目录下的 morning_done.txt 记『今天已推过的日期』→ 全天只推一次、多端同时开也不重复。
MORNING_REPORT_TIME = "08:00"   # 每天早上自动推送晨报的时刻（24小时制，改完重启生效）

def morning_report_due() -> str:
    """到点且今天还没推过晨报 → 生成晨报并落盘标记；未到点/已推过返回空串。"""
    now = datetime.datetime.now()
    h, m = map(int, MORNING_REPORT_TIME.split(":"))
    if (now.hour, now.minute) < (h, m):
        return ""
    flag = os.path.join(user_dir(), "morning_done.txt")
    done = ""
    try:
        if os.path.exists(flag):
            with open(flag, encoding="utf-8") as f:
                done = f.read().strip()
    except Exception:
        done = ""  # 读不到就当没推过，照常生成
    if done == now.strftime("%Y-%m-%d"):
        return ""
    report = make_morning_report()
    try:
        with open(flag, "w", encoding="utf-8") as f:
            f.write(now.strftime("%Y-%m-%d"))
    except Exception:
        pass  # 写失败只丢去重标记，晨报仍正常返回
    return report


# ---------------------------------------------------------------
# 2) 启动时：把已存的笔记加载成"长期记忆"塞进人设（重启不忘的关键）
# ---------------------------------------------------------------
def load_long_memory() -> str:
    lines = []
    for fn in sorted(os.listdir(user_dir())):
        if fn.endswith(".md") and fn not in NON_MEMORY_FNS:
            title = fn[:-3]
            with open(os.path.join(user_dir(), fn), encoding="utf-8") as f:
                content, _ = unpack_note(f.read())
            lines.append(f"- {title}: {content}")
    return "\n".join(lines) if lines else "（暂无记忆）"


# ---------------------------------------------------------------
# 3) 一个脑子，多个角色（每个 Agent 各配人设，职责清晰）
# 「只用云端 DeepSeek」：Windows 终端不继承用户级环境变量，
#  所以先查系统环境，再从注册表读用户级 DEEPSEEK_API_KEY，找不到就报错退出，
#  绝不再偷偷回退本地小模型（小模型会假执行，之前吃过亏）。
# ---------------------------------------------------------------
def _load_deepseek_key() -> str:
    """取 DEEPSEEK_API_KEY：进程环境 → Windows 用户级环境变量（注册表）→ exe 旁的 key 文件。
    key 文件供『发给朋友』场景用：对方首次启动时由界面弹窗引导填写并存盘。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            val, _ = winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")
            if val:
                os.environ["DEEPSEEK_API_KEY"] = str(val).strip()   # 顺手补进进程环境
                return str(val).strip()
    except Exception:
        pass
    try:
        key_path = os.path.join(BASE_DIR, "deepseek_key.txt")
        if os.path.exists(key_path):
            k = open(key_path, encoding="utf-8").read().strip()
            if k:
                os.environ["DEEPSEEK_API_KEY"] = k
                return k
    except Exception:
        pass
    return ""

def save_deepseek_key(key: str) -> None:
    """把 key 存到程序旁边的 deepseek_key.txt（发给朋友的便携用法）。"""
    key = (key or "").strip()
    if not key:
        return
    with open(os.path.join(BASE_DIR, "deepseek_key.txt"), "w", encoding="utf-8") as f:
        f.write(key)
    os.environ["DEEPSEEK_API_KEY"] = key

DEEPSEEK_API_KEY = _load_deepseek_key()
if not DEEPSEEK_API_KEY:
    raise SystemExit(
        "\n❌ 缺少 DEEPSEEK_API_KEY，无法连云端 DeepSeek。\n"
        "   1) 到 https://platform.deepseek.com 免费注册拿 Key；\n"
        "   2) 窗口版：直接双击 assistant_gui.py 或 私人助理.exe，会弹窗引导你填写；\n"
        "   3) 终端版：设置环境变量 DEEPSEEK_API_KEY，或在程序旁边建一个 deepseek_key.txt 写入 Key。"
    )

from langchain_deepseek import ChatDeepSeek
def make_model():
    """每个 Agent 用独立模型实例，避免 create_agent 的 bind_tools/序列化互相污染。"""
    return ChatDeepSeek(
        model="deepseek-flash",               # 官方现行模型名（DeepSeek-V4.1-Flash）
        api_key=DEEPSEEK_API_KEY,
        temperature=0.3,
    )
model = make_model()  # 保留一个主实例（用于横幅等）
receptionist_model, executor_model, secretary_model, chat_model, extractor_model, organizer_model = make_model(), make_model(), make_model(), make_model(), make_model(), make_model()
MODEL_SOURCE = "云端 DeepSeek (deepseek-flash)"

receptionist = create_agent(
    model=receptionist_model,
    tools=[],            # 前台不动手，只用嘴
    system_prompt=(
        "你是私人助理团队里的『前台』，负责理解用户意图。"
        "【严格规则】你只能输出下面六种格式之一，不要加任何别的客套话：\n"
        "1. 用户想让你记住某事 → 输出：记住→标题名|内容  （给个简短中文标题，内容写全）\n"
        "2. 用户问你还记得什么/你记得我什么/我的名字爱好等个人资料是什么 → 输出：回忆\n"
        "记住：凡是【问句】问『我叫什么/我是什么/你知道我……』，都是『回忆』不是『记住』！\n"
        "3. 用户想忘掉某条 → 输出：忘记→标题名（标题要跟已存笔记对应，如果只有一条笔记就写它的名字）\n"
        "4. 用户想修改/更新某条已有信息 → 输出：更新→标题名|新内容（标题要跟已存笔记对应，内容写改后的新值）\n"
        "5. 用户只是闲聊/打招呼/问一般性问题（不涉及记住、回忆、遗忘），或者想让助理安排日程、查天气、算数、设提醒、加待办、搜资料 → 输出：聊天"
        "（带？的疑问句，只要不是问记忆，都算聊天；『把X安排到某天』『明天有什么日程』也算聊天）\n"
        "6. 用户想让你整理/清理/归拢记忆 → 输出：整理\n"
        "示例：用户说『记住我爱吃火锅』→ 记住→喜好|我爱吃火锅\n"
        "      用户说『把爱好改成看电影』→ 更新→爱好|看电影\n"
        "      用户说『我叫什么名字？』→ 回忆\n"
        "      用户说『你好』→ 聊天\n"
        "      用户说『帮我整理一下记忆』→ 整理"
    ),
)

executor = create_agent(
    model=executor_model,
    tools=[save_note, recall_all, forget, update_note],
    system_prompt=(
        "你是私人助理团队里的『办事』Agent。你会收到前台给出的一个动作指令。"
        "【硬性要求】你必须调用对应的工具来真正完成动作："
        "- 指令是『记住』 → 调用 save_note（标题起个简短中文名，内容写要记住的话）；"
        "- 指令是『回忆』 → 调用 recall_all；"
        "- 指令是『忘记』 → 调用 forget（标题写要忘的那条）；"
        "- 指令是『更新』 → 调用 update_note（标题写要改的那条，内容写改后的新值）。"
        "必须调用工具，不许只动嘴。工具完成后用很短的话回答，如'已记住：生日'、'已更新：爱好'。"
    ),
)

secretary = create_agent(
    model=secretary_model,
    tools=[],
    system_prompt=(
        "你是私人助理团队里的『秘书』。你会看到某条『记忆内容』或『工具结果』。"
        "你的唯一任务：把看到的内容【如实、原样】念给用户听，不要编造、不要跑题。"
        "例如看到『记忆内容如下：- 生日：8月15号』就回答『我记得：生日是8月15号。』"
        "如果内容是『工具返回：已记住: X』就回答『好，已记住。』"
        "如果内容是『工具返回：已更新: X』就回答『好，已更新。』"
        "如果内容是『没有找到』就诚实说没找到。回答要短，一两句即可。"
    ),
)

# 『记忆秘书』：只动脑不动手——判断聊天里有没有值得长期记住的用户信息（执行交给确定性 Python）
extractor = create_agent(
    model=extractor_model,
    tools=[],            # 记忆秘书只看不说做，落盘由 Python 执行
    system_prompt=(
        "你是私人助理的记忆秘书，负责在每轮聊天后『摘记』值得长期记住的用户信息。"
        "你会看到用户刚刚说的话（带一点对话上下文）。"
        "【严格规则】只输出下面两种格式之一，每行一条，不要加任何别的字：\n"
        "1. 话里有值得长期记住的用户信息（关于用户本人的：事实/喜好/重要经历/正在经历的事/计划约定等）"
        " → 输出：记→标题名|内容\n"
        "2. 只是客套、寒暄、情绪发泄、给助理起名、问问题，或信息太琐碎不值得记 → 输出：无\n"
        "注意：标题要具体（如『前女友』『咖啡喜好』『最近的工作』），不要用『爱好/备注/记录/喜好』这类笼统词；"
        "只记关于用户的重要信息，不记废话；一句话里有多个要点就拆成多行输出。\n"
        "示例：用户说『最近被前女友伤透了心，她叫小雨』→ 记→前女友|用户被前女友小雨伤透了心\n"
        "      用户说『今天天气不错啊』→ 无"
    ),
)

# 『记忆整理员』：只动脑——看完整份记忆，给出合并/删除/更新的整理方案（执行交给确定性 Python）
organizer = create_agent(
    model=organizer_model,
    tools=[],
    system_prompt=(
        "你是私人助理团队的『记忆整理员』。你会看到当前记忆的清单（格式：- 标题：内容（记于 时间））。"
        "你的任务：找出 1) 内容重复/相近的条目→合并；2) 内容过时或互相矛盾、该清理的→删除；3) 需要修正的→更新。"
        "【严格规则】只输出下面几种指令行，每行一条，不要加任何别的字：\n"
        "合并→旧标题A+旧标题B|新标题|合并后内容\n"
        "删除→标题\n"
        "更新→标题|新内容\n"
        "如果记忆本来就清爽、不需要整理 → 输出：无\n"
        "注意：只做有价值的整理，别删用户重要的个人信息；每次改动尽量少；新标题要具体，不要用『爱好/备注/记录』这类笼统词。\n"
        "【硬规则】『名字』『默认城市』这类基础信息要单独保留：不许合并、改名或删除它们的标题。"
    ),
)


# ---------------------------------------------------------------
# 4) LangGraph 串成流水线：前台 → 办事 → 秘书
# ---------------------------------------------------------------
class AsstState(TypedDict):
    messages: list      # 本轮团队内流转的消息（前台判定用的）
    history: list       # 跨轮对话历史（干净的 用户/助理 消息对，聊天时用）
    memorized: str      # 记忆秘书本轮摘记落盘的结果（空串表示没记东西）
    tidied: str         # 记忆整理员本轮整理的结果（空串表示没整理）

def receptionist_node(state: AsstState) -> dict:
    result = receptionist.invoke({"messages": state["messages"]})
    return {"messages": result["messages"]}

def digest_title(raw: str) -> str:
    """从用户原话里挑一个简短中文词当笔记标题（规则法，不靠模型）。"""
    for kw in ("生日", "名字", "姓名", "爱好", "喜欢", "讨厌", "地址", "电话",
               "工作", "公司", "待办", "备注", "纪念日", "账号", "密码"):
        if kw in raw:
            return kw
    # 没有关键词，取首 4 个字
    head = raw[:4].replace("记住", "").replace("，", "").strip()
    return head or "记录"


def split_facts(raw: str) -> list[tuple[str, str]]:
    """把用户的一句话拆成 {槽位标题: 内容} 的多条记忆（规则法，不靠模型）。

    例：“记住，我是张俊浩，喜欢睡觉和打游戏，目前大三”
      → [("名字", "张俊浩"), ("爱好", "睡觉和打游戏"), ("年级", "大三")]
    """
    import re
    text = raw.replace("记住", "").strip()
    facts: list[tuple[str, str]] = []

    # 1) 名字：我是/我叫/我名字是/名字叫/我姓
    m = re.search(r"(我是|我叫|名字是|名字叫|我姓)(张三|李四|\S{1,8}?)(?:，|,|。|、|$)", text)
    if m:
        facts.append(("名字", m.group(2)))
        text = text.replace(m.group(0), "")   # 已消费，避免重复

    # 2) 年级/学历：大三/大二/研究生/读研…
    m = re.search(r"(目前|现在)?(大一|大二|大三|大四|研一|研二|研三|在读本科|在读硕士|高中生|初中生)", text)
    if m:
        facts.append(("年级", m.group(2)))
        text = text.replace(m.group(0), "")

    # 3) 爱好/喜欢：喜欢/爱/爱吃/爱玩 …
    m = re.search(r"(喜欢|爱|爱吃|爱玩|爱打|爱听|爱看|爱玩)(.+?)(?:，|,|。|、|$)", text)
    if m:
        facts.append(("爱好", m.group(2).strip()))
        text = text.replace(m.group(0), "")

    # 4) 剩下的段落整体作为一条备注（去掉口语化残余；太短的残词如"我"则丢弃）
    rest = re.sub(r"(目前|现在|还)|(，|,|。)+", " ", text).strip()
    rest = rest.strip("我你我她他是在对的  ")
    if rest and len(rest) >= 2 and not any(k in rest for k in ("帮我记住", "记住", "记为")):
        facts.append(("备注", rest))

    if not facts:  # 什么都没拆出来，原样存一条
        facts.append(("记录", raw.strip().replace("记住", "")))

    # 标题冲突去重（同名合并到已有？这里简单：同名则内容叠加用逗号）
    merged: dict[str, list[str]] = {}
    for title, content in facts:
        merged.setdefault(title, []).append(content)
    return [(t, "、".join(v)) for t, v in merged.items()]


# 问"自己的资料"也是回忆，不是记住（防止模型把问句当成陈述存进去）
RECALL_HINTS = ("记得", "回忆", "记住了啥", "纪要")
ASK_INFO_HINTS = ("我叫什么", "我是谁", "我的爱好", "我的名字", "我多大了", "知道我叫", "知道我", "我叫的名字", "我喜欢做什么", "我喜欢什么")

def summarize_recent_chat(history: list) -> str:
    """把最近一段对话的要点总结成记忆笔记。
    按日期存档（对话纪要_YYYY-MM-DD），同一天内多次总结会合并到同一份，
    跨天各自保留——所以总结多少次也不会覆盖丢失。聊得太短就返回空串。"""
    if len(history) < 6:
        return ""
    ctx = [
        SystemMessage(content=(
            "你是私人助理的『对话总结员』。把下面这段对话的要点提炼成一段简短总结：\n"
            "只保留值得长期记住的事实、偏好、决定、正在经历的事；丢掉寒暄客套；"
            "用 3~5 句话，不要前后缀，直接输出总结正文。"
        )),
    ] + list(history[-16:])
    try:
        ai = chat_model.invoke(ctx)
    except Exception:
        return ""
    summary = (ai.content or "").strip()
    if not summary:
        return ""
    title = f"对话纪要_{datetime.datetime.now().strftime('%Y-%m-%d')}"
    # 同一天的多次总结：新旧合并，不覆盖
    path = os.path.join(user_dir(), f"{title}.md")
    if os.path.exists(path):
        old, _ = unpack_note(open(path, encoding="utf-8").read())
        summary = f"{old}；{summary}"
    save_note.invoke({"title": title, "content": summary})
    return summary

def executor_node(state: AsstState) -> dict:
    # 工具执行完全交给确定性 Python，不让模型二次猜测/重复执行。
    # 只取【本轮用户输入】：messages 里第一条 human 消息（每轮只传本轮输入）
    user_raw = ""
    for m in reversed(state["messages"]):
        if m.type == "human" and m.content:
            user_raw = m.content
            break
    user_raw = user_raw.strip()

    # 意图判定（关键词，简单可靠）——待办/提醒优先，因为"提醒/记得"会和记忆命令抢
    todo_words = ("待办", "提醒", "别忘了")
    if any(w in user_raw for w in todo_words):
        is_remind = ("提醒" in user_raw) or ("别忘了" in user_raw)
        at, content = parse_remind_time(user_raw) if is_remind else (None, "")
        if is_remind and at and content:
            # —— 定时提醒：话里有具体时间 + 内容 → 真设提醒 ——
            note = f"提醒设置结果：{add_reminder(at, content)}"
        elif is_remind and at and not content:
            note = f"（时间记住了：{at.strftime('%m月%d日 %H:%M')}，但要提醒什么内容呢？）"
        elif is_remind and any(w in user_raw for w in ("看", "查", "什么", "哪些", "有")):
            note = f"提醒列表：\n{list_reminders()}"
        elif is_remind and any(w in user_raw for w in ("删", "取消", "移除")):
            kw = pick_todo_kw(user_raw)
            note = f"取消提醒结果：{cancel_reminder(kw)}" if kw else "（要取消哪条提醒？）"
        elif any(w in user_raw for w in ("加", "添加", "新增", "记个", "记一个", "写", "设", "提醒", "别忘了")):
            m_item = re.search(r"(?:提醒我?|记个?待办|加个?待办|添加个?待办|新增待办|设个?提醒|别忘了|写上)[，,：: ]*(.+)", user_raw)
            item = m_item.group(1).strip().strip("，,。！!") if m_item else ""
            if not item:   # 兜底：把命令词剥掉剩下的当内容
                item = user_raw
                for w in ("添加", "新增", "加个待办", "记个待办", "待办", "提醒我", "提醒", "别忘了", "设置", "写"):
                    item = item.replace(w, "")
                item = item.strip().strip("，,：:。！!")
            note = f"待办添加结果：{add_todo_item.func(item)}" if item else "（想提醒我做什么呢？）"
        elif any(w in user_raw for w in ("完成", "搞定", "做完", "做掉")):
            kw = pick_todo_kw(user_raw)
            note = f"待办完成结果：{done_todo(kw)}" if kw else "（哪件待办完成啦？）"
        elif any(w in user_raw for w in ("删", "取消", "移除", "忘掉")):
            kw = pick_todo_kw(user_raw)
            note = f"待办删除结果：{remove_todo(kw)}" if kw else "（要删哪件待办？）"
        else:
            note = f"待办清单如下：\n{list_todos()}"
    elif any(w in user_raw for w in ("忘记", "忘掉", "删掉")):
        title = ""
        # 先按标题精确匹配
        for fn in sorted(os.listdir(user_dir())):
            if fn.endswith(".md") and fn[:-3] in user_raw:
                title = fn[:-3]
                break
        # 再按内容模糊匹配：标题里没"猫"但内容里有 → 也删
        if not title:
            for fn in sorted(os.listdir(user_dir())):
                if not fn.endswith(".md"):
                    continue
                with open(os.path.join(user_dir(), fn), encoding="utf-8") as f:
                    content, _ = unpack_note(f.read())
                if content and any(k in content for k in (user_raw, user_raw.replace("忘记", "").replace("忘掉", "").replace("删掉", "").replace("那条", "").replace("的", "").strip())):
                    title = fn[:-3]
                    break
        if not title:  # 全没匹配到，就从话里截标题（可能不准）
            for w in ("忘记", "忘掉", "删掉", "那条", "的记忆", "的笔记"):
                user_raw = user_raw.replace(w, "").replace("，", "").replace(",", "").strip()
            title = user_raw
        out = forget.invoke({"title": title})
        note = f"记忆内容如下：\n{out}" if "没有找到" in out else f"工具返回：{out}"
    elif "默认城市" in user_raw and any(w in user_raw for w in ("设", "改", "换", "是")):
        # 默认城市：专属分支，支持『默认城市换成上海』『把默认城市设为成都』等说法
        m_city_set = re.search(r"(?:把默认城市|默认城市)(?:设为|改成|改为|换成|更新为?|是)[：:是]?\s*([\u4e00-\u9fa5A-Za-z]{2,12})", user_raw)
        city = m_city_set.group(1).strip() if m_city_set else ""
        if city:
            out = update_note.invoke({"title": "默认城市", "content": city})
            note = f"工具返回：{out}"
        else:
            note = "（默认城市想设成哪儿呢？）"
    elif any(w in user_raw for w in ("总结", "纪要")) and any(w in user_raw for w in ("对话", "聊", "刚才", "我们")):
        # 对话总结：把最近这段对话提炼存档（模型出稿，Python 落盘）
        s = summarize_recent_chat(state.get("history", []))
        note = f"已把这段对话总结存档：\n{s}" if s else "（这段对话还太短，没什么好总结的～）"
    elif any(w in user_raw for w in ("改成", "改为", "换成", "更新", "改一下", "修正", "改主意", "设为")):
        # 更新：先把"把A改成B"里要改的旧词 A 抠出来，用于在内容里定位是哪条笔记
        m_old = re.search(r"把(.+?)(?:改成|改为|换成|设为)", user_raw)
        old_kw = re.sub(r"[，,。、]|(我的|那个|那条|这个)", "", m_old.group(1)).strip() if m_old else ""
        # 新内容：取"改成/改为/换成/设为/更新为"之后的部分
        m_new = re.search(r"(?:改成|改为|换成|设为|更新为?|改成是|其他)[为是:]?\s*(.+)", user_raw)
        new_content = m_new.group(1).strip().strip("，,。") if m_new else ""
        # 定位标题：1) 话里直接含已存标题词  2) 旧词在内容里模糊匹配
        title = ""
        for fn in sorted(os.listdir(user_dir())):
            if fn.endswith(".md") and fn[:-3] in user_raw:
                title = fn[:-3]
                break
        if not title and old_kw:
            for fn in sorted(os.listdir(user_dir())):
                if not fn.endswith(".md"):
                    continue
                with open(os.path.join(user_dir(), fn), encoding="utf-8") as f:
                    c, _ = unpack_note(f.read())
                if old_kw and old_kw in c:
                    title = fn[:-3]
                    break
        if not title:
            title = old_kw if old_kw else "记录"   # 没有现成标题 → 用"旧词"当标题（把X改成Y，X即主题）
        if not new_content:  # 实在没提取出新值，就整句丢进去兜底
            new_content = user_raw
        out = update_note.invoke({"title": title, "content": new_content})
        note = f"工具返回：{out}"
    elif any(w in user_raw for w in ("整理", "清理记忆", "归拢")):
        # 整理：让记忆整理员出方案，确定性执行（前台误判时这层也能兜住）
        summary = run_tidy()
        note = f"整理结果：{summary}" if summary else "记忆不多，没什么要整理的。"
    elif any(w in user_raw for w in RECALL_HINTS) or (
            any(q in user_raw for q in ASK_INFO_HINTS)
            and not user_raw.startswith(("记住", "帮我记住", "记一下", "记着"))):
        out = recall_all.invoke({})
        note = f"记忆内容如下：\n{out}"
    else:
        # 兜底防护①：带问号/疑问词的句子大概率是提问，不是要记住的内容（模型偶尔会误判）
        if not user_raw.startswith(("记住", "帮我记住", "记一下", "记着", "记为")) and any(
                q in user_raw for q in ("？", "?", "吗", "什么", "啥", "呢", "怎么", "等于", "多少", "天气", "气温", "几度", "几点", "几号")):
            note = "（这句像是问句，我先不存。想让我记住什么，直接告诉我就行～）"
        elif (naming := remember_assistant_name(user_raw)):
            # 给助理自己起名：存助理名字槽，绝不碰用户名字
            note = naming
        else:
            # 记住：把复合信息拆成【名字/年级/爱好/备注】多条笔记分开存
            facts = split_facts(user_raw)
            # 兜底防护②：前台偶尔把问候误判成"记住"时，别让问候被存成笔记
            if (len(facts) == 1 and facts[0][0] in ("备注", "记录")
                    and any(g in user_raw.lower() for g in ("你好", "您好", "嗨", "哈喽", "hello", "hi", "hey", "谢谢", "再见", "在吗", "你是谁", "你会什么", "介绍一下你自己"))):
                note = "（这句不像要记住的内容，先不存。有什么想让我记的吗？）"
            else:
                results = []
                for title, content in facts:
                    if title in ("备注", "记录"):
                        # 泛化标题同名会互相覆盖，改为追加合并，防止不同轮的事实丢失
                        path = os.path.join(user_dir(), f"{title}.md")
                        if os.path.exists(path):
                            old, _ = unpack_note(open(path, encoding="utf-8").read())
                            content = f"{old}；{content}"
                        out = save_note.invoke({"title": title, "content": content})
                    else:
                        out = save_note.invoke({"title": title, "content": content})
                    results.append(str(out))
                note = "；".join(results)

    return {"messages": [HumanMessage(content=note)]}

def secretary_node(state: AsstState) -> dict:
    result = secretary.invoke({"messages": state["messages"]})
    return {"messages": result["messages"]}

@tool
def set_reminder(when: str, text: str) -> str:
    """设置一个定时提醒。when 用时间说法，如『明天9点』『3分钟后』；text 是要提醒的具体事（简短，不要带条件描述）。
    如果用户的要求带条件（如『下雨就提醒』），必须先调用其他工具核实条件是否满足，满足才设；不满足就直接说明。"""
    at, content = parse_remind_time(f"{when}提醒我{text}")
    if not at:
        return f"（时间『{when}』没看懂，请用『明天9点』『3分钟后』这类说法）"
    return add_reminder(at, content or text)

@tool
def add_todo_item(item: str) -> str:
    """往用户待办清单加【一件事】。item 只写那件事本身（例：『买牛奶』），不要写『把…加到待办』这类套话。
    用户一次说多件事（如『买牛奶和写作业』）时，必须拆开，对每件事分别调用一次本工具。"""
    # 确定性清洗+拆分：模型传参不完美时兜底（剥套话 → 按顿号/和/与拆成多件分别落盘）
    cleaned = item
    for w in ("加到我的待办", "加到我的待办里", "加进待办", "加入待办", "加到我的", "我的待办",
              "待办清单", "待办", "把", "请", "麻烦", "给我", "帮我"):
        cleaned = cleaned.replace(w, "")
    parts = [p.strip() for p in re.split(r"[，,、；;]|和|与|以及", cleaned) if p.strip()]
    if len(parts) > 1:
        return "；".join(add_todo(p) for p in parts)
    return add_todo(cleaned.strip())

# 聊天模型可自主调用的工具（含两个低危『写』工具：设提醒/加待办；
# 记忆文件的增删改仍走确定性 executor，不交给模型）
CHAT_TOOLS = [calculate, get_weather, get_current_time, list_todos_tool, search_web, morning_report,
              set_reminder, add_todo_item, add_schedule, list_schedule_tool]
CHAT_TOOL_REGISTRY = {t.name: t for t in CHAT_TOOLS}

def run_side_effects(user_raw: str) -> str:
    """前台误判成闲聊时，『写』操作的兜底。提醒/待办已由模型工具负责，
    这里只兜底默认城市设置（防止模型没调工具时漏掉）。"""
    hints = []
    # 默认城市：『把默认城市设为X』真落盘
    m_city_set = re.search(r"(?:把默认城市|默认城市)(?:设为|改成|改为|换成|更新为?)[：:是]?\s*([\u4e00-\u9fa5A-Za-z]{2,12})", user_raw)
    if m_city_set:
        city = m_city_set.group(1).strip()
        hints.append("[已更新默认城市] " + str(update_note.invoke({"title": "默认城市", "content": city})))
    # 给助理起名：聊天路径兜底（存入助理名字槽，不碰用户名字）
    if (named := remember_assistant_name(user_raw)):
        hints.append("[已记住助理名字] " + named)
    return "\n".join(hints)

def chat_node(state: AsstState) -> dict:
    """『聊天』节点（真·工具 Agent 循环）：
    模型自己判断要不要调工具 → 若调用，Python 确定性执行 → 结果还给模型 → 直到给出最终回答。
    最多允许 3 轮工具调用，防止无限循环。"""
    user_raw = ""
    for m in reversed(state["messages"]):
        if m.type == "human" and m.content:
            user_raw = m.content
            break
    long_mem = load_long_memory()
    system_extra = ""
    if fire_log():                        # 后台已经响过的提醒，要让聊天席知道，别再说"还在等"
        system_extra = f"\n系统近况（已发生的事，别把它当未来计划）：{'；'.join(fire_log())}"
    asst_name = get_assistant_name()
    name_hint = f"\n你（助理）的名字叫『{asst_name}』，用户问起你的名字要回答这个。" if asst_name else ""
    msgs: list = [
        SystemMessage(
            content=(
                "你是私人助理团队里的『聊天』担当，语气温柔，回答简短自然。\n"
                "你有几个工具可用：查天气(get_weather，可查今天/明天/后天)、算数(calculate)、查当前时间(get_current_time)、"
                "看待办清单(list_todos_tool)、联网搜索(search_web)、每日晨报(morning_report)、"
                "设提醒(set_reminder)、加待办(add_todo_item)、"
                "记日程(add_schedule)、查日程(list_schedule_tool)。"
                "遇到实时新闻/概念/百科类问题用 search_web 搜一下再回答；用户要你设置提醒/加待办/记日程时用对应工具真实执行。\n"
                "用户说『每日晨报』『今日晨报』『看晨报』『汇报今天的安排』时就调 morning_report 生成；"
                "多工具任务示例：用户说『明天下雨就提醒我带伞』→ 第一步调 get_weather 查明天降水概率 → "
                "第二步根据结果决定：会下雨才调 set_reminder（text 只写『带伞』，when 用用户给的时间，没给就用明天8点这种合理时间）；"
                "不下雨就直接告诉用户明天不用带伞。用户一次说多件事的待办，要拆开、对每件事分别调一次 add_todo_item。\n"
                "联动技巧：用户问某天的天气或安排时，可以顺带用 list_schedule_tool 查那天有没有日程，"
                "有的话结合天气给建议（如下雨建议改室内或带伞）；用户说『把X安排到某天』就用 add_schedule。\n"
                "没搜到就说没搜到，不要自己心算或瞎编数据。搜索技巧：用 2~4 个字的核心关键词（去掉『听说/吗/了』等虚词），"
                "第一轮搜到的内容不相关时，换个更短的关键词再搜一轮再作答。\n"
                "下面这些是用户之前让你记住的信息，聊到相关话题时自然地引用，"
                "不知道的事就直说不知道，不要编造。\n"
                f"{long_mem}{system_extra}{name_hint}"
            )
        ),
    ] + list(state.get("history", [])) + [HumanMessage(content=user_raw)]

    # 0) 关键『写』操作的兜底（加待办/设提醒/设默认城市）——确定性落盘，绝不交给模型写
    side = run_side_effects(user_raw)
    if side:
        msgs.append(HumanMessage(content=f"[刚刚已执行的操作] {side}"))
    # 对话总结：用户要求"总结对话"却被前台误判成闲聊时，这里兜底存档
    if ("总结" in user_raw or "纪要" in user_raw) and any(w in user_raw for w in ("对话", "聊", "刚才", "我们")):
        s = summarize_recent_chat(state.get("history", []))
        if s:
            msgs.append(HumanMessage(content=f"[对话总结已存档] {s}"))

    # 1) 模型自主调工具的主循环（bind_tools：给模型看工具说明书，让它自己决定）
    final = None
    llm = chat_model.bind_tools(CHAT_TOOLS, tool_choice="auto")
    for _ in range(3):
        ai = llm.invoke(msgs)
        msgs.append(ai)
        if getattr(ai, "tool_calls", None):
            for tc in ai.tool_calls:
                try:
                    out = CHAT_TOOL_REGISTRY[tc["name"]].invoke(tc.get("args") or {})
                except Exception as e:
                    out = f"（工具执行出错：{e}）"
                msgs.append(ToolMessage(content=str(out), name=tc["name"], tool_call_id=tc["id"]))
            continue                    # 工具结果已回填，让模型再想一次
        final = ai
        break
    if final is None or not final.content:
        # 三轮都在调工具还没给答案，或模型没说出话：诚实兜底
        return {"messages": [HumanMessage(content=user_raw),
                             AIMessage(content="（我有点绕晕了，你换个说法再问我一次？）")]}
    return {"messages": [HumanMessage(content=user_raw), final]}

def extractor_node(state: AsstState) -> dict:
    """『记忆秘书』节点：聊天结束后，把话里值得记的用户信息摘记落盘。
    模型只负责『判断』，真正的文件写入由 Python 执行，防止小模型假执行。"""
    user_raw = ""
    for m in reversed(state["messages"]):
        if m.type == "human" and m.content:
            user_raw = m.content
            break
    # 给摘记一点上下文：最近几轮对话 + 本轮，让它能接住「她」「刚才说的」这类指代
    ctx = list(state.get("history", []))[-6:] + [HumanMessage(content=user_raw)]
    out = extractor.invoke({"messages": ctx})
    directive = ""
    for m in reversed(out["messages"]):
        if m.type == "ai" and not m.tool_calls and m.content:
            directive = m.content
            break
    saved = []
    seen = set()          # 同一轮里同标题只存一次，避免重复覆盖
    for line in directive.splitlines():
        line = line.strip()
        if line.startswith("记→") and "|" in line:
            title, content = line[2:].split("|", 1)
            title = title.strip()
            if title in seen:
                continue
            seen.add(title)
            res = save_note.invoke({"title": title, "content": content.strip()})
            saved.append(str(res))
    # 不追加 messages：保持聊天席那句回答作为本轮最终回复；摘记结果单独上报
    return {"memorized": "；".join(saved) if saved else ""}


def list_notes() -> list[dict]:
    """把所有记忆笔记读出来（标题/纯净内容/记录时间），整理员和主循环都用。"""
    items = []
    for fn in sorted(os.listdir(user_dir())):
        if fn.endswith(".md") and fn not in NON_MEMORY_FNS:
            with open(os.path.join(user_dir(), fn), encoding="utf-8") as f:
                content, ts = unpack_note(f.read())
            items.append({"title": fn[:-3], "content": content, "time": ts})
    return items


def run_tidy() -> str:
    """自动整理记忆：整理员 Agent 看完全部笔记给出方案，Python 确定性执行。
    返回整理报告（空串 = 没问题需要整理）。"""
    items = list_notes()
    if len(items) < 3:
        return ""          # 没几条，没必要整理
    listing = "\n".join(f"- {it['title']}：{it['content']}（记于 {it['time'] or '未知'}）" for it in items)
    out = organizer.invoke({"messages": [HumanMessage(content=f"当前记忆清单如下：\n{listing}")]})
    directive = ""
    for m in reversed(out["messages"]):
        if m.type == "ai" and not m.tool_calls and m.content:
            directive = m.content
            break
    report = []
    for line in directive.splitlines():
        line = line.strip().lstrip("-* ")
        if line.startswith("合并→"):
            parts = line[len("合并→"):].split("|", 2)
            if len(parts) == 3:
                olds = [p.strip() for p in parts[0].split("+")]
                new_title, new_content = parts[1].strip(), parts[2].strip()
                exist = [t for t in olds if os.path.exists(os.path.join(user_dir(), f"{t}.md"))]
                if exist and new_title:
                    for t in olds:
                        forget.invoke({"title": t})
                    save_note.invoke({"title": new_title, "content": new_content})
                    report.append(f"合并 {'+'.join(olds)} → {new_title}")
        elif line.startswith("删除→"):
            t = line[len("删除→"):].strip()
            if "已忘记" in str(forget.invoke({"title": t})):
                report.append(f"删除 {t}")
        elif line.startswith("更新→"):
            parts = line[len("更新→"):].split("|", 1)
            if len(parts) == 2:
                update_note.invoke({"title": parts[0].strip(), "content": parts[1].strip()})
                report.append(f"更新 {parts[0].strip()}")
    return "；".join(report) if report else ""


def organizer_node(state: AsstState) -> dict:
    """『记忆整理员』节点：用户要求整理时执行，并回一句结果。"""
    summary = run_tidy()
    if summary:
        reply = f"记忆整理好啦：{summary}"
    else:
        reply = "我看了看，记忆挺清爽的，暂时不用整理～"
    return {"tidied": summary, "messages": [AIMessage(content=reply)]}


def route(state: AsstState) -> str:
    """前台之后的条件边：判定这轮是『闲聊』『整理记忆』还是『记忆命令』。"""
    last = state["messages"][-1]
    content = (last.content or "").strip()
    if content.startswith("聊天"):
        return "chat"
    if content.startswith("整理"):
        return "organizer"
    return "executor"

g = StateGraph(AsstState)
g.add_node("receptionist", receptionist_node)
g.add_node("chat", chat_node)
g.add_node("extractor", extractor_node)
g.add_node("organizer", organizer_node)
g.add_node("executor", executor_node)
g.add_node("secretary", secretary_node)
g.add_edge(START, "receptionist")
g.add_conditional_edges("receptionist", route,
                        {"chat": "chat", "organizer": "organizer", "executor": "executor"})
g.add_edge("chat", "extractor")
g.add_edge("extractor", END)
g.add_edge("organizer", END)
g.add_edge("executor", "secretary")
g.add_edge("secretary", END)
team = g.compile()


if __name__ == "__main__":
    # ---------------------------------------------------------------
    # 5) 交互式聊天
    # ---------------------------------------------------------------
    print("=" * 58)
    print("🤵 你的私人助理已就位（多Agent团队版）")
    print(f"   🧠 当前用脑：{MODEL_SOURCE}")
    print("   能帮你：记住事项 / 回忆 / 忘掉 / 陪你闲聊 / 查天气 / 算数 / 待办 / 定时提醒")
    print("   聊到重要的事（喜好、经历、烦恼…）我会自动记下来，以后随时能回忆")
    print("   每条记忆都带记录时间；说『帮我整理一下记忆』我会合并重复、清理过时")
    print("   试试：『加个待办：明天买牛奶』『3分钟后提醒我喝水』『看看我的提醒』")
    print("   聊满几轮我会自动把对话要点存成『对话纪要』；想立刻做就说『总结一下我们的对话』")
    print("   想偷懒：先跟我说『把默认城市设为成都』，之后问天气就不用带城市了")
    print("   输入 退出 结束")
    print("=" * 58)

    # 主动汇报：像真人秘书一样先开口报今天的情况
    print("📋 主动汇报")
    print(make_startup_briefing())
    print("=" * 58)

    # 启动后台提醒线程：到点会在屏幕上响铃喊你
    threading.Thread(target=_reminder_watcher, daemon=True).start()

    history: list = []   # 短线对话历史（干净的 用户/助理 消息对，跨轮携带）
    MAX_HISTORY = 12     # 最多保留最近 ~6 轮，避免对话越长、模型输入越臃肿
    AUTO_TIDY_EVERY = 5  # 每聊 5 轮自动整理一次记忆
    SUMMARY_EVERY = 6   # 每聊 6 轮自动把对话要点总结存档
    turn = 0             # 轮次计数（用于触发自动整理/总结）
    while True:
        try:
            text = input("\n你：").strip()
        except EOFError:
            print("\n再见！我记住的东西都在 assistant_memory/ 里。👋")
            break
        if text.lower() in ("exit", "quit", "退出"):
            print("再见！我记住的东西都在 assistant_memory/ 里。👋")
            break
        if not text:
            continue

        # 交给团队：本轮输入 + 之前的对话历史（长线记忆由文件负责，重启不忘）
        result = team.invoke({
            "messages": [HumanMessage(content=text)],
            "history": history,
            "memorized": "",
            "tidied": "",
        })

        # 找出本轮最后的回答（记忆命令→秘书说的；闲聊→聊天节点说的）
        final_reply = ""
        for m in reversed(result["messages"]):
            if m.type == "ai" and not m.tool_calls and m.content:
                final_reply = m.content
                break

        # 若本条是"回忆"，直接把记忆原文打印出来，保证用户一定看到真相
        if any(w in text for w in RECALL_HINTS) or (
                any(q in text for q in ASK_INFO_HINTS)
                and not text.startswith(("记住", "帮我记住", "记一下", "记着"))):
            raw_mem = recall_all.invoke({})
            print("   📒 原始记忆：", raw_mem.replace("\n", "；").strip())

        # 把这一来一回记进短线历史（最早的超了窗口就丢），并回显给用户
        if final_reply:
            history = (history + [HumanMessage(content=text), AIMessage(content=final_reply)])[-MAX_HISTORY:]
            print("助理：", final_reply)

        # 记忆秘书摘记了什么，补一句让用户知道
        if result.get("memorized"):
            print("   🧠 已摘记：", result["memorized"])

        # 定期自动整理：聊到整轮数时，悄悄把记忆归拢一遍（本轮刚手动整理过就跳过，避免重复）
        turn += 1
        if turn % AUTO_TIDY_EVERY == 0 and not result.get("tidied"):
            auto = run_tidy()
            if auto:
                print("   🧹 自动整理：", auto)

        # 定期对话总结：聊够一轮就自动把要点存档，之后能回看"我们聊过什么"
        if turn % SUMMARY_EVERY == 0 and len(history) >= 6:
            s = summarize_recent_chat(history)
            if s:
                print("   📋 这段对话的要点已自动存档（说『看看对话纪要』或『回忆』可回看）")