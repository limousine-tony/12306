"""
12306 抢票控制台 · 桌面启动器
=============================

双击【启动.bat】（或 python launcher.py）打开本界面，免去命令行操作：

  ▶ 启动服务      后台启动 Web 抢票服务（python -m server）
  ⏹ 停止服务      停止本启动器启动的实例；外部启动的也能按端口停止
  🌐 打开网页      打开 http://127.0.0.1:8765 控制台

下方日志框显示服务端原始日志（data/logs/web.log）与启动器操作记录。

仅用 Python 标准库（tkinter/urllib/subprocess），不依赖第三方包，
因此在 pip 安装完成之前启动器自身也能正常运行。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

# 路径统一由 server/paths.py 解析（纯标准库，pip 安装前也可用）：
# exe 在 Program Files 等只读位置时，数据/日志自动落到 %LOCALAPPDATA%\12306\data
from server.paths import FROZEN, ROOT as PROJECT_ROOT, WEB_LOG_FILE

ROOT = str(PROJECT_ROOT)
VENV_PY = os.path.join(ROOT, ".venv314", "Scripts", "python.exe")
WEB_URL = "http://127.0.0.1:8765"
PORT = 8765
LOG_FILE = str(WEB_LOG_FILE)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _enable_dpi_awareness():
    """Windows 高分屏下 tkinter 默认被位图拉伸发糊；开启 DPI 感知后按真实 DPI 渲染"""
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # Per-Monitor V1
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _scale_factor() -> float:
    import ctypes
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def _cleanup_stale_mei():
    """清扫历史 _MEI 解包残留（被强杀的 --server 子进程会留下）"""
    import glob
    import shutil
    import tempfile
    own = getattr(sys, "_MEIPASS", None)
    own = os.path.abspath(own) if own else None
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "_MEI*")):
        if own and os.path.abspath(d) == own:
            continue
        shutil.rmtree(d, ignore_errors=True)


def _quiet_exit():
    """
    退出前先清扫其他 _MEI 残留，再跳过 PyInstaller 的内置清理退出——
    内置清理在 DLL 仍被映射时删除 _MEI 必然失败并弹
    "Failed to remove temporary directory" 警告框。
    本进程自己的 _MEI 目录由下次启动的 _cleanup_stale_mei() 清扫。
    （不再用 cmd/ping 延迟删除——控制台窗口会闪出来。）
    """
    _cleanup_stale_mei()
    os._exit(0)


def find_server_cmd() -> list:
    """
    服务进程启动命令:
      打包版: exe 自身以 --server 运行（服务已内置，无需 Python 环境）
      源码版: .venv314 的 python -m server（--no-browser 由启动器决定开网页）
    """
    if FROZEN:
        return [sys.executable, "--server"]
    return [find_python(), "-m", "server", "--no-browser"]


def find_python() -> str:
    """服务/安装用解释器：优先 .venv314，其次当前解释器，再次 PATH"""
    if os.path.exists(VENV_PY):
        return VENV_PY
    if sys.executable and not sys.executable.lower().endswith("pythonw.exe"):
        return sys.executable
    return "python"


def check_status() -> dict | None:
    """探测服务状态（标准库实现，不要求已装 requests）"""
    try:
        with urllib.request.urlopen(WEB_URL + "/api/status", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def kill_port_listeners(port: int) -> list:
    """结束监听指定端口的进程（用于停止非本启动器启动的服务）"""
    killed = []
    try:
        out = subprocess.run("netstat -ano", shell=True, capture_output=True,
                             text=True, creationflags=CREATE_NO_WINDOW).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.split()[-1]
                if pid.isdigit() and pid not in killed:
                    subprocess.run(f"taskkill /F /T /PID {pid}", shell=True,
                                   capture_output=True, creationflags=CREATE_NO_WINDOW)
                    killed.append(pid)
    except Exception:
        pass
    return killed


class Launcher:
    BG = "#121826"
    BG2 = "#0d1220"
    FG = "#e8eef9"
    FG2 = "#9aa8c0"
    ACCENT = "#3b82f6"
    GREEN = "#22c55e"
    RED = "#ef4444"
    AMBER = "#f59e0b"

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, scrolledtext
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("12306 抢票控制台 · 启动器")
        s = _scale_factor()
        self.root.geometry(f"{int(760*s)}x{int(540*s)}")
        self.root.minsize(int(640*s), int(420*s))
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=self.BG)
        style.configure("TLabel", background=self.BG, foreground=self.FG, font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Sub.TLabel", foreground=self.FG2, font=("Microsoft YaHei UI", 9))
        style.configure("TCheckbutton", background=self.BG, foreground=self.FG2,
                        font=("Microsoft YaHei UI", 9))
        style.map("TCheckbutton", background=[("active", self.BG)])

        self._build_ui(scrolledtext, tk)

        self.proc = None              # 本启动器启动的服务子进程
        self._log_pos = 0             # web.log 读取位置
        self._log_file_ok = False
        self._closing = False

        self.root.after(300, self._tail_log)
        self.root.after(500, self._poll_status)
        threading.Thread(target=_cleanup_stale_mei, daemon=True).start()
        self.append_log("启动器就绪。点【启动服务】即可使用（无需安装依赖）。")

    # ---------------- UI ----------------

    def _build_ui(self, scrolledtext, tk):
        ttk = self.tk.ttk

        top = ttk.Frame(self.root, padding=(16, 14, 16, 6))
        top.pack(fill="x")
        ttk.Label(top, text="🚄 12306 抢票控制台", style="Title.TLabel").pack(side="left")
        self.status_lbl = ttk.Label(top, text="● 检测中…", style="Sub.TLabel")
        self.status_lbl.pack(side="right")

        bar = ttk.Frame(self.root, padding=(16, 8))
        bar.pack(fill="x")
        self.btn_start = tk.Button(bar, text="▶  启动服务", command=self.start_service,
                                   bg=self.ACCENT, fg="white", activebackground="#2f74e8",
                                   activeforeground="white", relief="flat", bd=0,
                                   font=("Microsoft YaHei UI", 10, "bold"),
                                   padx=18, pady=7, cursor="hand2")
        self.btn_stop = tk.Button(bar, text="⏹  停止服务", command=self.stop_service,
                                  bg=self.RED, fg="white", activebackground="#dc2626",
                                  activeforeground="white", relief="flat", bd=0,
                                  font=("Microsoft YaHei UI", 10, "bold"),
                                  padx=18, pady=7, cursor="hand2")
        self.btn_open = tk.Button(bar, text="🌐  打开网页", command=self.open_browser,
                                  bg=self.BG2, fg=self.FG, activebackground="#1a2234",
                                  activeforeground=self.FG, relief="flat", bd=0,
                                  font=("Microsoft YaHei UI", 10, "bold"),
                                  padx=18, pady=7, cursor="hand2")
        for b in (self.btn_start, self.btn_stop, self.btn_open):
            b.pack(side="left", padx=(0, 10))

        opts = ttk.Frame(self.root, padding=(16, 0, 16, 4))
        opts.pack(fill="x")
        self.auto_open = tk.BooleanVar(value=True)
        self.stop_on_exit = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="启动后自动打开网页",
                        variable=self.auto_open).pack(side="left")
        ttk.Checkbutton(opts, text="关闭启动器时同时停止服务",
                        variable=self.stop_on_exit).pack(side="left", padx=(18, 0))

        mid = ttk.Frame(self.root, padding=(16, 8, 16, 0))
        mid.pack(fill="both", expand=True)
        ttk.Label(mid, text="运行日志（服务端 data/logs/web.log + 启动器操作）",
                  style="Sub.TLabel").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(
            mid, height=16, bg=self.BG2, fg="#a7b6cf",
            insertbackground=self.FG, relief="flat",
            font=("Consolas", 9), wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))
        self.log_box.tag_configure("info", foreground="#a7b6cf")
        self.log_box.tag_configure("ok", foreground=self.GREEN)
        self.log_box.tag_configure("warn", foreground=self.ACCENT)
        self.log_box.tag_configure("err", foreground=self.RED)

        foot = ttk.Frame(self.root, padding=(16, 8, 16, 12))
        foot.pack(fill="x")
        self.foot_lbl = ttk.Label(foot, text=f"控制台地址: {WEB_URL}", style="Sub.TLabel")
        self.foot_lbl.pack(side="left")

    # ---------------- 日志 ----------------

    def append_log(self, text: str, tag: str = "info"):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _tail_log(self):
        """增量读取 web.log（服务端输出）"""
        if self._closing:
            return
        try:
            size = os.path.getsize(LOG_FILE)
            if not self._log_file_ok:
                self._log_pos = max(0, size - 16384)   # 首次只带出末尾 16KB
                self._log_file_ok = True
            if size > self._log_pos:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._log_pos)
                    chunk = f.read()
                    self._log_pos = f.tell()
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    tag = "err" if "ERROR" in line else "warn" if "WARNING" in line else "info"
                    self.append_log(line, tag)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        self.root.after(900, self._tail_log)

    # ---------------- 状态轮询 ----------------

    def _poll_status(self):
        if self._closing:
            return

        def work():
            st = check_status()
            self.root.after(0, lambda: self._apply_status(st))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(2500, self._poll_status)

    def _apply_status(self, st):
        if st:
            login = st.get("login", {})
            who = login.get("username") or "" if login.get("logged_in") else "未登录"
            task = st.get("state", {}).get("task", {})
            mode = task.get("mode") or ""
            extra = f" · 任务{mode}" if mode and task.get("state") != "idle" else ""
            self.status_lbl.configure(
                text=f"● 服务运行中 · {who}{extra}",
                foreground=self.GREEN)
        else:
            self.status_lbl.configure(text="● 服务未运行", foreground=self.RED)

    # ---------------- 操作 ----------------

    def start_service(self):
        if check_status():
            self.append_log("服务已在运行，无需重复启动。", "warn")
            if self.auto_open.get():
                self.open_browser()
            return
        cmd = find_server_cmd()
        self.append_log(f"启动服务中…（命令: {' '.join(cmd)}）")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.append_log(f"启动失败: {e}", "err")
            return
        self.append_log("服务进程已创建，等待就绪…", "ok")

        def wait_ready():
            for _ in range(30):
                if self._closing:
                    return
                if check_status():
                    self.root.after(0, lambda: self.append_log(
                        f"✅ 服务已就绪: {WEB_URL}", "ok"))
                    if self.auto_open.get():
                        self.root.after(0, self.open_browser)
                    return
                time.sleep(0.5)
            self.root.after(0, lambda: self.append_log(
                "⚠ 15 秒内未检测到服务就绪，请查看下方日志排查。", "warn"))

        threading.Thread(target=wait_ready, daemon=True).start()

    def stop_service(self):
        self.append_log("停止服务中…")
        stopped = False
        if self.proc and self.proc.poll() is None:
            try:
                subprocess.run(f"taskkill /F /T /PID {self.proc.pid}", shell=True,
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
                stopped = True
                self.append_log(f"已停止本启动器创建的服务进程 (PID {self.proc.pid})。", "ok")
            except Exception as e:
                self.append_log(f"停止失败: {e}", "err")
            self.proc = None
        if not stopped:
            pids = kill_port_listeners(PORT)
            if pids:
                self.append_log(f"已停止占用端口 {PORT} 的进程: {', '.join(pids)}", "ok")
            else:
                self.append_log("当前没有正在运行的服务。", "warn")

    def open_browser(self):
        import webbrowser
        webbrowser.open(WEB_URL)
        self.append_log(f"已打开浏览器: {WEB_URL}")

    def on_close(self):
        self._closing = True
        if self.stop_on_exit.get():
            self.stop_service()
        else:
            if self.proc and self.proc.poll() is None:
                self.append_log("启动器退出，服务继续在后台运行（可用停止服务按钮或任务管理器结束）。")
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    # 打包版服务模式：12306.exe --server —— 作为服务子进程运行（不启 GUI、不开浏览器）
    if "--server" in sys.argv:
        import multiprocessing
        multiprocessing.freeze_support()          # PyInstaller 冻结应用必需
        # 无控制台环境（--windowed/pythonw）下 sys.stdout/stderr 为 None，
        # uvicorn 日志 formatter 调 .isatty() 会崩 —— 先替换为 devnull 流
        for _name in ("stdout", "stderr"):
            if getattr(sys, _name) is None:
                setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
        try:
            import uvicorn
            from server.app import app
            from server.config_store import store
            cfg = store.get()["server"]
            uvicorn.run(app, host=cfg.get("host", "127.0.0.1"),
                        port=int(cfg.get("port", 8765)), log_level="warning")
        except Exception:
            import traceback
            try:
                os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"服务启动失败:\n{traceback.format_exc()}\n")
            except Exception:
                pass
            sys.exit(1)
        return
    if "--selftest" in sys.argv:
        _enable_dpi_awareness()
        app = Launcher()          # 构建 UI 验证可运行
        app.root.update()
        app.root.destroy()
        print("selftest OK")
        return
    _enable_dpi_awareness()
    Launcher().run()
    _quiet_exit()                 # 静默退出，替代 PyInstaller 内置清理（零弹窗）


if __name__ == "__main__":
    main()
