"""
GSVI 适配器启动器 —— 打包成 exe 后放在 GPT-SoVITS 根目录双击运行。

它本身不做任何 TTS 工作，只负责：
  1. 找到同目录下 GPT-SoVITS 自带的 runtime\\python.exe
  2. 用它启动同目录的 gsvi_adapter.py
  3. 出错时把窗口停住，让错误信息留在屏幕上

打包命令（在装有 pyinstaller 的环境里执行）：
    pyinstaller --onefile --console --name "启动GSVI适配器" gsvi_launcher.py
"""

import sys
import subprocess
from pathlib import Path

DEFAULT_ARGS = ["-a", "127.0.0.1", "-p", "8000"]
BANNER = "=" * 44


def force_utf8_console():
    """等价于 bat 里的 chcp 65001，否则中文在 GBK 控制台会乱码。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def base_dir() -> Path:
    """打包后取 exe 所在目录；直接跑 .py 时取脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def pause(msg: str = ""):
    try:
        input(msg or "\n按回车键退出…")
    except (EOFError, KeyboardInterrupt):
        pass


def main() -> int:
    force_utf8_console()
    base = base_dir()
    py = base / "runtime" / "python.exe"
    script = base / "gsvi_adapter.py"

    print(BANNER)
    print("  GSVI Adapter for GPT-SoVITS")
    print(f"  工作目录: {base}")
    print(BANNER)

    if not py.is_file():
        print(f"\n[错误] 找不到 GPT-SoVITS 自带的 Python：\n  {py}")
        print("\n请把本程序放到 GPT-SoVITS 项目根目录")
        print("（和 api_v2.py、runtime\\ 文件夹同一级）。")
        pause()
        return 2

    if not script.is_file():
        print(f"\n[错误] 找不到 gsvi_adapter.py：\n  {script}")
        print("\n请确认它和本程序在同一个目录。")
        pause()
        return 3

    args = sys.argv[1:] or DEFAULT_ARGS
    cmd = [str(py), str(script), *args]
    print("  启动命令: " + " ".join(c if " " not in c else f'"{c}"' for c in cmd))
    print("  关闭本窗口即停止服务。\n")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    try:
        code = subprocess.call(cmd, cwd=str(base))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:
        print(f"\n[错误] 启动失败：{exc}")
        pause()
        return 1

    if code != 0:
        print(f"\n[错误] 适配器异常退出，退出码 {code}，原因见上面的输出。")
        pause()
    return code


if __name__ == "__main__":
    sys.exit(main())
