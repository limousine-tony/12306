"""
TaskManager — 后台任务调度与事件广播

任务模式:
  monitor  长期监控捡漏（移植 TicketBot 三态自适应轮询）
  sniper   定时狙击（移植 SniperBot 时刻表 + 服务器校时 + 预热）
  手动下单  前端点击【预订】触发一次性 book()

所有状态变化通过 EventBus 广播到 WebSocket；同一时刻最多一个自动任务，
所有下单（自动/手动）互斥，防止并发重复下单。
"""

import logging
import queue
import random
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import requests

from .config_store import store
from .railway_client import (
    BookResult, NotLoggedInError, QueryError, RailwayClient, logger,
)
from . import protocol as P


# ==================== 事件总线 ====================

class EventBus:
    """线程安全的多订阅者事件广播，带最近事件回放（新连接补齐上下文）"""

    def __init__(self, history_size: int = 300, q_size: int = 500):
        self._subs: List["queue.Queue[dict]"] = []
        self._history: List[dict] = []
        self._history_size = history_size
        self._q_size = q_size
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue[dict]":
        q: "queue.Queue[dict]" = queue.Queue(maxsize=self._q_size)
        with self._lock:
            for ev in self._history:
                q.put_nowait(ev)
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict):
        event = {"ts": time.time(), **event}
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_size:
                del self._history[: len(self._history) - self._history_size]
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)   # 消费太慢的订阅者丢弃旧事件
            for q in dead:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass


bus = EventBus()


def emit(event: dict):
    bus.publish(event)


def emit_log(level: str, msg: str):
    emit({"type": "log", "level": level, "msg": msg})


class BusLogHandler(logging.Handler):
    """把 web12306 logger 的记录转发到事件总线（在 app.py 挂载）"""

    def emit(self, record):
        try:
            emit_log(record.levelname, record.getMessage())
        except Exception:
            pass


# ==================== 推送通知（移植 common.BaseBot._send_notify） ====================

def send_notify(title: str, content: str):
    cfg = store.get().get("notify", {})
    channel, key = cfg.get("channel", ""), cfg.get("key", "")
    if not channel or not key:
        return
    try:
        if channel == "serverchan":
            requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": title, "desp": content}, timeout=10)
        elif channel == "pushdeer":
            requests.post("https://api2.pushdeer.com/message/push",
                          data={"pushkey": key, "text": f"{title}\n{content}"},
                          timeout=10)
        elif channel == "bark":
            requests.get(
                f"https://api.day.app/{key}/"
                f"{urllib.parse.quote(title)}/{urllib.parse.quote(content)}",
                timeout=10)
    except Exception as e:
        logger.warning(f"推送通知失败: {e}")


# ==================== 任务管理 ====================

class TaskManager:
    def __init__(self):
        self.client = RailwayClient()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._book_lock = threading.Lock()     # 所有下单互斥
        self.task_mode: Optional[str] = None    # monitor | sniper
        self.task_state = "idle"                # idle|waiting|prewarm|running|booking|paused|stopping
        self.task_message = ""
        self.countdown_sec: Optional[float] = None
        self.latest_tickets: List[Dict[str, Any]] = []
        self.latest_query_ok = False
        self.query_count = 0
        self.started_at: Optional[float] = None
        self.book_active = False
        self.book_stage = ""
        self.last_order: Optional[Dict[str, Any]] = None
        self._fail_cooldown: Dict[tuple, float] = {}   # (车次,席别)→冷却截止ts
        self._state_lock = threading.Lock()

    # ---------- 状态 ----------

    def state_snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "task": {
                    "mode": self.task_mode,
                    "state": self.task_state,
                    "message": self.task_message,
                    "countdown_sec": self.countdown_sec,
                    "started_at": self.started_at,
                    "query_count": self.query_count,
                },
                "booking": {
                    "active": self.book_active,
                    "stage": self.book_stage,
                },
                "last_order": self.last_order,
                "clock_offset": self.client._clock_offset,
                "query_endpoint": self.client._query_endpoint,
            }

    def login_snapshot(self) -> Dict[str, Any]:
        return {
            "logged_in": self._logged_in_cache.get("value", False),
            "username": self.client.username,
            "checked_at": self._logged_in_cache.get("at", 0),
        }

    _logged_in_cache: Dict[str, Any] = {"value": False, "at": 0.0}

    def refresh_login(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self._logged_in_cache["at"] < 10:
            return self._logged_in_cache["value"]
        ok = self.client.check_login()
        if ok and not self.client.username:
            self.client.fetch_username()
        self._logged_in_cache = {"value": ok, "at": now}
        emit({"type": "login", "logged_in": ok, "username": self.client.username})
        return ok

    def _set_state(self, state: str, message: str = "", **extra):
        with self._state_lock:
            self.task_state = state
            if message:
                self.task_message = message
            for k, v in extra.items():
                setattr(self, k, v)
        emit({"type": "state", "state": self.state_snapshot()["task"]})

    # ---------- 任务启停 ----------

    def start_task(self, mode: str) -> Dict[str, Any]:
        if mode not in ("monitor", "sniper"):
            return {"ok": False, "error": f"未知模式: {mode}"}
        if self._thread and self._thread.is_alive():
            return {"ok": False, "error": f"已有任务在运行（{self.task_mode}），请先停止"}

        self._stop_event.clear()
        errors = self.validate_for_task(mode)
        if errors:
            return {"ok": False, "error": "；".join(errors)}
        self.task_mode = mode
        self.started_at = time.time()
        self.query_count = 0
        loop = self._monitor_loop if mode == "monitor" else self._sniper_loop
        self._thread = threading.Thread(
            target=self._task_wrapper, args=(loop,), daemon=True,
            name=f"task-{mode}")
        self._thread.start()
        return {"ok": True, "mode": mode}

    def _task_wrapper(self, loop: Callable[[], None]):
        try:
            loop()
        except Exception as e:
            logger.exception("任务异常退出")
            emit_log("ERROR", f"任务异常: {e}")
        finally:
            self._set_state("idle", "任务已结束", countdown_sec=None)
            self.task_mode = None

    def stop_task(self):
        if not self._thread or not self._thread.is_alive():
            return {"ok": True, "msg": "当前没有运行中的任务"}
        self._stop_event.set()
        self._set_state("stopping", "正在停止（等待当前步骤完成）...")
        return {"ok": True, "msg": "停止信号已发出"}

    def validate_for_task(self, mode: str = "") -> List[str]:
        cfg = store.get()
        errors = []
        trip = cfg["trip"]
        if not trip.get("train_date") or not trip.get("from_station") or not trip.get("to_station"):
            errors.append("行程（日期/出发站/到达站）未配置")
        if not cfg.get("seat_types"):
            errors.append("未选择座位类型")
        if not cfg.get("passengers"):
            errors.append("未选择乘车人")
        try:
            datetime.strptime(trip.get("train_date", ""), "%Y-%m-%d")
        except ValueError:
            errors.append("日期格式应为 YYYY-MM-DD")
        if mode == "sniper":
            try:
                hh, mm = cfg["sniper"]["release_time"].split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError
            except Exception:
                errors.append("放票时间格式应为 HH:MM（24小时制）")
        return errors

    # ---------- 查询 ----------

    def do_query(self, emit_tickets: bool = True) -> List[Dict[str, Any]]:
        cfg = store.get()
        trip = cfg["trip"]
        tickets = self.client.query_tickets(
            trip["train_date"], trip["from_station"], trip["to_station"])
        preferred = cfg.get("preferred_trains") or []
        if preferred:
            tickets.sort(key=lambda t: t["station_train_code"] not in preferred)
        with self._state_lock:
            self.latest_tickets = tickets
            self.latest_query_ok = True
            self.query_count += 1
        if emit_tickets:
            emit({"type": "tickets", "items": tickets,
                  "date": trip["train_date"],
                  "route": f"{trip['from_station']} → {trip['to_station']}"})
        return tickets

    def _safe_query(self) -> Optional[List[Dict[str, Any]]]:
        try:
            return self.do_query()
        except QueryError as e:
            with self._state_lock:
                self.latest_query_ok = False
            emit_log("WARNING", f"查询失败: {e}")
            return None
        except NotLoggedInError:
            self.refresh_login(force=True)
            emit_log("ERROR", "登录已失效，任务暂停——请在页面重新扫码")
            return None

    # ---------- 下单（互斥） ----------

    def _seat_stock_rank(self, ticket: Dict[str, Any], seat: str) -> int:
        """余票数排序权重：数字取整，'有' 视为 99（充足）"""
        val = ticket["seats"].get(seat, "无")
        if val == "有":
            return 99
        try:
            return int(val)
        except ValueError:
            return 0

    def _in_cooldown(self, train_code: str, seat: str) -> bool:
        """终局失败（余票不足等）的 车次+席别 冷却期内跳过，减少无效请求"""
        until = self._fail_cooldown.get((train_code, seat))
        if until is None:
            return False
        if time.time() < until:
            return True
        del self._fail_cooldown[(train_code, seat)]
        return False

    def _mark_cooldown(self, train_code: str, seat: str, message: str):
        if P.is_terminal_order_failure(message):
            self._fail_cooldown[(train_code, seat)] = time.time() + 60

    def _candidates(self, tickets: List[Dict[str, Any]], cfg: Dict[str, Any],
                    allow_fallback: bool) -> List[tuple]:
        """
        筛选可下单候选 (ticket, seat)：配置席别有票；preferred 存在时默认严格过滤。
        同一优先级层内按余票量降序（票多的成功率更高），并跳过冷却期车次。
        """
        preferred = cfg.get("preferred_trains") or []
        preferred_set = set(preferred)
        seat_types = cfg.get("seat_types") or []
        out = []
        for t in tickets:
            code = t["station_train_code"]
            if preferred and code not in preferred and not allow_fallback:
                continue
            if not t.get("can_buy_online", True):
                continue
            for seat in seat_types:
                if not P.is_seat_available(t["seats"].get(seat, "无")):
                    continue
                if self._in_cooldown(code, seat):
                    continue
                out.append((t, seat, code in preferred_set if preferred else True))
                break
        # 首选车次恒优先，同层内按余票量降序（票多成功率更高）
        out.sort(key=lambda x: (not x[2], -self._seat_stock_rank(x[0], x[1])))
        return [(t, s) for t, s, _ in out]

    def attempt_book(self, ticket: Dict[str, Any], seat_type: str,
                     passengers_api: List[Dict[str, Any]],
                     cfg: Dict[str, Any]) -> BookResult:
        """带互斥锁与进度广播的下单"""
        if not self._book_lock.acquire(blocking=False):
            return BookResult("rejected", "lock", message="已有下单进行中，请稍候")
        try:
            with self._state_lock:
                self.book_active = True
                self.book_stage = "submit"
            booking_cfg = cfg.get("booking", {})

            def progress(stage: str, msg: str):
                with self._state_lock:
                    self.book_stage = stage
                emit({"type": "book_progress", "stage": stage, "msg": msg})
                emit_log("INFO", f"[{stage}] {msg}" if msg else f"[{stage}]")

            result = self.client.book(
                ticket, seat_type, passengers_api,
                dry_run=bool(booking_cfg.get("dry_run")),
                choose_seats=booking_cfg.get("choose_seats", ""),
                seat_detail_type=booking_cfg.get("seat_detail_type", "000"),
                progress=progress,
                stop_check=self._stop_event.is_set,
            )
            self.last_order = {
                "status": result.status, "stage": result.stage,
                "order_id": result.order_id, "message": result.message,
                "train": ticket.get("station_train_code"), "seat": seat_type,
                "at": time.time(),
            }
            emit({"type": "order", "result": self.last_order})
            if result.status == "success":
                title = "抢票成功"
                content = (f"{ticket.get('station_train_code')} "
                           f"{ticket.get('from_station')}→{ticket.get('to_station')} "
                           f"{ticket.get('query_date')} 订单号: {result.order_id}")
                emit_log("INFO", f"🎉 {content}")
                send_notify(title, content)
            elif result.status == "unknown":
                emit_log("ERROR", f"⚠️ 订单结果未知: {result.message}")
                send_notify("12306 订单结果未知", result.message)
            elif result.status == "dry_run_ok":
                emit_log("INFO", f"✅ 试运行通过: {result.message}")
            return result
        finally:
            with self._state_lock:
                self.book_active = False
                self.book_stage = ""
            self._book_lock.release()

    def manual_book(self, ticket: Dict[str, Any], seat_type: str) -> Dict[str, Any]:
        """前端手动【预订】"""
        cfg = store.get()
        matched = self._matched_passengers(cfg)
        if not matched:
            return {"ok": False, "error": "未匹配到乘车人，请先在配置中选择"}
        if not self.refresh_login():
            return {"ok": False, "error": "登录已失效，请重新扫码"}

        def run():
            self.attempt_book(ticket, seat_type, matched, cfg)

        if not self._book_lock.acquire(blocking=False):
            return {"ok": False, "error": "已有下单进行中"}
        self._book_lock.release()
        threading.Thread(target=run, daemon=True, name="manual-book").start()
        return {"ok": True}

    def _matched_passengers(self, cfg: Dict[str, Any],
                            force: bool = False) -> List[Dict[str, Any]]:
        wanted = cfg.get("passengers") or []
        if not wanted:
            return []
        try:
            matched = self.client.match_passengers(wanted, force=force)
        except Exception as e:
            emit_log("ERROR", f"获取乘车人失败: {e}")
            return []
        if len(matched) != len(wanted):
            names = {m.get("passenger_name") for m in matched}
            missing = [w["name"] for w in wanted if w.get("name") not in names]
            emit_log("WARNING", f"以下乘车人未在账户联系人中匹配到: {missing}")
        return matched

    # ---------- 监控任务 ----------

    def _monitor_loop(self):
        self._set_state("running", "监控中")
        emit_log("INFO", "监控任务启动")
        if not self.refresh_login(force=True):
            emit_log("ERROR", "未登录，监控任务退出——请先扫码登录")
            return

        cfg = store.get()
        mon = cfg["monitor"]
        poll_mode = "normal"
        no_ticket_streak = 0
        fast_empty_streak = 0
        retries = 0
        max_retries = int(mon.get("max_retries", 0))

        while not self._stop_event.is_set():
            cfg = store.get()          # 每轮读取最新配置（前端可热更新）
            mon = cfg["monitor"]
            retries += 1
            if max_retries > 0 and retries > max_retries:
                emit_log("INFO", f"已达最大轮询次数 {max_retries}，任务结束")
                break

            # 维护时段
            hour = datetime.now().hour
            if mon.get("pause_in_maintenance", True) and (hour >= 23 or hour < 6):
                self._set_state("paused", "12306 维护时段（23:00–06:00），暂停查询")
                if self._stop_event.wait(60):
                    break
                continue
            if self.task_state == "paused":
                self._set_state("running", "监控中")

            tickets = self._safe_query()
            found = False
            if tickets is not None:
                candidates = self._candidates(tickets, cfg, allow_fallback=False)
                found = bool(candidates)
                if candidates and mon.get("auto_submit", True):
                    matched = self._matched_passengers(cfg)
                    if matched:
                        self._set_state("booking", f"发现 {len(candidates)} 个候选，下单中")
                        done = self._try_candidates(candidates, matched, cfg)
                        if done:
                            return
                        self._set_state("running", "监控中")
                    else:
                        emit_log("ERROR", "乘车人匹配失败，无法下单")
                elif candidates:
                    emit_log("INFO", f"发现 {len(candidates)} 个候选（auto_submit 关闭，仅提示）")

            mode_ref = [poll_mode]
            streaks = [no_ticket_streak, fast_empty_streak]
            interval = self._adaptive_interval(found, mon, mode_ref, streaks)
            poll_mode = mode_ref[0]
            no_ticket_streak, fast_empty_streak = streaks
            self._stop_event.wait(interval)

        emit_log("INFO", "监控任务已停止")

    def _adaptive_interval(self, found: bool, mon: Dict[str, Any],
                           mode_ref: List[str], streaks: List[int]) -> float:
        """三态自适应轮询（移植 TicketBot._get_sleep_interval）"""
        mode = mode_ref[0]
        if found:
            if mode != "fast":
                emit_log("INFO", "⚡ 发现可用余票，切换快速轮询")
                mode_ref[0] = "fast"
            streaks[0] = streaks[1] = 0
            return round(mon["fast_interval"] + random.uniform(-0.02, 0.02), 3)

        streaks[0] += 1
        if mode == "fast":
            streaks[1] += 1
            if streaks[1] >= int(mon.get("fast_mode_timeout", 5)):
                mode_ref[0] = "normal"
                streaks[1] = 0
                emit_log("INFO", "快速模式连续无票，回到正常轮询")
            return round(mon["fast_interval"] + random.uniform(-0.02, 0.02), 3)

        if streaks[0] >= int(mon.get("backoff_streak", 15)):
            if mode != "backoff":
                mode_ref[0] = "backoff"
                emit_log("INFO", f"🔄 连续 {streaks[0]} 次无票，进入退避模式")
        if mode_ref[0] == "backoff":
            extra = streaks[0] - int(mon.get("backoff_streak", 15))
            backoff = min(float(mon["query_interval"]) + 0.3 * extra,
                          float(mon.get("max_interval", 10.0)))
            return round(backoff + random.uniform(-0.15, 0.15), 2)

        base = float(mon["query_interval"])
        return round(base + base * random.uniform(-0.15, 0.15), 2)

    def _try_candidates(self, candidates: List[tuple], matched: List[Dict[str, Any]],
                        cfg: Dict[str, Any]) -> bool:
        """
        依次尝试候选。返回 True 表示任务应结束（成功/结果未知）。
        结果未知 → 立即停止全部自动重试（防重复下单）。
        """
        for ticket, seat in candidates:
            if self._stop_event.is_set():
                return True
            code = ticket.get("station_train_code")
            emit_log("INFO", f"🎯 尝试下单: {code} {seat}")
            result = self.attempt_book(ticket, seat, matched, cfg)
            if result.status == "success":
                self._set_state("idle", f"抢票成功！订单号 {result.order_id}")
                return True
            if result.status == "unknown":
                self._set_state("idle", "订单结果未知，任务已安全停止")
                return True
            if result.status == "dry_run_ok":
                emit_log("INFO", f"{code} 试运行验证通过（未真实下单）")
                # dry_run 下继续尝试意义不大，验证一次即可结束
                self._set_state("idle", "试运行完成：协议链路验证通过")
                return True
            self._mark_cooldown(code, seat, result.message)
            emit_log("WARNING", f"{code} 下单失败: {result.message}，尝试下一候选")
        return False

    # ---------- 狙击任务 ----------

    def _sniper_loop(self):
        self._set_state("waiting", "狙击任务启动")
        cfg = store.get()
        snp = cfg["sniper"]

        if not self.refresh_login(force=True):
            emit_log("ERROR", "未登录，狙击任务退出——请先扫码登录")
            return

        # 服务器校时
        emit_log("INFO", "正在与 12306 服务器校时...")
        self.client.sync_clock()

        release_at = self._next_release(snp["release_time"])
        emit_log("INFO", f"目标放票时间: {release_at.strftime('%Y-%m-%d %H:%M:%S')}（服务器时间）")

        # 等待预热时刻
        prewarm_at = release_at - timedelta(seconds=int(snp.get("pre_warm_seconds", 60)))
        while not self._stop_event.is_set():
            remaining = (prewarm_at - self.client.server_now()).total_seconds()
            self.countdown_sec = max(0, remaining)
            self._set_state("waiting", f"距预热还有 {int(remaining // 60)}分{int(remaining % 60)}秒")
            if remaining <= 0:
                break
            self._stop_event.wait(min(30, max(1, remaining)))
        if self._stop_event.is_set():
            return

        # 预热
        self._set_state("prewarm", "预热中：登录复查 / 端点发现 / 乘车人缓存")
        self.client.prime()
        matched = self._matched_passengers(cfg, force=True)   # 强制刷新缓存
        if not matched:
            emit_log("ERROR", "乘车人匹配失败，无法下单（狙击仍会查询）")
        try:
            warm_tickets = self._safe_query()
            if warm_tickets is not None:
                emit_log("INFO", f"预热查询成功（{len(warm_tickets)} 个车次），端点已就绪")
        except Exception:
            pass

        # 等待放票瞬间（T-10s 二次精确校时，T-2s 预热连接）
        resynced = warmed = False
        while not self._stop_event.is_set():
            remaining = (release_at - self.client.server_now()).total_seconds()
            self.countdown_sec = max(0, remaining)
            if remaining <= 0:
                break
            if not resynced and remaining <= 10:
                resynced = True
                self.client.sync_clock()          # 临近放票二次校时，消掉等待期漂移
                remaining = (release_at - self.client.server_now()).total_seconds()
            if not warmed and remaining <= 2:
                warmed = True
                self.client.warm_connection()     # 焐热 TCP/TLS，爆发首查零握手
            self._set_state("prewarm", f"距放票 {remaining:.1f} 秒")
            self._stop_event.wait(min(5, max(0.02, remaining)))
        if self._stop_event.is_set():
            return

        # 狙击
        self._set_state("running", "🚀 放票时间到，高速轮询中")
        emit_log("INFO", "🚀 开始狙击")
        tried = set()
        interval = float(snp.get("sniper_interval", 0.3))
        max_attempts = int(snp.get("max_attempts", 30))
        fallback = bool(snp.get("fallback_to_other_trains", False))
        burst_until = time.time() + 15.0   # 放票后前 15s 激进窗口（票通常几秒内抢完）
        burst_interval = min(interval, 0.25)

        for round_no in range(1, max_attempts + 1):
            if self._stop_event.is_set():
                break
            tickets = self._safe_query()
            if tickets is None:
                self._stop_event.wait(self._sniper_interval_now(interval, burst_until, burst_interval))
                continue
            cfg = store.get()
            candidates = [
                (t, s) for (t, s) in self._candidates(tickets, cfg, allow_fallback=fallback)
                if t["station_train_code"] not in tried
            ]
            if candidates:
                self._set_state("booking", f"第 {round_no} 轮：发现候选，下单中")
                self.refresh_login()   # 10s 缓存，通常零开销
                matched = matched or self._matched_passengers(cfg)
                if matched:
                    done = self._try_candidates(candidates, matched, cfg)
                    for t, _ in candidates:
                        tried.add(t["station_train_code"])
                    if done:
                        return
                self._set_state("running", f"狙击第 {round_no}/{max_attempts} 轮")
            self._stop_event.wait(self._sniper_interval_now(interval, burst_until, burst_interval))

        self._set_state("idle", f"狙击结束：{max_attempts} 轮内未成功")
        emit_log("WARNING", "❌ 已达最大狙击轮次，未抢到票")

    @staticmethod
    def _sniper_interval_now(interval: float, burst_until: float,
                             burst_interval: float) -> float:
        """爆发窗口内用最小间隔；窗口外正常间隔 + ±20% 抖动（降低风控特征）"""
        if time.time() < burst_until:
            return burst_interval
        return round(interval * random.uniform(0.8, 1.2), 3)

    def _next_release(self, release_hhmm: str) -> datetime:
        """以服务器时间计算下一个放票时刻"""
        hh, mm = (int(x) for x in release_hhmm.split(":"))
        now = self.client.server_now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target


manager = TaskManager()
