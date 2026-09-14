"""
语音交互模块（桌面图形界面用 · 可选依赖）
=========================================
提供三段能力（全部在函数内延迟 import，不装语音依赖也能 import 本模块）：
  · record_audio(stop_event)    —— 麦克风录音（sounddevice），阻塞直到 stop_event 触发
  · transcribe(wav_path)        —— 本地中文识别（faster-whisper，完全离线）
  · speak(text)                 —— 朗读（edge-tts 生成 + Windows winmm 播放，需要联网）
  · speech_available()          —— 探测依赖是否齐全（给 UI 决定按钮灰不灰）

模型约定：首次点话筒自动从 HuggingFace 下载 ~150MB 中文小模型到『项目根/cache/models/whisper』，
之后完全离线。国内网络先执行：$env:HF_ENDPOINT = "https://hf-mirror.com"
"""

import os
import re
import sys
import wave
import ctypes
import asyncio
import tempfile
import threading

# 项目根：源码在 src/ 下，语音模型与向量模型同一约定放 cache/models/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHISPER_DIR = os.path.join(BASE_DIR, "cache", "models", "whisper")

_WHISPER_MODEL = None
_WHISPER_LOCK = threading.Lock()
_MCI_LOCK = threading.Lock()          # winmm 播放是"一个设备"，串行访问防乱
_LAST_MP3 = ""                        # 正在播放的 mp3 路径（替换时才删除）
_HAS_PROXY = None                     # 系统代理是否已探测（缓存，避免每句都查注册表）


def speech_available() -> tuple:
    """返回 (是否可用, 说明)。只做 import 探测，不做下载。"""
    missing = []
    for name in ("sounddevice", "faster_whisper", "edge_tts", "numpy"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        return False, ("缺少语音依赖：{}（pip install -r requirements-voice.txt）"
                       .format("、".join(sorted(missing))))
    return True, "可用"


def model_ready() -> bool:
    """whisper 模型是否已加载过（首次识别会自动下载/加载，之后复用单例秒识别）。"""
    return _WHISPER_MODEL is not None


# ---------------- 录音 ----------------

_REC_PEAK = 0.0                      # 当前录音电平（0.0~1.0），回调里实时更新
_REC_LOCK = threading.Lock()

def rec_level() -> float:
    """录音中的实时音量（0.0~1.0），给界面显示电平条；没在录则为 0。"""
    with _REC_LOCK:
        return _REC_PEAK

def record_audio(stop_event: threading.Event) -> str:
    """录音直到 stop_event 被 set。返回临时 wav 路径（16k mono int16）；没录到声音返回 ""。"""
    import sounddevice as sd
    import numpy as np

    global _REC_PEAK
    frames = []
    # 按设备默认采样率录（PortAudio 会做设备级重采样），再用 numpy 线性重采样到 16k
    info = sd.query_devices(kind="input")
    sr = int(info.get("default_samplerate", 16000) or 16000)

    def _cb(indata, _frames, _time, _status):
        if not stop_event.is_set():
            frames.append(indata.copy())
            with _REC_LOCK:                     # 实时音量的粗略峰值（int16 归一）
                _REC_PEAK = min(1.0, float(np.abs(indata).max()) / 20000.0)

    try:
        with sd.InputStream(samplerate=sr, channels=1, dtype="int16", callback=_cb):
            stop_event.wait()                     # 阻塞直到用户点停
    finally:
        with _REC_LOCK:
            _REC_PEAK = 0.0
    if not frames:
        return ""
    audio = np.concatenate(frames).reshape(-1)          # int16
    if sr != 16000:                                      # 线性重采样到 whisper 友好采样率
        idx = np.linspace(0, audio.size - 1,
                          int(audio.size * 16000 // sr)).astype(int)
        audio = audio[idx]
    wav = tempfile.mktemp(prefix="pa_voice_", suffix=".wav")
    with wave.open(wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio.tobytes())
    return wav


# ---------------- 识别（本地 whisper） ----------------

def _get_whisper():
    """faster-whisper 模型进程内单例（锁保证只加载一次）。"""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    with _WHISPER_LOCK:
        if _WHISPER_MODEL is None:
            import faster_whisper
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            os.makedirs(WHISPER_DIR, exist_ok=True)
            # 模型 ID 写全名，避免只写 "small" 时的下载歧义
            _WHISPER_MODEL = faster_whisper.WhisperModel(
                "Systran/faster-whisper-small", device="cpu",
                compute_type="int8", download_root=WHISPER_DIR)
    return _WHISPER_MODEL


def transcribe(wav_path: str) -> str:
    """本地识别中文语音 → 文字（空串 = 没听清）。"""
    return transcribe_with_conf(wav_path)[0]


def transcribe_with_conf(wav_path: str) -> tuple:
    """返回 (文本, 置信度)。置信度过低且非空时，自动换更宽松的搜索参数重试一次，降低错字率。"""
    if not wav_path or not os.path.exists(wav_path):
        return "", -99.0
    model = _get_whisper()

    def _run(**kw):
        segments, _info = model.transcribe(wav_path, language="zh",
                                           vad_filter=True, **kw)
        parts = list(segments)
        text = "".join(s.text for s in parts).strip()
        conf = -99.0 if not parts else sum(getattr(s, "avg_logprob", 0.0)
                                           for s in parts) / len(parts)
        return text, float(conf)

    text, conf = _run(beam_size=5)
    if text and conf < -0.6:                        # 低置信 → 抬高温度再试一次
        text2, conf2 = _run(beam_size=5, temperature=[0.0, 0.2, 0.4])
        if conf2 > conf:
            text, conf = text2, conf2
    return text, conf


# ---------------- 朗读（edge-tts + Windows 自带播放器） ----------------

# 常用中文音色（edge-tts 名 → 显示名）；语速取值如 "+0%"/"-20%"/"+30%"
VOICES = {
    "晓晓（温柔女声）": "zh-CN-XiaoxiaoNeural",
    "云希（阳光男声）": "zh-CN-YunxiNeural",
    "晓伊（活力女声）": "zh-CN-XiaoyiNeural",
    "云扬（新闻男声）": "zh-CN-YunyangNeural",
}
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_RATE = "+0%"

def _system_proxy():
    """读 Windows 注册表里的系统代理（有则返回 'http://host:port'，无则 ''）。"""
    global _HAS_PROXY
    if _HAS_PROXY is not None:
        return _HAS_PROXY
    _HAS_PROXY = ""
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            enable = winreg.QueryValueEx(k, "ProxyEnable")[0]
            proxy = winreg.QueryValueEx(k, "ProxyServer")[0]
        if enable and proxy:
            _HAS_PROXY = proxy if "://" in proxy else "http://" + proxy
    except Exception:
        pass
    return _HAS_PROXY


async def _tts_save(text: str, mp3: str, voice: str = "", rate: str = "") -> None:
    import edge_tts
    proxy = _system_proxy()
    voice = voice or DEFAULT_VOICE
    rate = rate or DEFAULT_RATE
    try:
        com = edge_tts.Communicate(text, voice, rate=rate, proxy=proxy or None)
    except TypeError:                       # 老版本 edge-tts 没有 proxy 参数
        com = edge_tts.Communicate(text, voice, rate=rate)
    await com.save(mp3)


def _play_mci(mp3: str) -> None:
    """winmm 顺序播放 mp3（Windows 自带，零额外依赖）：wait 播完才返回，供"一句接一句"朗读。
    mp3 文件生命周期归本函数管理，播完即删。"""
    global _LAST_MP3
    winmm = ctypes.windll.winmm
    alias = "voice_out"
    with _MCI_LOCK:
        winmm.mciSendStringW("close " + alias, None, 0, 0)      # 清掉上一段（无并发）
        if _LAST_MP3 and os.path.exists(_LAST_MP3):             # 删上一段残留文件
            try:
                os.remove(_LAST_MP3)
            except Exception:
                pass
            _LAST_MP3 = ""
        if winmm.mciSendStringW(f'open "{mp3}" alias {alias}', None, 0, 0) != 0:
            try:
                os.remove(mp3)                                    # 没打开成功，直接清掉
            except Exception:
                pass
            return
        _LAST_MP3 = mp3
        winmm.mciSendStringW(f"play {alias} wait", None, 0, 0)  # 阻塞到播完（stop_speak 可打断）
        winmm.mciSendStringW("close " + alias, None, 0, 0)
        if _LAST_MP3 and os.path.exists(_LAST_MP3):
            try:
                os.remove(_LAST_MP3)
            except Exception:
                pass
            _LAST_MP3 = ""


def speak(text: str, voice: str = "", rate: str = "") -> str:
    """把文字朗读出来（voice/rate 为 edge-tts 音色/语速，空则用默认）：
    合成 + 顺序播放一次完成。成功返回 ""；失败返回给用户看的中文提示。"""
    r = synthesize(text, voice, rate)
    if r:
        return r
    return ""


def synthesize(text: str, voice: str = "", rate: str = "") -> str:
    """把一句话合成为 mp3（edge-tts，联网）；前方为语音常驻 mp3 路径。
    返回 "" 成功；失败返回中文提示。语音开流水线时用合成/播放分离的两个原语。"""
    import tempfile as _tf
    text = _strip_emoji(text)
    text = (text or "").strip()
    if not text:
        return ""
    mp3 = _tf.mktemp(prefix="pa_tts_", suffix=".mp3")
    try:
        asyncio.run(_tts_save(text, mp3, voice, rate))
        if not os.path.exists(mp3) or os.path.getsize(mp3) < 1024:
            raise OSError("empty audio")
        return mp3                                   # 成功：返回 mp3 路径（播放完由播放端删除）
    except Exception:
        try:
            os.remove(mp3)
        except Exception:
            pass
        return "🌐 朗读需要联网（或代理没通），这次没读出声音。"


def play_file(mp3: str) -> None:
    """顺序播放一段已合成的 mp3（会阻塞到播完），播完自动删除文件。"""
    if mp3:
        _play_mci(mp3)


# 朗读前剥掉 emoji 等装饰字符，避免 TTS 把😊读成"微笑"之类的尴尬
_EMOJI_FILTER = re.compile(
    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3\uFE0E]")

def _strip_emoji(text: str) -> str:
    return _EMOJI_FILTER.sub("", text or "")


def stop_speak() -> None:
    """立即停止当前朗读（录音开始时/用户关朗读时调用；即便播放正 wait 阻塞也能打断）。"""
    global _LAST_MP3
    try:
        # 故意不拿 _MCI_LOCK：close 会打断播放线程里阻塞的 wait，随后锁自然释放
        ctypes.windll.winmm.mciSendStringW("close voice_out", None, 0, 0)
        if _LAST_MP3 and os.path.exists(_LAST_MP3):
            try:
                os.remove(_LAST_MP3)
            except Exception:
                pass
            _LAST_MP3 = ""
    except Exception:
        pass