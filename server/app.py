"""
FastAPI 入口 — 12306 Web 抢票服务

启动:  python -m server.app
访问:  http://127.0.0.1:8765 （仅绑定本机）

REST + WebSocket + 静态前端（web/index.html）。
"""

import asyncio
import logging
import os
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

# Windows GBK 控制台无法编码 emoji（🚄🎯🎉 等），会导致 print/日志 UnicodeEncodeError
# 崩溃。在任何输出前把 stdout/stderr 重配为 UTF-8（errors=replace 兜底）。
# 打包版(--windowed)或 pythonw 运行时 sys.stdout/stderr 为 None，uvicorn 的日志
# formatter 会对 sys.stderr 调 .isatty() 而崩溃（AttributeError: NoneType），
# 此处替换为 devnull 流兜底。
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name)
    if _stream is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
    else:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 允许 python server/app.py 直接运行
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "server"

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config_store import store
from .paths import WEB_DIR as _WEB_PATH, WEB_LOG_FILE
from .task_manager import BusLogHandler, bus, manager
from .railway_client import QueryError, RailwayError, logger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = str(_WEB_PATH)


# ==================== 日志 ====================

def setup_logging():
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(str(WEB_LOG_FILE), encoding="utf-8")
    fh.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(sh)
    logger.addHandler(fh)
    logger.addHandler(BusLogHandler())


# ==================== 应用 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("12306 Web 抢票服务启动")
    manager.client.prime()               # 预热会话 cookie
    manager.refresh_login(force=True)    # 恢复已保存的登录态
    yield
    manager.stop_task()
    logger.info("服务关闭")


app = FastAPI(title="12306 Web 抢票服务", lifespan=lifespan)


# ---------- 请求/响应模型 ----------

class ConfigPatch(BaseModel):
    config: Dict[str, Any]


class QueryReq(BaseModel):
    date: Optional[str] = None
    from_station: Optional[str] = None
    to_station: Optional[str] = None


class BookReq(BaseModel):
    ticket: Dict[str, Any]
    seat_type: str


class TaskStartReq(BaseModel):
    mode: str  # monitor | sniper


class LoginPollReq(BaseModel):
    uuid: str


# ---------- 页面 ----------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ---------- 状态与配置 ----------

@app.get("/api/status")
def api_status():
    return {
        "state": manager.state_snapshot(),
        "login": manager.login_snapshot(),
        "dry_run": store.get()["booking"].get("dry_run", False),
    }


@app.get("/api/config")
def api_get_config():
    return store.get()


@app.put("/api/config")
def api_put_config(body: ConfigPatch):
    try:
        return {"ok": True, "config": store.update(body.config)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ---------- 登录 ----------

@app.post("/api/login/qr")
def api_login_qr():
    try:
        image_b64, uuid_ = manager.client.create_qr()
        return {"ok": True, "image_base64": image_b64, "uuid": uuid_}
    except Exception as e:
        logger.exception("获取二维码失败")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.post("/api/login/poll")
def api_login_poll(body: LoginPollReq):
    """
    前端每 2 秒轮询。返回 status:
      waiting | scanned | confirmed | expired | error
    """
    try:
        code, message = manager.client.check_qr(body.uuid)
    except Exception as e:
        return {"ok": False, "status": "error", "message": str(e)}
    if code == "0":
        return {"ok": True, "status": "waiting"}
    if code == "1":
        return {"ok": True, "status": "scanned",
                "message": "已扫码，请在手机上确认"}
    if code == "2":
        ok, msg = manager.client.complete_login()
        if ok:
            manager.refresh_login(force=True)
            logger.info(f"扫码登录成功: {manager.client.username}")
            return {"ok": True, "status": "confirmed",
                    "username": manager.client.username}
        return {"ok": False, "status": "error", "message": f"登录校验失败: {msg}"}
    if code == "3":
        return {"ok": True, "status": "expired", "message": "二维码已过期，请刷新"}
    if code == "":
        # check_qr 遇到 WAF/重定向/非 JSON —— 如实上报，前端提示刷新二维码
        return {"ok": False, "status": "error",
                "message": message or "扫码状态查询失败，请刷新二维码重试"}
    return {"ok": True, "status": "waiting", "message": message}


@app.post("/api/login/check")
def api_login_check():
    ok = manager.refresh_login(force=True)
    return {"ok": True, "logged_in": ok, "username": manager.client.username}


# ---------- 数据 ----------

@app.get("/api/stations")
def api_stations():
    try:
        stations = manager.client.load_stations()
        return {"ok": True, "stations": stations}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.get("/api/passengers")
def api_passengers():
    """账户常用联系人（脱敏证件号），供前端勾选"""
    if not manager.refresh_login():
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    try:
        passengers = manager.client.get_passengers()
    except RailwayError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    out = []
    for p in passengers:
        out.append({
            "name": p.get("passenger_name", ""),
            "id_number": p.get("passenger_id_no", ""),   # API 返回即脱敏号
            "id_type": p.get("passenger_id_type_name", ""),
            "passenger_type": p.get("passenger_type", "1"),
            # normal_passengers 即已通过身份核验、可购票的联系人
            "verified": True,
        })
    return {"ok": True, "passengers": out}


@app.post("/api/query")
def api_query(body: QueryReq):
    """手动单次查询（可临时覆盖行程参数，不改配置）"""
    try:
        if body.date and body.from_station and body.to_station:
            tickets = manager.client.query_tickets(
                body.date, body.from_station, body.to_station)
        else:
            tickets = manager.do_query()
        return {"ok": True, "tickets": tickets}
    except QueryError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except RailwayError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/book")
def api_book(body: BookReq):
    result = manager.manual_book(body.ticket, body.seat_type)
    if not result.get("ok"):
        return JSONResponse(result, status_code=409)
    return result


# ---------- 任务 ----------

@app.post("/api/task/start")
def api_task_start(body: TaskStartReq):
    result = manager.start_task(body.mode)
    if not result.get("ok"):
        return JSONResponse(result, status_code=409)
    return result


@app.post("/api/task/stop")
def api_task_stop():
    return manager.stop_task()


@app.get("/api/tickets")
def api_tickets():
    return {
        "ok": True,
        "tickets": manager.latest_tickets,
        "query_ok": manager.latest_query_ok,
        "query_count": manager.query_count,
    }


# ---------- WebSocket ----------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    q = bus.subscribe()
    loop = asyncio.get_running_loop()
    try:
        # 连接即推送全量状态
        await ws.send_json({
            "type": "hello",
            "state": manager.state_snapshot(),
            "login": manager.login_snapshot(),
            "tickets": manager.latest_tickets,
        })
        while True:
            try:
                event = await loop.run_in_executor(None, _blocking_get, q)
            except Exception:
                break
            if event is None:
                break
            try:
                await ws.send_json(event)
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(q)


def _blocking_get(q, timeout: float = 25.0):
    """带超时的阻塞读取，超时发心跳防止代理断连"""
    try:
        return q.get(timeout=timeout)
    except Exception:
        return {"type": "ping", "ts": time.time()}


# ==================== 启动 ====================

def main():
    import uvicorn
    cfg = store.get()["server"]
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8765))
    url = f"http://{host}:{port}"
    if "--no-browser" not in sys.argv:   # 启动器传此参数时由启动器决定是否开网页
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\n  🚄 12306 Web 抢票服务: {url}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
