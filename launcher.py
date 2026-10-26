"""Portable entry point. Persistent runtime files live outside the repository."""
import os
import sys
from pathlib import Path

repo = Path(__file__).resolve().parent
# Accept the previous environment key only when the current key is absent.
local = Path(os.environ.get("BOYO_LOCAL") or os.environ.get("LIUJI_LOCAL") or repo.parent / "local")
for folder in ("tmp", "cache/pycache", "cache/huggingface", "cache/matplotlib", "cache/qt"):
    (local / folder).mkdir(parents=True, exist_ok=True)
sys.pycache_prefix = str(local / "cache" / "pycache")
for key, value in {
    "BOYO_LOCAL": str(local), "TEMP": str(local / "tmp"), "TMP": str(local / "tmp"),
    "HF_HOME": str(local / "cache" / "huggingface"), "MPLCONFIGDIR": str(local / "cache" / "matplotlib"),
    "XDG_CACHE_HOME": str(local / "cache"), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
}.items():
    os.environ[key] = value
sys.path.insert(0, str(repo / "src"))

if __name__ == "__main__":
    try:
        from boyo.cli import main
        main()
    except Exception:
        import traceback
        details = traceback.format_exc()
        error_log = local / "logs" / "startup-error.log"
        error_log.parent.mkdir(parents=True, exist_ok=True)
        error_log.write_text(details, encoding="utf-8")
        if sys.stderr is not None:
            sys.stderr.write(details)
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                "Boyo 未能启动。错误记录已保存：\n" + str(error_log) + "\n\n" + details[-1500:],
                "Boyo 启动失败", 0x10)
        sys.exit(1)
