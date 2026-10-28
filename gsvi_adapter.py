"""
GSVI 适配器 —— 桥接 AstrBot GSVI TTS 提供商与 GPT-SoVITS 推理引擎。

用法:
    python gsvi_adapter.py -a 127.0.0.1 -p 8000
    管理界面: http://127.0.0.1:8000/ui

AstrBot 侧配置:
    API 地址: http://127.0.0.1:8000
    角色:     在管理界面里创建的角色名

=== v3 变更 ===
新增:
  - 角色配置外置到 gsvi_characters.json，网页上直接增删改，不用改代码
  - 内置目录浏览器，点选权重/参考音频，不用手打路径
  - 试听：每个角色一键合成固定文本并播放
  - 写接口鉴权：对局域网开放时必须设 API Key 才能改配置

v2 已具备:
  - 角色级权重绑定 + 运行时热切换（失败自动回滚）
  - dl_url 动态参考音频
  - 音频缓存 TTL 清理、可选 API Key、路径穿越防护

=== 使用说明 ===
1. 把 gsvi_adapter.py 放到 GPT-SoVITS 项目根目录（和 api_v2.py 同级）
2. 双击 启动GSVI适配器.exe
3. 浏览器打开 http://127.0.0.1:8000/ui 添加角色
"""

import os
import re
import sys
import gc
import json
import time
import uuid
import argparse
import signal
import threading
import traceback
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import urlopen, Request as UrlRequest

# ---------------------------------------------------------------------------
# 路径准备：以脚本所在目录为基准，不再依赖启动时的工作目录
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "GPT_SoVITS"))
sys.path.insert(0, str(BASE_DIR))

import soundfile as sf
import uvicorn
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

try:
    import torch
except Exception:  # pragma: no cover - torch 由 GPT-SoVITS runtime 提供
    torch = None


# ============================================================================
# 配置
# ============================================================================
CONFIG_FILE = BASE_DIR / "gsvi_characters.json"

# 首次运行（还没有 gsvi_characters.json）时的初始角色。
# 默认留空 —— 角色统一在网页 http://127.0.0.1:8000/ui 上创建，
# 这样把适配器发给别人时，不会带上你这台机器的绝对路径。
#
# 想预置角色就按下面格式填（路径建议用相对 GPT-SoVITS 根目录的相对路径，
# 这样换台机器、只要目录结构一样就能直接复用）：
#
# BUILTIN_CHARACTERS = {
#     "示例角色": {
#         "ref_audio_path": "refs/示例.wav",
#         "prompt_text": "参考音频里说的原话。",
#         "prompt_lang": "zh",
#         "gpt_weights": "GPT_weights_v2Pro/示例-e15.ckpt",
#         "sovits_weights": "SoVITS_weights_v2Pro/示例_e8_s344.pth",
#         "seed": -1,
#         "parallel_infer": True,
#     },
# }
BUILTIN_CHARACTERS = {}

DEFAULT_SETTINGS = {
    "allow_dirs": [],                                        # 目录浏览器额外允许的根目录
    "preview_text": "你好，我是{name}，很高兴见到你。",        # 试听文本，{name} 会被替换
    "preview_lang": "auto",                                  # 试听文本语言；auto = 自动检测
}

LANG_MAP = {
    "中文": "zh", "英文": "en", "日文": "ja",
    "韩文": "ko", "粤语": "yue", "自动": "auto",
    "zh": "zh", "en": "en", "ja": "ja",
    "ko": "ko", "yue": "yue", "auto": "auto",
}
VALID_LANGS = {"zh", "en", "ja", "ko", "yue", "auto"}

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wma"}

# 音频缓存策略
AUDIO_TTL = 600
AUDIO_MAX_FILES = 300
CLEANUP_INTERVAL = 60

# dl_url 下载限制
DL_TIMEOUT = 10
DL_MAX_BYTES = 20 * 1024 * 1024


# ============================================================================
# 命令行
# ============================================================================
parser = argparse.ArgumentParser(description="GSVI Adapter for GPT-SoVITS")
parser.add_argument("-c", "--tts_config", type=str, default="GPT_SoVITS/configs/tts_infer.yaml",
                    help="tts_infer.yaml 配置文件路径")
parser.add_argument("-a", "--bind_addr", type=str, default="127.0.0.1",
                    help="绑定 IP 地址，默认 127.0.0.1（仅本机）；局域网访问改为 0.0.0.0")
parser.add_argument("-p", "--port", type=int, default=8000,
                    help="绑定端口，默认 8000")
parser.add_argument("-k", "--api-key", type=str, default="",
                    help="可选。设置后写接口需要携带此 Key")
parser.add_argument("--audio-ttl", type=int, default=AUDIO_TTL,
                    help=f"音频缓存保留秒数，默认 {AUDIO_TTL}")
parser.add_argument("--audio-max-files", type=int, default=AUDIO_MAX_FILES,
                    help=f"音频缓存最多文件数，默认 {AUDIO_MAX_FILES}")
_args = parser.parse_args()

AUDIO_TTL = max(60, _args.audio_ttl)
AUDIO_MAX_FILES = max(1, _args.audio_max_files)
API_KEY = (_args.api_key or "").strip()


# ============================================================================
# 小工具
# ============================================================================
def _abs(p) -> str:
    """把配置里的路径统一成绝对路径（相对路径按 GPT-SoVITS 根目录解析）。"""
    if not p:
        return ""
    q = Path(str(p).replace("/", os.sep))
    if not q.is_absolute():
        q = BASE_DIR / q
    try:
        return str(q.resolve())
    except Exception:
        return str(q)


def _empty_cache():
    gc.collect()
    if torch is not None and torch.cuda.is_available():
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass


def _html_escape(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


# ---------------------------------------------------------------------------
# 文本语言检测
# 注意区分两个概念：
#   prompt_lang —— 参考音频里说的语言，给 GPT 理解音色用
#   text_lang   —— 要合成的这段文本的语言
# 用错会导致前端 G2P 处理不了，产出一段近乎静音的短音频。
# ---------------------------------------------------------------------------
_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")   # 平假名 + 片假名
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _detect_lang(text: str) -> str:
    """按字符集粗判文本语言。日文优先判假名，因为日文里也含汉字。"""
    if not text:
        return "zh"
    if _KANA_RE.search(text):
        return "ja"
    if _HANGUL_RE.search(text):
        return "ko"
    if _HAN_RE.search(text):
        return "zh"
    return "en"


def _audio_warning(sr, audio) -> str:
    """合成结果异常检测：过短且几乎无声，基本就是语言/文本不匹配。

    刻意不依赖 numpy —— 抽样求峰值就够了，省得再引入一个依赖。
    """
    try:
        total = len(audio)
        if total == 0:
            return "合成结果为空，检查参考音频和参考文本"
        duration = float(total) / float(sr or 1)
        step = max(1, total // 20000)
        peak = 0.0
        for i in range(0, total, step):
            val = abs(float(audio[i]))
            if val > peak:
                peak = val
    except Exception:
        return ""
    if duration < 0.35 and peak < 0.02:
        return (f"合成结果几乎无声（{duration:.2f} 秒，峰值 {peak:.3f}）。"
                f"常见原因是「文本语言」和文本内容不匹配，"
                f"比如用日文模式去合成中文文本。")
    return ""


def _normalize_char(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    out = {
        "ref_audio_path": str(raw.get("ref_audio_path") or ""),
        "prompt_text": str(raw.get("prompt_text") or ""),
        "prompt_lang": str(raw.get("prompt_lang") or "zh"),
        "gpt_weights": str(raw.get("gpt_weights") or ""),
        "sovits_weights": str(raw.get("sovits_weights") or ""),
    }
    try:
        out["seed"] = int(raw.get("seed", -1))
    except (TypeError, ValueError):
        out["seed"] = -1
    out["parallel_infer"] = bool(raw.get("parallel_infer", True))
    return out


# ============================================================================
# 配置读写
# ============================================================================
_config_lock = threading.RLock()
CHARACTERS: dict = {}
SETTINGS: dict = dict(DEFAULT_SETTINGS)


def load_config():
    """读 gsvi_characters.json；不存在则从 BUILTIN_CHARACTERS 迁移一份。"""
    global CHARACTERS, SETTINGS

    if CONFIG_FILE.is_file():
        try:
            raw = CONFIG_FILE.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except Exception as exc:
            print(f"[warn] 配置文件读取失败（{exc}），本次使用空配置。")
            print(f"       文件位置：{CONFIG_FILE}")
            data = {}

        chars = {}
        for name, item in (data.get("characters") or {}).items():
            norm = _normalize_char(item)
            if norm is not None:
                chars[str(name)] = norm

        settings = dict(DEFAULT_SETTINGS)
        raw_settings = data.get("settings") or {}
        if isinstance(raw_settings, dict):
            allow = raw_settings.get("allow_dirs")
            if isinstance(allow, list):
                settings["allow_dirs"] = [str(x) for x in allow if str(x).strip()]
            text = raw_settings.get("preview_text")
            if isinstance(text, str) and text.strip():
                settings["preview_text"] = text
            plang = raw_settings.get("preview_lang")
            if isinstance(plang, str) and plang.strip():
                settings["preview_lang"] = plang.strip()

        CHARACTERS = chars
        SETTINGS = settings
        print(f"[config] 已加载 {CONFIG_FILE.name}，共 {len(chars)} 个角色")
        return

    # 首次运行：迁移
    CHARACTERS = {}
    for name, item in BUILTIN_CHARACTERS.items():
        norm = _normalize_char(item)
        if norm is not None:
            CHARACTERS[str(name)] = norm
    SETTINGS = dict(DEFAULT_SETTINGS)
    try:
        save_config()
        print(f"[config] 首次运行，已生成配置文件：{CONFIG_FILE}")
        print("         之后请到网页 http://127.0.0.1:%d/ui 增删改角色" % _args.port)
    except Exception:
        traceback.print_exc()


def save_config():
    """原子写入，避免断电/并发把配置写坏。"""
    with _config_lock:
        payload = {"characters": CHARACTERS, "settings": SETTINGS}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)


def _commit(new_chars: dict, new_settings: Optional[dict] = None):
    """先落盘再提交到内存，落盘失败时内存不受影响。"""
    global CHARACTERS, SETTINGS
    with _config_lock:
        old_chars, old_settings = CHARACTERS, SETTINGS
        CHARACTERS = new_chars
        if new_settings is not None:
            SETTINGS = new_settings
        try:
            save_config()
        except Exception:
            CHARACTERS, SETTINGS = old_chars, old_settings
            raise


# ============================================================================
# 版本探测（只用于报错诊断，不参与正常流程）
# ============================================================================
def _peek_version(path: str) -> str:
    """尽力从权重文件里读出训练时的模型版本；读不到就返回空串。"""
    if not path or torch is None:
        return ""
    try:
        data = torch.load(path, map_location="cpu", weights_only=False)
        cfg = (data or {}).get("config") or {}
        model = cfg.get("model") or {}
        return str(model.get("version") or cfg.get("version") or "")
    except Exception:
        return ""


def _guess_version_from_path(path: str) -> str:
    """从 GPT_weights_v2Pro / SoVITS_weights_v3 这类目录名里猜版本。"""
    try:
        name = Path(path).parent.name
    except Exception:
        return ""
    low = name.lower()
    if "weights_" in low:
        return name.split("weights_", 1)[-1]
    return ""


# ============================================================================
# 模型管理：角色级权重热切换
# ============================================================================
class ModelSwitchError(RuntimeError):
    pass


class ModelManager:
    """持有唯一的 TTS 引擎实例，负责按需切换 GPT / SoVITS 权重。

    同一时刻只保留一对权重。切换与推理共用一把可重入锁，
    保证不会在推理中途把模型换掉。
    """

    def __init__(self, engine: TTS):
        self.engine = engine
        self.lock = threading.RLock()
        self.current = self._snapshot()
        self.last_good = self.current
        self.version = str(getattr(engine.configs, "version", "") or "")
        self._bert = getattr(engine.configs, "bert_base_path", None)
        self._cnhubert = getattr(engine.configs, "cnhuhbert_base_path", None)

    def _snapshot(self):
        cfg = self.engine.configs
        return (_abs(getattr(cfg, "t2s_weights_path", "")),
                _abs(getattr(cfg, "vits_weights_path", "")))

    def _restore_bert(self):
        """init_vits_weights 会用 pth 内嵌的路径覆盖 BERT 配置，这里回填成 yaml 里的值。"""
        try:
            if self._bert:
                self.engine.configs.bert_base_path = self._bert
            if self._cnhubert:
                self.engine.configs.cnhuhbert_base_path = self._cnhubert
        except Exception:
            pass

    def _apply(self, gpt: str, sovits: str):
        if gpt:
            self.engine.init_t2s_weights(gpt)
        if sovits:
            self.engine.init_vits_weights(sovits)
        self._restore_bert()

    def _diagnose(self, gpt: str, sovits: str) -> str:
        """切换失败时尝试给出具体原因（版本不匹配、pth 内嵌路径失效最常见）。"""
        if torch is None:
            return ""
        hints = []

        gpt_ver = _peek_version(gpt)
        sov_ver = _peek_version(sovits)
        if gpt_ver and sov_ver and gpt_ver != sov_ver:
            hints.append(f"GPT 权重是 {gpt_ver}，SoVITS 权重是 {sov_ver}，两者不是同一代，"
                         f"必须来自同一次训练")
        loaded = str(getattr(self.engine.configs, "version", "") or "")
        for label, ver in (("GPT", gpt_ver), ("SoVITS", sov_ver)):
            if ver and loaded and ver != loaded:
                hints.append(f"{label} 权重是 {ver}，但 tts_infer.yaml 里 version 填的是 "
                             f"{loaded}，请把 version 改成 {ver}")

        if sovits:
            try:
                data = torch.load(sovits, map_location="cpu", weights_only=False)
                cfg = (data or {}).get("config", {}) or {}
                bad = []
                for key in ("bert_base_path", "cnhuhbert_base_path"):
                    val = cfg.get(key)
                    if val and not Path(str(val)).exists():
                        bad.append(f"{key}={val}")
                if bad:
                    hints.append("该 pth 内嵌的预训练路径在本机不存在：" + "；".join(bad)
                                 + "，请把这些模型放到对应路径，或改用本机训练的权重")
            except Exception:
                pass

        return ("。" + "；".join(hints)) if hints else ""

    def ensure(self, gpt: str, sovits: str) -> bool:
        """确保当前加载的是指定权重。返回是否发生了切换。"""
        target = (gpt or "", sovits or "")
        if target == ("", ""):
            return False  # 该角色没绑权重，沿用当前模型
        if target == self.current:
            return False
        if not SWITCH_SUPPORTED:
            raise ModelSwitchError(
                "当前 GPT-SoVITS 版本不支持运行时切换权重"
                "（TTS 类缺少 init_t2s_weights / init_vits_weights）"
            )
        with self.lock:
            if target == self.current:
                return False
            self._load(target)
            return True

    def _load(self, target):
        gpt, sovits = target
        if bool(gpt) != bool(sovits):
            raise ModelSwitchError("gpt_weights 与 sovits_weights 必须成对配置")
        for path, label in ((gpt, "GPT 权重(.ckpt)"), (sovits, "SoVITS 权重(.pth)")):
            if path and not Path(path).exists():
                raise ModelSwitchError(f"{label} 不存在: {path}")

        t0 = time.time()
        print(f"[model] 切换 -> {Path(gpt).name if gpt else '(保持)'} + "
              f"{Path(sovits).name if sovits else '(保持)'}")
        try:
            self._apply(gpt, sovits)
        except Exception as exc:
            traceback.print_exc()
            hint = self._diagnose(gpt, sovits)
            self._rollback()
            raise ModelSwitchError(f"{exc}{hint}") from exc

        _empty_cache()
        self.current = self._snapshot()
        self.last_good = self.current
        self.version = str(getattr(self.engine.configs, "version", "") or "")
        print(f"[model] 切换完成，耗时 {time.time() - t0:.2f}s")

    def _rollback(self):
        """切换失败后回退到上一个可用模型，保证服务不被打死。"""
        gpt, sovits = self.last_good
        if not gpt and not sovits:
            return
        print("[model] 回滚到上一个可用模型…")
        try:
            self._apply(gpt, sovits)
            self.current = self._snapshot()
            print("[model] 回滚成功")
        except Exception:
            traceback.print_exc()
            print("[model] 回滚也失败了，请重启适配器")
        finally:
            _empty_cache()


def _resolve_model(char: dict):
    return _abs(char.get("gpt_weights", "")), _abs(char.get("sovits_weights", ""))


# ============================================================================
# 引擎启动
# ============================================================================
_tts_cfg = TTS_Config(_args.tts_config)
print(_tts_cfg)
_tts = TTS(_tts_cfg)

SWITCH_SUPPORTED = hasattr(_tts, "init_t2s_weights") and hasattr(_tts, "init_vits_weights")

MODEL = ModelManager(_tts)

APP = FastAPI(title="GSVI Adapter for GPT-SoVITS")
AUDIO_DIR = BASE_DIR / "gsvi_audio"
AUDIO_DIR.mkdir(exist_ok=True)

load_config()


# ============================================================================
# 启动自检 / 缓存清理
# ============================================================================
def validate_characters():
    if not SWITCH_SUPPORTED:
        print("[warn] 当前 GPT-SoVITS 版本不支持运行时切换权重，"
              "所有角色都会沿用 tts_infer.yaml 里的模型")
    if not CHARACTERS:
        print("[warn] 还没有配置任何角色，请到 /ui 添加")
        return
    print("-" * 64)
    print("角色自检：")
    for name, char in CHARACTERS.items():
        ref = _abs(char.get("ref_audio_path", ""))
        gpt, sovits = _resolve_model(char)
        problems = []
        if not ref:
            problems.append("缺参考音频")
        elif not Path(ref).exists():
            problems.append("参考音频不存在")
        if gpt and not Path(gpt).exists():
            problems.append("GPT 权重不存在")
        if sovits and not Path(sovits).exists():
            problems.append("SoVITS 权重不存在")
        if (gpt and not sovits) or (sovits and not gpt):
            problems.append("GPT/SoVITS 只配了一个")

        # 版本一致性：先从目录名猜，猜不到就跳过（不误报）
        g_ver = _guess_version_from_path(gpt) if gpt else ""
        s_ver = _guess_version_from_path(sovits) if sovits else ""
        if g_ver and s_ver and g_ver != s_ver:
            problems.append(f"权重版本不一致（{g_ver} / {s_ver}）")
        elif MODEL.version:
            for label, ver in (("GPT", g_ver), ("SoVITS", s_ver)):
                if ver and ver != MODEL.version:
                    problems.append(f"{label} 权重是 {ver}，yaml 里是 {MODEL.version}")
        flag = "OK " if not problems else "!! "
        detail = " + ".join(problems) if problems else "就绪"
        print(f"  {flag}{name}  |  {detail}")
    print("-" * 64)


def cleanup_audio_dir():
    now = time.time()
    keep = []
    for p in AUDIO_DIR.glob("*.wav"):
        try:
            if now - p.stat().st_mtime > AUDIO_TTL:
                p.unlink()
            else:
                keep.append(p)
        except FileNotFoundError:
            pass
        except Exception:
            pass
    if len(keep) > AUDIO_MAX_FILES:
        keep.sort(key=lambda x: x.stat().st_mtime)
        for p in keep[: len(keep) - AUDIO_MAX_FILES]:
            try:
                p.unlink()
            except Exception:
                pass


def _cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            cleanup_audio_dir()
        except Exception:
            traceback.print_exc()


# ============================================================================
# 鉴权
# ============================================================================
def check_auth(request: Request):
    """API_KEY 为空时不鉴权；否则接受三种携带方式。"""
    if not API_KEY:
        return
    token = ""
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif auth:
        token = auth
    if not token:
        token = (request.headers.get("x-api-key") or "").strip()
    if not token:
        token = (request.query_params.get("api_key") or "").strip()
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="API Key 无效")


def _is_local_client(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def guard_admin(request: Request):
    """写配置和浏览磁盘的守卫：有 Key 就验 Key，没 Key 只允许本机操作。"""
    if API_KEY:
        check_auth(request)
        return
    if _is_local_client(request):
        return
    raise HTTPException(
        status_code=403,
        detail="服务已对局域网开放，请用 -k 设置 API Key 后再修改配置",
    )


# ============================================================================
# 目录浏览（白名单）
# ============================================================================
def _allowed_roots() -> list:
    roots = [BASE_DIR]
    for d in SETTINGS.get("allow_dirs", []):
        p = Path(str(d).replace("/", os.sep))
        if p.exists():
            roots.append(p)
    # 已配置文件的所在目录自动放行，避免「要选文件却进不去那个目录」的死循环
    for char in CHARACTERS.values():
        for key in ("ref_audio_path", "gpt_weights", "sovits_weights"):
            v = _abs(char.get(key, ""))
            if v:
                roots.append(Path(v).parent)
    out = []
    for r in roots:
        try:
            out.append(r.resolve())
        except Exception:
            pass
    return out


def _is_allowed(path: Path) -> bool:
    try:
        target = path.resolve()
    except Exception:
        return False
    for root in _allowed_roots():
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


BROWSE_FILTERS = {
    "ckpt": {".ckpt"},
    "pth": {".pth"},
    "audio": AUDIO_EXTS,
    "model": {".ckpt", ".pth"},
    "all": None,
}


# ============================================================================
# dl_url：下载动态参考音频
# ============================================================================
def _guess_suffix(path: str, content_type: str) -> str:
    suffix = Path(urlparse(path).path).suffix.lower()
    if suffix in AUDIO_EXTS:
        return suffix
    ctype = (content_type or "").lower()
    for key, ext in (("mpeg", ".mp3"), ("mp3", ".mp3"), ("flac", ".flac"),
                     ("ogg", ".ogg"), ("mp4", ".m4a"), ("wav", ".wav")):
        if key in ctype:
            return ext
    return ".wav"


def _looks_like_audio(path: Path) -> bool:
    """按文件头判断是不是音频，防止把网页/JSON 当参考音频喂给引擎。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except Exception:
        return False
    if len(head) < 4:
        return False
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    if head[:4] in (b"fLaC", b"OggS"):
        return True
    if head[:3] == b"ID3":                      # mp3 带 ID3 头
        return True
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:   # mp3 帧同步
        return True
    if head[4:8] == b"ftyp":                    # m4a/mp4
        return True
    if head[:4] == b"\x1aE\xdf\xa3":            # webm
        return True
    return False


def _is_self_url(url: str, request: Request) -> bool:
    """AstrBot 的 GSVI 提供商会把 api_base 塞进 dl_url，这种要直接忽略。

    见 astrbot/core/provider/sources/gsvi_tts_source.py:
        data = {"dl_url": self.api_base, ...}
    """
    try:
        target = urlparse(url)
        host = (request.headers.get("host") or "").strip().lower()
        if not host:
            return False
        return target.netloc.lower() == host and target.path in ("", "/")
    except Exception:
        return False


def fetch_dl_audio(url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("dl_url 只支持 http / https")
    req = UrlRequest(url, headers={"User-Agent": "gsvi-adapter/3.0"})
    with urlopen(req, timeout=DL_TIMEOUT) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        data = resp.read(DL_MAX_BYTES + 1)
        suffix = _guess_suffix(url, ctype)
    if len(data) > DL_MAX_BYTES:
        raise ValueError(f"dl_url 音频超过 {DL_MAX_BYTES // 1024 // 1024}MB 上限")
    if not data:
        raise ValueError("dl_url 返回了空内容")
    if "json" in ctype or "html" in ctype or ctype.startswith("text/"):
        raise ValueError(f"dl_url 返回的不是音频（Content-Type: {ctype}）")
    tmp = AUDIO_DIR / f"ref_{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(data)
    return tmp


# ============================================================================
# 请求模型
# ============================================================================
class GSVIRequest(BaseModel):
    # 全部用 Optional：AstrBot 那边配置没填时会传 null，
    # 用裸 str 会直接 422，报错信息还很难懂
    dl_url: Optional[str] = ""
    version: Optional[str] = "v2Pro"
    model_name: Optional[str] = None
    prompt_text_lang: Optional[str] = "中文"
    emotion: Optional[str] = "默认"
    text: Optional[str] = ""
    text_lang: Optional[str] = "中文"
    prompt_text: Optional[str] = ""


class CharacterPayload(BaseModel):
    name: str = ""
    ref_audio_path: str = ""
    prompt_text: str = ""
    prompt_lang: str = "zh"
    gpt_weights: str = ""
    sovits_weights: str = ""
    seed: int = -1
    parallel_infer: bool = True


class SettingsPayload(BaseModel):
    allow_dirs: list = []
    preview_text: str = ""
    preview_lang: str = "auto"


# ============================================================================
# 合成核心
# ============================================================================
def _synth(char: dict, text: str, text_lang: str, prompt_lang: str,
           ref_audio: str, prompt_text: str):
    gpt, sovits = _resolve_model(char)
    with MODEL.lock:
        MODEL.ensure(gpt, sovits)
        tts_req = {
            "text": text,
            "text_lang": text_lang,
            "ref_audio_path": ref_audio,
            "prompt_text": prompt_text,
            "prompt_lang": prompt_lang,
            "streaming_mode": False,
            "media_type": "wav",
            "text_split_method": "cut5",
            "batch_size": 1,
            "seed": char.get("seed", -1),
            "parallel_infer": char.get("parallel_infer", True),
        }
        gen = _tts.run(tts_req)
        try:
            sr, audio_data = next(gen)
        finally:
            gen.close()
    return sr, audio_data


def _write_wav(sr, audio_data) -> str:
    filename = f"{uuid.uuid4().hex}.wav"
    sf.write(str(AUDIO_DIR / filename), audio_data, sr)
    return filename


def build_audio_url(request: Request, filename: str) -> str:
    """优先用请求的 Host 头回填，避免绑 0.0.0.0 时返回不可达地址。"""
    host = (request.headers.get("host") or "").strip()
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0"):]
    if host:
        return f"http://{host}/audio/{filename}"
    fallback = _args.bind_addr
    if fallback in ("0.0.0.0", "::", "None", ""):
        fallback = "127.0.0.1"
    return f"http://{fallback}:{_args.port}/audio/{filename}"


# ============================================================================
# 推理接口
# ============================================================================
@APP.post("/infer_single")
def infer_single(req: GSVIRequest, request: Request):
    """GSVI 格式的 TTS 推理，供 AstrBot 调用。同步 def，不阻塞事件循环。"""
    try:
        check_auth(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})

    req_name = (req.model_name or "").strip()
    char = CHARACTERS.get(req_name) if req_name else None
    if char is None:
        names = "、".join(CHARACTERS) or "（未配置）"
        hint = "未指定角色" if not req_name else f"未找到角色 '{req_name}'"
        return JSONResponse(status_code=400, content={
            "msg": f"{hint}；可用角色：{names}"
        })

    text = (req.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"msg": "text 不能为空"})

    text_lang = LANG_MAP.get(req.text_lang, "zh")
    # 角色表里的 prompt_lang 优先；没配才跟随 AstrBot 的「文本语言」
    prompt_lang = char.get("prompt_lang") or LANG_MAP.get(req.prompt_text_lang, "zh")

    ref_audio = _abs(char.get("ref_audio_path", ""))
    prompt_text = char.get("prompt_text", "")
    tmp_ref = None
    dl_note = ""

    try:
        if req.dl_url:
            if _is_self_url(req.dl_url, request):
                # AstrBot 会把 api_base 填进 dl_url，这不是音频地址，直接忽略
                print(f"[dl_url] 忽略指向本服务的地址：{req.dl_url}")
            else:
                try:
                    tmp_ref = fetch_dl_audio(req.dl_url)
                    if not _looks_like_audio(tmp_ref):
                        raise ValueError("下载到的内容不是有效音频")
                    ref_audio = str(tmp_ref)
                    prompt_text = req.prompt_text or ""
                    print(f"[dl_url] 已使用动态参考音频：{req.dl_url}")
                except Exception as exc:
                    # 动态参考音频失败不能让整个请求挂掉，退回角色自带的参考音频
                    print(f"[dl_url] 失败（{exc}），回退到角色自带参考音频")
                    dl_note = f"dl_url 已忽略：{exc}"
                    if tmp_ref is not None:
                        try:
                            tmp_ref.unlink()
                        except Exception:
                            pass
                        tmp_ref = None

        if not ref_audio:
            return JSONResponse(status_code=400, content={
                "msg": f"角色 '{req.model_name}' 未配置参考音频"
            })

        sr, audio_data = _synth(char, text, text_lang, prompt_lang, ref_audio, prompt_text)
        filename = _write_wav(sr, audio_data)
        # msg 必须严格是「合成成功」，AstrBot 的 GSVI 提供商按这个字符串判断成败
        payload = {"msg": "合成成功", "audio_url": build_audio_url(request, filename)}
        notes = [n for n in (dl_note, _audio_warning(sr, audio_data)) if n]
        if notes:
            payload["warning"] = "；".join(notes)
        return payload

    except ModelSwitchError as e:
        return JSONResponse(status_code=500, content={"msg": f"模型切换失败: {e}"})
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"msg": f"合成失败: {e}"})
    finally:
        if tmp_ref is not None:
            try:
                tmp_ref.unlink()
            except Exception:
                pass


@APP.get("/audio/{filename}")
def serve_audio(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse(status_code=404, content={"msg": "音频文件不存在"})
    filepath = AUDIO_DIR / filename
    if not filepath.is_file():
        return JSONResponse(status_code=404, content={"msg": "音频文件不存在"})
    return FileResponse(str(filepath), media_type="audio/wav")


# ============================================================================
# 角色读取
# ============================================================================
def _character_view():
    cur_gpt, cur_sovits = MODEL.current
    items = []
    for name, char in CHARACTERS.items():
        gpt, sovits = _resolve_model(char)
        ref = _abs(char.get("ref_audio_path", ""))
        problems = []
        if not ref or not Path(ref).exists():
            problems.append("参考音频缺失")
        if gpt and not Path(gpt).exists():
            problems.append("GPT 权重缺失")
        if sovits and not Path(sovits).exists():
            problems.append("SoVITS 权重缺失")
        if (gpt and not sovits) or (sovits and not gpt):
            problems.append("权重只配了一个")
        items.append({
            "name": name,
            "gpt_weights": gpt,
            "sovits_weights": sovits,
            "bound": bool(gpt and sovits),
            "loaded": bool(gpt and sovits) and (gpt, sovits) == (cur_gpt, cur_sovits),
            "ref_audio": ref,
            "prompt_text": char.get("prompt_text", ""),
            "prompt_lang": char.get("prompt_lang") or "跟随请求",
            "seed": char.get("seed", -1),
            "parallel_infer": char.get("parallel_infer", True),
            "problems": problems,
        })
    return {
        "current": {"gpt_weights": cur_gpt, "sovits_weights": cur_sovits},
        "characters": items,
        "settings": SETTINGS,
        "base_dir": str(BASE_DIR),
        "loaded_version": MODEL.version,
        "auth_required": bool(API_KEY),
    }


@APP.get("/api/characters")
@APP.get("/characters")
def list_characters():
    return _character_view()


@APP.get("/api/settings")
def get_settings():
    return {"settings": SETTINGS, "base_dir": str(BASE_DIR)}


# ============================================================================
# 角色写操作
# ============================================================================
def _validate_char(payload: CharacterPayload, original: Optional[str]):
    """返回 (char_dict, errors, warnings)。errors 非空则不保存。"""
    errors = {}
    warnings = {}

    name = (payload.name or "").strip()
    if not name:
        errors["name"] = "角色名不能为空"
    elif len(name) > 40:
        errors["name"] = "角色名不能超过 40 个字符"
    elif any(ch in name for ch in '\\/:*?"<>|'):
        errors["name"] = '角色名不能包含 \\ / : * ? " < > |'
    elif name != original and name in CHARACTERS:
        errors["name"] = f"角色「{name}」已存在"

    ref = _abs(payload.ref_audio_path)
    if not ref:
        errors["ref_audio_path"] = "必须指定参考音频"
    elif not Path(ref).is_file():
        errors["ref_audio_path"] = "文件不存在"
    elif Path(ref).suffix.lower() not in AUDIO_EXTS:
        errors["ref_audio_path"] = "不是支持的音频格式"

    gpt = _abs(payload.gpt_weights)
    sovits = _abs(payload.sovits_weights)
    if bool(gpt) != bool(sovits):
        errors["gpt_weights"] = "GPT 与 SoVITS 权重必须成对填写"
    if gpt:
        if not Path(gpt).is_file():
            errors["gpt_weights"] = "文件不存在"
        elif Path(gpt).suffix.lower() != ".ckpt":
            errors["gpt_weights"] = "应为 .ckpt 文件"
    if sovits:
        if not Path(sovits).is_file():
            errors["sovits_weights"] = "文件不存在"
        elif Path(sovits).suffix.lower() != ".pth":
            errors["sovits_weights"] = "应为 .pth 文件"

    lang = (payload.prompt_lang or "zh").strip()
    if lang not in VALID_LANGS:
        errors["prompt_lang"] = "不支持的语言"

    prompt_text = (payload.prompt_text or "").strip()
    if not prompt_text:
        warnings["prompt_text"] = "参考文本为空，音色相似度会下降"

    if errors:
        return None, errors, warnings

    char = {
        "ref_audio_path": ref,
        "prompt_text": prompt_text,
        "prompt_lang": lang,
        "gpt_weights": gpt,
        "sovits_weights": sovits,
        "seed": int(payload.seed),
        "parallel_infer": bool(payload.parallel_infer),
    }
    return {"name": name, "char": char}, errors, warnings


@APP.post("/api/characters")
async def create_character(payload: CharacterPayload, request: Request):
    try:
        guard_admin(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})

    result, errors, warnings = _validate_char(payload, None)
    if errors:
        return JSONResponse(status_code=400, content={"msg": "配置有误", "errors": errors})

    name, char = result["name"], result["char"]
    new_chars = dict(CHARACTERS)
    new_chars[name] = char
    try:
        await run_in_threadpool(_commit, new_chars, None)
    except Exception as e:
        return JSONResponse(status_code=500, content={"msg": f"保存失败: {e}"})

    return {"msg": f"已添加角色「{name}」", "warnings": warnings}


@APP.put("/api/characters/{name}")
async def update_character(name: str, payload: CharacterPayload, request: Request):
    try:
        guard_admin(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})

    if name not in CHARACTERS:
        return JSONResponse(status_code=404, content={"msg": f"角色「{name}」不存在"})

    result, errors, warnings = _validate_char(payload, name)
    if errors:
        return JSONResponse(status_code=400, content={"msg": "配置有误", "errors": errors})

    new_name, char = result["name"], result["char"]
    new_chars = {}
    for k, v in CHARACTERS.items():
        new_chars[new_name if k == name else k] = char if k == name else v

    try:
        await run_in_threadpool(_commit, new_chars, None)
    except Exception as e:
        return JSONResponse(status_code=500, content={"msg": f"保存失败: {e}"})

    msg = f"已更新角色「{new_name}」"
    if new_name != name:
        msg += f"（原名「{name}」，记得去 AstrBot 同步修改角色名）"
    return {"msg": msg, "warnings": warnings}


@APP.delete("/api/characters/{name}")
async def delete_character(name: str, request: Request):
    try:
        guard_admin(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})

    if name not in CHARACTERS:
        return JSONResponse(status_code=404, content={"msg": f"角色「{name}」不存在"})

    new_chars = {k: v for k, v in CHARACTERS.items() if k != name}
    try:
        await run_in_threadpool(_commit, new_chars, None)
    except Exception as e:
        return JSONResponse(status_code=500, content={"msg": f"保存失败: {e}"})
    return {"msg": f"已删除角色「{name}」"}


@APP.put("/api/settings")
async def update_settings(payload: SettingsPayload, request: Request):
    try:
        guard_admin(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})

    allow = []
    for item in payload.allow_dirs:
        text = str(item).strip()
        if not text:
            continue
        p = Path(text.replace("/", os.sep))
        if not p.is_dir():
            return JSONResponse(status_code=400, content={
                "msg": f"目录不存在：{text}", "errors": {"allow_dirs": "目录不存在"}
            })
        allow.append(str(p.resolve()))

    plang = (payload.preview_lang or "auto").strip()
    if plang not in VALID_LANGS:
        return JSONResponse(status_code=400, content={
            "msg": "试听语言不支持", "errors": {"preview_lang": "不支持的语言"}
        })

    new_settings = {
        "allow_dirs": allow,
        "preview_text": (payload.preview_text or "").strip() or DEFAULT_SETTINGS["preview_text"],
        "preview_lang": plang,
    }
    try:
        await run_in_threadpool(_commit, dict(CHARACTERS), new_settings)
    except Exception as e:
        return JSONResponse(status_code=500, content={"msg": f"保存失败: {e}"})
    return {"msg": "设置已保存"}


# ============================================================================
# 目录浏览
# ============================================================================
@APP.get("/api/browse")
def api_browse(request: Request, path: str = Query(default=""), kind: str = Query(default="all")):
    try:
        guard_admin(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})

    target = Path(path.replace("/", os.sep)) if path else BASE_DIR
    try:
        target = target.resolve()
    except Exception:
        return JSONResponse(status_code=400, content={"msg": "路径无效"})

    if not target.is_dir():
        return JSONResponse(status_code=400, content={"msg": f"不是目录：{target}"})
    if not _is_allowed(target):
        roots = [str(r) for r in _allowed_roots()]
        return JSONResponse(status_code=403, content={
            "msg": "此目录不在允许范围内，可在「设置」里把它加入白名单",
            "allowed_roots": roots,
        })

    exts = BROWSE_FILTERS.get(kind, None)
    dirs, files = [], []
    try:
        entries = list(target.iterdir())
    except PermissionError:
        return JSONResponse(status_code=403, content={"msg": "没有权限读取此目录"})

    for entry in entries:
        try:
            if entry.is_dir():
                if entry.name.startswith("$") or entry.name == "__pycache__":
                    continue
                dirs.append({"name": entry.name, "path": str(entry)})
            elif entry.is_file():
                if exts is not None and entry.suffix.lower() not in exts:
                    continue
                files.append({"name": entry.name, "path": str(entry),
                              "size": entry.stat().st_size})
        except Exception:
            continue

    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())

    parent = target.parent
    parent_path = str(parent) if (parent != target and _is_allowed(parent)) else ""

    return {
        "path": str(target),
        "parent": parent_path,
        "dirs": dirs,
        "files": files,
        "allowed_roots": [str(r) for r in _allowed_roots()],
    }


# ============================================================================
# 试听
# ============================================================================
@APP.post("/api/preview")
def api_preview(request: Request, name: str = Query(default="")):
    try:
        check_auth(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})

    char = CHARACTERS.get(name)
    if char is None:
        return JSONResponse(status_code=404, content={"msg": f"角色「{name}」不存在"})

    ref = _abs(char.get("ref_audio_path", ""))
    if not ref or not Path(ref).is_file():
        return JSONResponse(status_code=400, content={"msg": "参考音频不存在，请先在编辑里修正"})

    template = SETTINGS.get("preview_text") or DEFAULT_SETTINGS["preview_text"]
    text = template.replace("{name}", name)

    # prompt_lang 是参考音频的语言（给 GPT 理解音色用），
    # text_lang 是这段试听文本本身的语言 —— 两者必须分开算
    prompt_lang = char.get("prompt_lang") or "zh"
    if prompt_lang not in VALID_LANGS:
        prompt_lang = "zh"
    forced = str(SETTINGS.get("preview_lang") or "auto").strip()
    if forced in VALID_LANGS and forced != "auto":
        text_lang = forced
    else:
        text_lang = _detect_lang(text)

    try:
        sr, audio_data = _synth(char, text, text_lang, prompt_lang, ref, char.get("prompt_text", ""))
        filename = _write_wav(sr, audio_data)
        result = {
            "msg": "试听已生成",
            "audio_url": build_audio_url(request, filename),
            "text": text,
            "text_lang": text_lang,
        }
        warn = _audio_warning(sr, audio_data)
        if warn:
            result["msg"] = "试听已生成，但结果异常"
            result["warning"] = warn
        return result
    except ModelSwitchError as e:
        return JSONResponse(status_code=500, content={"msg": f"模型切换失败: {e}"})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"msg": f"试听失败: {e}"})


# ============================================================================
# 切换角色
# ============================================================================
def _do_switch(name: str) -> bool:
    char = CHARACTERS.get(name)
    if char is None:
        raise KeyError(name)
    gpt, sovits = _resolve_model(char)
    with MODEL.lock:
        return MODEL.ensure(gpt, sovits)


@APP.post("/switch")
async def switch(request: Request, name: str = Query(default="")):
    try:
        check_auth(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"msg": e.detail})

    if not name:
        try:
            payload = await request.json()
            name = (payload or {}).get("name", "") if isinstance(payload, dict) else ""
        except Exception:
            name = ""
    name = (name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"msg": "缺少 name 参数"})

    try:
        switched = await run_in_threadpool(_do_switch, name)
    except KeyError:
        names = "、".join(CHARACTERS) or "（未配置）"
        return JSONResponse(status_code=404, content={
            "msg": f"未找到角色 '{name}'；可用角色：{names}"
        })
    except ModelSwitchError as e:
        return JSONResponse(status_code=500, content={"msg": f"切换失败: {e}"})

    return {
        "msg": f"已切换到「{name}」" + ("" if switched else "（权重未变化）"),
        "switched": switched,
    }


# ============================================================================
# 管理界面（纯静态外壳，数据全部走 /api/characters）
# ============================================================================
UI_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GSVI 适配器 · 角色管理</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;padding:24px;font-family:system-ui,"Microsoft YaHei",sans-serif;background:#f7f7f5;color:#2c2c2a}
  h1{font-size:18px;font-weight:500;margin:0}
  .sub{font-size:13px;color:#5f5e5a;margin:6px 0 18px;word-break:break-all}
  .bar{display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
  button{padding:6px 14px;border:1px solid #185fa5;background:#e6f1fb;color:#0c447c;border-radius:8px;font-size:13px;cursor:pointer;font-family:inherit}
  button:hover{background:#d8e9f8}
  button:disabled{opacity:.45;cursor:default}
  button.primary{background:#185fa5;color:#fff;border-color:#185fa5}
  button.primary:hover{background:#0c447c}
  button.danger{background:#fcebeb;color:#a32d2d;border-color:#e0a3a3}
  button.danger:hover{background:#f8dcdc}
  button.ghost{background:#fff;color:#444441;border-color:#d3d1c7}
  input,select,textarea{padding:6px 10px;border:1px solid #d3d1c7;border-radius:8px;font-size:13px;font-family:inherit;background:#fff;color:#2c2c2a}
  input:focus,select:focus,textarea:focus{outline:none;border-color:#185fa5}
  #key{width:200px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
  .card{background:#fff;border:1px solid #e3e1d9;border-radius:12px;padding:14px;display:flex;flex-direction:column}
  .card.on{border-color:#0f6e56;background:#f4fbf8}
  .card.bad{border-color:#e0a3a3}
  .name{font-size:15px;font-weight:500;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;gap:8px}
  .tag{font-size:11px;padding:2px 8px;border-radius:999px;background:#e1f5ee;color:#0f6e56;white-space:nowrap}
  .tag.off{background:#f1efe8;color:#888780}
  .tag.warn{background:#fcebeb;color:#a32d2d}
  .meta{font-size:11px;color:#888780;word-break:break-all;line-height:1.8}
  .meta b{color:#5f5e5a;font-weight:400}
  .ops{margin-top:auto;padding-top:12px;display:flex;gap:6px;flex-wrap:wrap}
  .ops button{padding:5px 10px;font-size:12px}
  .empty{color:#888780;font-size:13px;padding:32px;text-align:center;grid-column:1/-1}
  .mask{position:fixed;inset:0;background:rgba(44,44,42,.32);display:flex;align-items:center;justify-content:center;padding:20px;z-index:50}
  .mask[hidden]{display:none}
  .dialog{background:#fff;border-radius:12px;padding:20px;width:520px;max-width:100%;max-height:88vh;overflow:auto}
  .dialog.wide{width:640px}
  .dialog h2{font-size:15px;font-weight:500;margin:0 0 16px}
  .dialog label{display:block;font-size:12px;color:#5f5e5a;margin-bottom:12px}
  .dialog label input,.dialog label select,.dialog label textarea{display:block;width:100%;margin-top:5px}
  .hint{color:#888780;font-size:11px;margin-left:6px}
  .row{display:flex;gap:6px;margin-top:5px}
  .row input{margin-top:0 !important;flex:1}
  .row button{white-space:nowrap}
  .row2{display:flex;gap:12px}
  .row2 label{flex:1}
  .err{color:#a32d2d;font-size:12px;min-height:18px;line-height:1.6}
  .err div{margin-bottom:2px}
  .foot{display:flex;justify-content:flex-end;gap:8px;margin-top:8px}
  .crumb{font-size:12px;color:#444441;background:#f1efe8;border-radius:8px;padding:8px 10px;word-break:break-all;margin-bottom:8px}
  .list{border:1px solid #e3e1d9;border-radius:8px;max-height:46vh;overflow:auto}
  .item{display:flex;justify-content:space-between;gap:10px;padding:7px 10px;font-size:12px;cursor:pointer;border-bottom:1px solid #f1efe8}
  .item:last-child{border-bottom:none}
  .item:hover{background:#e6f1fb}
  .item.dir{color:#0c447c}
  .item.file{color:#2c2c2a}
  .item .sz{color:#888780;font-size:11px;white-space:nowrap}
  #toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#2c2c2a;color:#fff;padding:9px 16px;border-radius:8px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:99;max-width:80vw}
  #toast.show{opacity:1}
  #toast.bad{background:#a32d2d}
</style>
</head>
<body>
<h1>GSVI 适配器 · 角色管理</h1>
<div class="sub" id="cur">加载中…</div>
<div class="bar">
  <button class="primary" onclick="openEdit('')">+ 新增角色</button>
  <button class="ghost" onclick="openSettings()">设置</button>
  <button class="ghost" onclick="load()">刷新</button>
  <input id="key" type="password" placeholder="API Key（未启用可留空）">
</div>
<div class="grid" id="cards"></div>

<div class="mask" id="modal" hidden>
  <div class="dialog">
    <h2 id="mTitle">新增角色</h2>
    <label>角色名<span class="hint">AstrBot 里「角色」栏填这个名字</span>
      <input id="f_name" placeholder="例如 爱莉希雅"></label>
    <label>参考音频<span class="hint">3~10 秒的 wav</span>
      <div class="row"><input id="f_ref" placeholder="点击右侧浏览选择"><button type="button" onclick="pick('f_ref','audio')">浏览…</button></div></label>
    <label>参考音频文本<span class="hint">必须和音频内容完全一致</span>
      <textarea id="f_ptext" rows="3" placeholder="这段音频里说的原话"></textarea></label>
    <label>参考音频语言
      <select id="f_plang">
        <option value="zh">中文</option><option value="en">英文</option><option value="ja">日文</option>
        <option value="ko">韩文</option><option value="yue">粤语</option><option value="auto">自动</option>
      </select></label>
    <label>GPT 权重 (.ckpt)
      <div class="row"><input id="f_gpt" placeholder="留空则沿用当前模型"><button type="button" onclick="pick('f_gpt','ckpt')">浏览…</button></div></label>
    <label>SoVITS 权重 (.pth)
      <div class="row"><input id="f_sovits" placeholder="留空则沿用当前模型"><button type="button" onclick="pick('f_sovits','pth')">浏览…</button></div></label>
    <div class="row2">
      <label>seed<span class="hint">-1 为随机</span><input id="f_seed" type="number" value="-1"></label>
      <label>并行推理<input id="f_par" type="checkbox" checked style="width:auto"></label>
    </div>
    <div class="err" id="mErr"></div>
    <div class="foot"><button class="ghost" onclick="closeModal()">取消</button><button class="primary" onclick="saveChar()">保存</button></div>
  </div>
</div>

<div class="mask" id="browser" hidden>
  <div class="dialog wide">
    <h2>选择文件</h2>
    <div class="crumb" id="bPath"></div>
    <div class="list" id="bList"></div>
    <div class="foot"><button class="ghost" onclick="closeBrowser()">取消</button></div>
  </div>
</div>

<div class="mask" id="settings" hidden>
  <div class="dialog">
    <h2>设置</h2>
    <label>试听文本<span class="hint">{name} 会替换成角色名</span>
      <input id="s_preview"></label>
    <label>试听文本语言<span class="hint">自动 = 按字符判断。选错会合成出静音</span>
      <select id="s_plang">
        <option value="auto">自动检测</option>
        <option value="zh">中文</option><option value="en">英文</option>
        <option value="ja">日文</option><option value="ko">韩文</option>
        <option value="yue">粤语</option>
      </select></label>
    <label>目录浏览器额外允许的目录<span class="hint">每行一个，留空则只能浏览项目目录</span>
      <textarea id="s_dirs" rows="4" placeholder="C:\Users\你的用户名\Desktop\参考音频"></textarea></label>
    <div class="err" id="sErr"></div>
    <div class="foot"><button class="ghost" onclick="closeSettings()">取消</button><button class="primary" onclick="saveSettings()">保存</button></div>
  </div>
</div>

<div id="toast"></div>
<script>
var STATE = {chars:[], current:{}, editing:'', picker:{field:'', kind:'all', path:''}};

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function base(p){ if(!p) return ''; var a=String(p).split(/[\\/]/); return a[a.length-1]||p; }
function key(){ return $('key').value.trim(); }
function hdr(json){
  var h={}; if(key()) h['X-API-Key']=key(); if(json) h['Content-Type']='application/json'; return h;
}
function toast(msg,bad){
  var t=$('toast'); t.textContent=msg; t.className='show'+(bad?' bad':'');
  clearTimeout(t._t); t._t=setTimeout(function(){ t.className=''; },2600);
}
async function req(url,opt){
  opt=opt||{};
  opt.headers=Object.assign(hdr(!!opt.body),opt.headers||{});
  var r=await fetch(url,opt), j=null;
  try{ j=await r.json(); }catch(e){}
  if(!r.ok){
    var m=(j&&(j.msg||j.detail))||('HTTP '+r.status);
    var err=new Error(m); err.data=j; throw err;
  }
  return j;
}
function fmtSize(n){ if(n>1048576) return (n/1048576).toFixed(1)+' MB'; return Math.round(n/1024)+' KB'; }

async function load(){
  try{
    var j=await req('/api/characters');
    STATE.chars=j.characters; STATE.current=j.current;
    $('cur').textContent='当前加载：'+(base(j.current.sovits_weights)||'未知')
      +(j.loaded_version?'　版本 '+j.loaded_version:'')+'　|　配置目录：'+j.base_dir;
    render();
  }catch(e){ toast(e.message,true); }
}

function render(){
  var box=$('cards');
  if(!STATE.chars.length){
    box.innerHTML='<div class="empty">还没有角色，点左上角「新增角色」开始</div>';
    return;
  }
  box.innerHTML=STATE.chars.map(function(c){
    var tag=c.loaded?'<span class="tag">已加载</span>'
      :(c.problems.length?'<span class="tag warn">配置有问题</span>'
      :(c.bound?'<span class="tag off">可切换</span>':'<span class="tag off">沿用当前</span>'));
    var cls='card'+(c.loaded?' on':'')+(c.problems.length?' bad':'');
    var warn=c.problems.length?'<div class="meta" style="color:#a32d2d">'+esc(c.problems.join('、'))+'</div>':'';
    return '<div class="'+cls+'">'
      +'<div class="name"><span>'+esc(c.name)+'</span>'+tag+'</div>'
      +'<div class="meta"><b>GPT：</b>'+esc(base(c.gpt_weights)||'沿用当前')+'</div>'
      +'<div class="meta"><b>SoVITS：</b>'+esc(base(c.sovits_weights)||'沿用当前')+'</div>'
      +'<div class="meta"><b>参考音频：</b>'+esc(base(c.ref_audio)||'未配置')+'</div>'
      +'<div class="meta"><b>语言：</b>'+esc(c.prompt_lang)+'　<b>seed：</b>'+esc(c.seed)+'</div>'
      +warn
      +'<div class="ops">'
      +'<button data-act="preview" data-name="'+esc(c.name)+'">试听</button>'
      +'<button data-act="switch" data-name="'+esc(c.name)+'">切换</button>'
      +'<button class="ghost" data-act="edit" data-name="'+esc(c.name)+'">编辑</button>'
      +'<button class="danger" data-act="del" data-name="'+esc(c.name)+'">删除</button>'
      +'</div></div>';
  }).join('');
}

$('cards').onclick=async function(ev){
  var b=ev.target.closest('button[data-act]');
  if(!b) return;
  var act=b.dataset.act, name=b.dataset.name;
  if(act==='edit'){ openEdit(name); return; }
  if(act==='del'){
    if(!confirm('确定删除角色「'+name+'」？')) return;
    try{ var r=await req('/api/characters/'+encodeURIComponent(name),{method:'DELETE'}); toast(r.msg); load(); }
    catch(e){ toast(e.message,true); }
    return;
  }
  b.disabled=true;
  try{
    if(act==='switch'){
      var r2=await req('/switch?name='+encodeURIComponent(name),{method:'POST'});
      toast(r2.msg); load();
    }else{
      toast('正在合成试听…');
      var r3=await req('/api/preview?name='+encodeURIComponent(name),{method:'POST'});
      new Audio(r3.audio_url).play();
      if(r3.warning){ toast(r3.warning,true); }
      else{ toast('试听：'+r3.text+'　（'+r3.text_lang+'）'); }
    }
  }catch(e){ toast(e.message,true); }
  finally{ b.disabled=false; }
};

function openEdit(name){
  STATE.editing=name;
  var c=null;
  for(var i=0;i<STATE.chars.length;i++) if(STATE.chars[i].name===name) c=STATE.chars[i];
  $('mTitle').textContent=name?('编辑角色 · '+name):'新增角色';
  $('f_name').value=c?c.name:'';
  $('f_ref').value=c?c.ref_audio:'';
  $('f_ptext').value=c?c.prompt_text:'';
  $('f_plang').value=c&&c.prompt_lang&&c.prompt_lang!=='跟随请求'?c.prompt_lang:'zh';
  $('f_gpt').value=c?c.gpt_weights:'';
  $('f_sovits').value=c?c.sovits_weights:'';
  $('f_seed').value=c?c.seed:-1;
  $('f_par').checked=c?!!c.parallel_infer:true;
  $('mErr').textContent='';
  $('modal').hidden=false;
}
function closeModal(){ $('modal').hidden=true; STATE.editing=''; }

async function saveChar(){
  var payload={
    name:$('f_name').value.trim(),
    ref_audio_path:$('f_ref').value.trim(),
    prompt_text:$('f_ptext').value.trim(),
    prompt_lang:$('f_plang').value,
    gpt_weights:$('f_gpt').value.trim(),
    sovits_weights:$('f_sovits').value.trim(),
    seed:parseInt($('f_seed').value||'-1',10),
    parallel_infer:$('f_par').checked
  };
  var url=STATE.editing?'/api/characters/'+encodeURIComponent(STATE.editing):'/api/characters';
  var method=STATE.editing?'PUT':'POST';
  try{
    var r=await req(url,{method:method,body:JSON.stringify(payload)});
    closeModal(); toast(r.msg); load();
    if(r.warnings&&Object.keys(r.warnings).length) setTimeout(function(){ toast(Object.values(r.warnings)[0],true); },2700);
  }catch(e){
    var box=$('mErr'); box.innerHTML='';
    if(e.data&&e.data.errors){
      Object.keys(e.data.errors).forEach(function(k){
        var d=document.createElement('div'); d.textContent='· '+e.data.errors[k]; box.appendChild(d);
      });
    }else{ box.textContent=e.message; }
  }
}

function pick(field,kind){
  STATE.picker={field:field,kind:kind,path:''};
  var cur=$(field).value.trim();
  $('browser').hidden=false;
  browse(cur?cur.replace(/[\\/][^\\/]*$/,''):'');
}
function closeBrowser(){ $('browser').hidden=true; }

async function browse(path){
  try{
    var j=await req('/api/browse?kind='+encodeURIComponent(STATE.picker.kind)+'&path='+encodeURIComponent(path||''));
    STATE.picker.path=j.path;
    $('bPath').textContent=j.path;
    var html='';
    if(j.parent) html+='<div class="item dir" data-t="dir" data-path="'+esc(j.parent)+'"><span>.. 上级目录</span><span class="sz"></span></div>';
    j.dirs.forEach(function(d){
      html+='<div class="item dir" data-t="dir" data-path="'+esc(d.path)+'"><span>'+esc(d.name)+'</span><span class="sz">目录</span></div>';
    });
    j.files.forEach(function(f){
      html+='<div class="item file" data-t="file" data-path="'+esc(f.path)+'"><span>'+esc(f.name)+'</span><span class="sz">'+fmtSize(f.size)+'</span></div>';
    });
    $('bList').innerHTML=html||'<div class="item" style="cursor:default;color:#888780">此目录下没有匹配的文件</div>';
  }catch(e){
    $('bList').innerHTML='<div class="item" style="cursor:default;color:#a32d2d">'+esc(e.message)+'</div>';
  }
}

$('bList').onclick=function(ev){
  var it=ev.target.closest('.item[data-t]');
  if(!it) return;
  if(it.dataset.t==='dir'){ browse(it.dataset.path); }
  else if(it.dataset.t==='file'){
    $(STATE.picker.field).value=it.dataset.path;
    closeBrowser();
  }
};

function openSettings(){
  req('/api/settings').then(function(j){
    $('s_preview').value=j.settings.preview_text||'';
    $('s_plang').value=j.settings.preview_lang||'auto';
    $('s_dirs').value=(j.settings.allow_dirs||[]).join('\n');
    $('sErr').textContent='';
    $('settings').hidden=false;
  }).catch(function(e){ toast(e.message,true); });
}
function closeSettings(){ $('settings').hidden=true; }

async function saveSettings(){
  var dirs=$('s_dirs').value.split('\n').map(function(s){ return s.trim(); }).filter(function(s){ return s; });
  try{
    var r=await req('/api/settings',{method:'PUT',body:JSON.stringify({
      allow_dirs:dirs, preview_text:$('s_preview').value.trim(),
      preview_lang:$('s_plang').value
    })});
    closeSettings(); toast(r.msg);
  }catch(e){ $('sErr').textContent=e.message; }
}

load();
</script>
</body>
</html>"""


@APP.get("/ui", response_class=HTMLResponse)
def ui_page():
    return HTMLResponse(UI_TEMPLATE)


@APP.get("/")
def root():
    return {
        "service": "GSVI Adapter for GPT-SoVITS",
        "status": "ok",
        "characters": len(CHARACTERS),
        "current_gpt": MODEL.current[0],
        "current_sovits": MODEL.current[1],
        "auth": bool(API_KEY),
        "config_file": str(CONFIG_FILE),
        "ui": "/ui",
    }


@APP.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    try:
        validate_characters()
        cleanup_audio_dir()
        threading.Thread(target=_cleanup_loop, daemon=True).start()

        if _args.bind_addr not in ("127.0.0.1", "localhost", "::1") and not API_KEY:
            print("!" * 64)
            print(f"[warn] 正在绑定 {_args.bind_addr}，但没有设置 API Key。")
            print("       局域网内任何人都能调用你的显卡；配置修改与目录浏览已限制为本机。")
            print("       建议加 -k 你的密钥，并在管理界面右上角填同一个值。")
            print("!" * 64)

        host = None if _args.bind_addr == "None" else _args.bind_addr
        print(f"GSVI Adapter starting on http://{_args.bind_addr}:{_args.port}")
        print(f"管理界面: http://127.0.0.1:{_args.port}/ui")
        uvicorn.run(app=APP, host=host, port=_args.port, workers=1)
    except Exception:
        traceback.print_exc()
        os.kill(os.getpid(), signal.SIGTERM)
