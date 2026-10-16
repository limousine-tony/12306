"""
运行时路径统一管理

所有生成文件（配置/会话/日志）集中在 ROOT/data/ 目录，该目录被 .gitignore 忽略。

两种运行形态：
  - 源码运行:  ROOT = 项目根目录（server/ 的上级）
  - 打包运行:  ROOT = exe 所在目录（数据与 exe 同级，config/会话可与源码版共用）；
               web/ 静态资源从 PyInstaller 的 _MEIPASS 解包目录读取
"""

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

DATA_DIR = ROOT / "data"
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
    """创建 data/ 目录树，并把重构前散落在根目录的运行时文件搬进来"""
    DATA_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    for src, dst in _LEGACY_MOVES:
        try:
            if src.exists() and not dst.exists():
                src.rename(dst)
        except OSError:
            pass    # 跨盘/占用等情况忽略，下次启动再试


ensure_dirs()
