# 私人助理（网页版）· 本地部署指南

## 你需要准备

1. 一台装了 **Python 3.12** 的电脑（https://www.python.org/downloads/ 安装时勾选 Add to PATH）
2. 一个 DeepSeek API Key（免费注册：https://platform.deepseek.com ，注册送 500 万 token）

## 三步启动

解压本压缩包，在解压出来的文件夹里打开终端（PowerShell），依次执行：

```powershell
# 第 1 步：安装依赖（只需要第一次执行）
python -m pip install -r requirements.txt

# 第 2 步：启动
python server.py
```

第一次运行会提示你粘贴 DeepSeek Key，粘进去回车（会自动保存，以后不用再填）。

```powershell
# 第 3 步：打开浏览器，访问
http://127.0.0.1:8000
```

看到「主动汇报」就成功了。浏览器页面直接聊天即可。

## 停止

回到跑 `python server.py` 的终端，按 `Ctrl + C`。

## 说明

- 全部数据存在本文件夹的 assistant_memory/（不会上传任何地方）
- 仅本机可访问，安全
- 依赖清单见 requirements.txt