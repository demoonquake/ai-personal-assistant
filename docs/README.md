# 私人助理（AI Personal Assistant）v0.2

一个从零学习 Agent 开发过程中长出来的**桌面级私人助理**：有记忆、有日程、会用工具、能联网，支持终端 / 桌面窗口 / 浏览器三种使用方式。v0.2 起浏览器版支持**多用户**：每人独立记忆、互不可见。

> 技术栈：Python 3.12 · LangChain + LangGraph · DeepSeek API · FastAPI · 原生 HTML/CSS/JS

## ✨ 功能一览

| 能力 | 说明 |
|---|---|
| 多轮对话 | 跨轮上下文 + 长线记忆（存文件，重启不忘） |
| 记忆管理 | 记住 / 回忆 / 遗忘 / 更新，自动摘记聊天要事，按日期保存对话纪要，定时自动整理 |
| 时效信息 | 查时间（含星期）、三天天气预报（默认城市 + 降水概率） |
| 工具自主调用 | 模型自己决定调哪个工具、怎么组合（bind_tools Agent 循环） |
| 联网搜索 | 必应 + DuckDuckGo + 维基，实时新闻/百科可查 |
| 待办清单 | 增 / 查 / 完成 / 删，含记录时间 |
| 定时提醒 | 支持"3分钟后 / 明天9点 / 周三晚上7点 / 十秒钟之后"等说法，到点响铃 |
| 日程表 | 某天几点做什么，与天气、提醒联动 |
| 主动汇报 | 启动时先开口：今日待办、提醒、最近的日程（有明日日程自动附明日天气） |
| 人事分离 | 用户的名字与助理自己的名字分开存储，互不污染 |
| **多用户（浏览器版）** | 用户名+可选密码登录，每人独立记忆/待办/提醒/日程，互不可见 |

## 🚀 快速开始

需要：Python 3.12 + [DeepSeek API Key](https://platform.deepseek.com)（注册免费送额度）

```bash
# 1) 安装依赖
python -m pip install -r requirements.txt

# 2) 选一个入口启动：

#  终端版（最轻）
python src/personal_assistant.py

#  桌面窗口版（Tkinter，深色主题）
python src/assistant_gui.py

#  浏览器版（访问 http://127.0.0.1:8000）
python src/server.py
```

首次启动会引导你填写 DeepSeek API Key（自动保存到程序旁的 `deepseek_key.txt`）。
三个入口**共用同一份记忆库**（`runtime/assistant_memory/`），随便切换。

## 🏗 项目结构

```
├── src/                    # Python 源码入口
│   ├── personal_assistant.py   # 内核：团队编排 + 全部工具 + 记忆/待办/提醒/日程数据层
│                           #（v0.2：数据层支持『每线程一个用户目录』的多用户切换）
│   ├── assistant_gui.py        # 窗口版界面（Tkinter，三件套里最'桌面'的）
│   └── server.py               # 浏览器版后端（FastAPI：v0.2 含注册/登录/token 鉴权）
├── web/static/              # 浏览器版前端（index.html + style.css + app.js，纯原生）
├── requirements.txt        # 依赖清单
├── docs/                   # 项目说明与许可证
├── runtime/                # 运行后生成的数据（已 gitignore）
│   ├── data/               # 浏览器版用户数据
│   └── assistant_memory/   # 终端/窗口版单用户记忆
├── cache/models/           # 可选向量模型缓存（已 gitignore）
├── packaging/              # PyInstaller spec 与图标
├── releases/               # 发布压缩包
└── build_artifacts/        # 构建产物
```

## 👥 浏览器版多用户（v0.2 新增）

打开页面先登录：输入一个**用户名**（字母/数字）+ 可选密码（留空 = 无密码保护）。
- 首次输入即自动注册；密码用 SHA-256 加盐哈希存储（`runtime/data/users.json`）
- 每人独立的数据目录 `runtime/data/users/<名字>/assistant_memory/`，记忆、待办、提醒、日程全部隔离
- 登录态保存在浏览器 localStorage，重启服务后需重新登录（单机自用足够）

## 🧠 架构

`你说话 → 前台 Agent 分意图 → 聊天席（自主调工具）/ 记忆流水线 → 回答`

- 长线记忆 = runtime/assistant_memory/ 下的笔记文件（带时间戳）
- 数据表 = todo.md / reminders.md / schedule.md（与记忆严格隔离，整理员不碰）
- 聊天工具集：天气、算数、时间、待办查/加、提醒设、日程记/查、联网搜索（9 个，模型自主编排）

## ⚙️ Key 配置方式（三选一）

1. 环境变量 `DEEPSEEK_API_KEY`
2. Windows 用户级环境变量（程序会自动从注册表读取）
3. 程序旁的 `deepseek_key.txt`（首次启动有引导，最省事）

## 📄 License

MIT
