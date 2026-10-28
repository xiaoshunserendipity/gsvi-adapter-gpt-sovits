"""
制作分发包 —— 把「适配器 + 角色音色」打成可以直接发给别人的 zip。

用法：
    双击 制作分发包.bat
    或   runtime\\python.exe 制作分发包.py

包里会有：
    gsvi_adapter.py / 启动GSVI适配器.exe / 启动GSVI适配器.bat / README.md
    weights\\   各角色的 .ckpt 与 .pth
    refs\\      各角色的参考音频
    使用说明.txt   给对方的操作步骤
    音色清单.txt   每个角色的参考文本、语言、对应文件，方便对方在网页上重建

注意：包里**不含** gsvi_characters.json —— 那个文件里全是本机的绝对路径，
发出去也用不了，对方在网页上按「音色清单」重建即可。
"""

import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "gsvi_characters.json"
OUTPUT = BASE_DIR / "GSVI适配器-分发包.zip"

ADAPTER_FILES = [
    "gsvi_adapter.py",
    "启动GSVI适配器.exe",
    "启动GSVI适配器.bat",
    "README.md",
]

INSTRUCTIONS = """GSVI 适配器 · 分发包
========================================

这个包让你在已经装好 GPT-SoVITS 的机器上，用上别人训练好的声音。
不需要装 Python —— 适配器直接用 GPT-SoVITS 自带的 runtime\\python.exe。

一、包里有什么
----------------------------------------
  gsvi_adapter.py          适配器主程序
  启动GSVI适配器.exe        启动器，双击这个
  启动GSVI适配器.bat        备用启动脚本
  README.md                完整文档
  weights\\                 各角色的 GPT(.ckpt) 与 SoVITS(.pth) 权重
  refs\\                    各角色的参考音频(.wav)
  音色清单.txt              每个角色的参考文本、语言、对应文件

二、怎么装
----------------------------------------
  1. 把 gsvi_adapter.py、启动GSVI适配器.exe、启动GSVI适配器.bat
     复制到你的 GPT-SoVITS 项目根目录
     （和 api_v2.py、runtime\\ 文件夹同一级）

  2. 把 weights\\ 和 refs\\ 也复制过去
     （放在别处也行，第 4 步浏览时选过去就可以）

  3. 打开 GPT_SoVITS\\configs\\tts_infer.yaml，确认 custom 段里
       version
       t2s_weights_path
       vits_weights_path
     三项指向你自己能用的权重。启动时的初始模型由这里决定，
     之后切换角色会自动换成角色绑定的权重。

  4. 双击 启动GSVI适配器.exe
     看到 "Uvicorn running on http://127.0.0.1:8000" 就是成功了。
     窗口里会打印一张角色自检表，缺文件的角色会标 !!。

  5. 浏览器打开 http://127.0.0.1:8000/ui
     点「+ 新增角色」，照着「音色清单.txt」填：
       角色名      —— 清单里的名字
       参考音频    —— 点「浏览…」选 refs\\ 里的 wav
       参考文本    —— 清单里的参考文本（必须一字不差）
       语言        —— 清单里的语言
       GPT 权重    —— 点「浏览…」选 weights\\ 里的 .ckpt
       SoVITS 权重 —— 点「浏览…」选 weights\\ 里的 .pth
     保存后点「试听」确认音色正常。

  6. 接 AstrBot：WebUI → 配置 → 服务提供商 → 新增
       提供商类型：GSVI TTS API
       API 地址：  http://127.0.0.1:8000
       API Key：   留空
       角色：      你在网页上建的角色名
     点「测试」，返回「合成成功」即可。

三、几个必须注意的点
----------------------------------------
  * 权重必须和你的 GPT-SoVITS 是同一代。
    v2Pro 的权重不能用在 v2 上，加载会失败或者出杂音。
    不确定的话，看启动窗口里的自检输出，或者看权重所在目录名。

  * GPT(.ckpt) 和 SoVITS(.pth) 必须成对使用，不能和别的角色的混搭。

  * 参考文本必须和音频里说的原话完全一致，差一个字音色就会飘。

  * 首次启动会自动生成 gsvi_characters.json，角色配置都存在这里。
    这个文件不要手改，全部在网页上改。

四、遇到问题
----------------------------------------
  详见 README.md 的「常见问题」一节。
  启动窗口不要关 —— 那是唯一的报错现场。
"""


def _abs(p):
    if not p:
        return None
    q = Path(str(p).replace("/", os.sep))
    if not q.is_absolute():
        q = BASE_DIR / q
    return q


def _dedupe(paths):
    """同一文件名来自不同目录时，给后面的加序号，避免互相覆盖。"""
    seen = {}
    out = {}
    for src in paths:
        name = Path(src).name
        if name in seen and seen[name] != src:
            stem, ext = os.path.splitext(name)
            n = 2
            while f"{stem}_{n}{ext}" in seen:
                n += 1
            name = f"{stem}_{n}{ext}"
        seen[name] = src
        out[src] = name
    return out


def main():
    if not CONFIG_FILE.is_file():
        print("找不到 gsvi_characters.json。")
        print("请先启动一次适配器，在网页 http://127.0.0.1:8000/ui 上创建角色，再来打包。")
        return 1

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"配置文件读取失败：{exc}")
        return 1

    chars = data.get("characters") or {}
    if not chars:
        print("配置里还没有角色，先去网页上创建。")
        return 1

    weight_paths, ref_paths, missing = [], [], []
    for name, char in chars.items():
        for key, bucket in (("gpt_weights", weight_paths),
                            ("sovits_weights", weight_paths),
                            ("ref_audio_path", ref_paths)):
            p = _abs(char.get(key))
            if not p:
                continue
            if p.is_file():
                if str(p) not in bucket:
                    bucket.append(str(p))
            else:
                missing.append((name, key, str(p)))

    if missing:
        print("以下文件找不到，先修好再打包：")
        for name, key, path in missing:
            print(f"  {name} 的 {key} -> {path}")
        return 1

    weights = _dedupe(weight_paths)
    refs = _dedupe(ref_paths)

    stage = Path(tempfile.mkdtemp(prefix="gsvi_pack_"))
    try:
        print("收集文件…")
        for fname in ADAPTER_FILES:
            src = BASE_DIR / fname
            if src.is_file():
                shutil.copy2(src, stage / fname)
                print(f"  + {fname}")
            else:
                print(f"  ! 缺少 {fname}（已跳过）")

        for sub, mapping in (("weights", weights), ("refs", refs)):
            (stage / sub).mkdir()
            for src, name in mapping.items():
                shutil.copy2(src, stage / sub / name)
                print(f"  + {sub}\\{name}")

        # 音色清单：对方照着在网页上重建角色
        lines = ["音色清单", "=" * 40, ""]
        for name, char in chars.items():
            gpt = _abs(char.get("gpt_weights"))
            sov = _abs(char.get("sovits_weights"))
            ref = _abs(char.get("ref_audio_path"))
            lines += [
                f"角色名：{name}",
                f"  参考音频：refs\\{refs.get(str(ref), '（未配置）') if ref else '（未配置）'}",
                f"  参考文本：{char.get('prompt_text') or '（空）'}",
                f"  语言：{char.get('prompt_lang') or 'zh'}",
                f"  GPT 权重：weights\\{weights.get(str(gpt), '（沿用当前）') if gpt else '（沿用当前）'}",
                f"  SoVITS 权重：weights\\{weights.get(str(sov), '（沿用当前）') if sov else '（沿用当前）'}",
                f"  seed：{char.get('seed', -1)}",
                "",
            ]
        (stage / "音色清单.txt").write_text("\n".join(lines), encoding="utf-8")
        (stage / "使用说明.txt").write_text(INSTRUCTIONS, encoding="utf-8")
        print("  + 音色清单.txt")
        print("  + 使用说明.txt")

        if OUTPUT.exists():
            OUTPUT.unlink()
        print("打包…")
        with zipfile.ZipFile(OUTPUT, "w") as z:
            for p in sorted(stage.rglob("*")):
                if not p.is_file():
                    continue
                ext = p.suffix.lower()
                # 模型和音频本身压不动，直接存储，省时间
                mode = zipfile.ZIP_STORED if ext in (".ckpt", ".pth", ".wav", ".exe") \
                    else zipfile.ZIP_DEFLATED
                z.write(p, p.relative_to(stage).as_posix(), compress_type=mode)

        size = OUTPUT.stat().st_size / 1024 / 1024
        print("-" * 46)
        print(f"完成：{OUTPUT.name}")
        print(f"体积：{size:.1f} MB")
        print(f"角色：{'、'.join(chars)}")
        print(f"路径：{OUTPUT}")
        print("-" * 46)
        print("提醒：别用邮件发 exe，多数邮箱会拦。走网盘或压缩包传输。")
        return 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
