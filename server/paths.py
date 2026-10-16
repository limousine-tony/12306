"""
运行时路径统一管理

数据目录（配置/会话/日志）的解析规则：
  - ROOT 可写（普通文件夹）       → ROOT/data/（与 exe 同级，便于携带）
  - ROOT 只读（Program Files 等） → %LOCALAPPDATA%/12306/data/（Windows 标准用户数据位置）

web/ 静态资源在打包态从 PyInstaller 的 _MEIPASS 解包目录读取（只读资源，不随数据迁移）。
"""

import os
import shutil
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    ROOT = Path(sys.executable).resolve().parent
    WEB_DIR = Path(getattr(sys, "_MEIPASS", ".")) / "web"
else:
    ROOT = Path(__file__).resolve().parent.parent   # 项目根目录
    WEB_DIR = ROOT / "web"                          # 前端静态文件

CLI_DIR = ROOT / "cli"                              # 旧版 CLI（config 迁移来源）


def _writable(p: Path) -> bool:
    """目录是否可写（建探针文件实测，不依赖 ACL 猜测）"""
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_data_dir(root: Path, writable=None) -> Path:
    """可写 → root/data；只读 → %LOCALAPPDATA%/12306/data"""
    if (writable or _writable)(root):
        return root / "data"
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "12306" / "data"


DATA_DIR = _resolve_data_dir(ROOT)
LOGS_DIR = DATA_DIR / "logs"

CONFIG_FILE = DATA_DIR / "web_config.json"          # Web 配置
WEB_SESSION_FILE = DATA_DIR / "web_session.pkl"     # Web 登录会话
CLI_SESSION_FILE = DATA_DIR / "12306_cookies.pkl"   # CLI 登录会话（历史遗留）
WEB_LOG_FILE = LOGS_DIR / "web.log"

# 旧根目录遗留文件（重构前的位置）——启动时自动搬迁到 data/
_LEGACY_MOVES = [
    (ROOT / "web_config.json", CONFIG_FILE),
    (ROOT / "web_session.pkl", WEB_SESSION_FILE),
    (ROOT / "12306_cookies.pkl", CLI_SESSION_FILE),
    (ROOT / "12306_web.log", WEB_LOG_FILE),
]


def ensure_dirs():
    """创建 data/ 目录树，并搬运历史遗留数据（含 exe 换位置后旧 data/ 的整体迁移）"""
    # exe 从可写目录迁到只读位置（如 Program Files）时，把旁边的旧 data/ 复制过来，
    # 保证配置与登录会话连续（复制而非移动，原件留作备份）
    legacy_data = ROOT / "data"
    if (legacy_data.is_dir() and DATA_DIR != legacy_data
            and not DATA_DIR.exists()):
        try:
            shutil.copytree(legacy_data, DATA_DIR)
        except OSError:
            pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    for src, dst in _LEGACY_MOVES:
        try:
            if src.exists() and not dst.exists():
                src.rename(dst)
        except OSError:
            pass    # 跨盘/占用等情况忽略，下次启动再试


ensure_dirs()
