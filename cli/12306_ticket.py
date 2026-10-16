#!/usr/bin/env python3
"""
12306 自动抢票脚本
===================
功能:
  1. 手动登录 (浏览器扫码)
  2. 自动查询余票（CDN 并发）
  3. 自动提交订单
  4. 自动处理排队

使用方法:
  1. 修改 config.py 中的配置
  2. python 12306_ticket.py

依赖安装:
  pip install -r requirements.txt
"""

import json
import logging
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

import requests

import config
from common import BaseBot, is_seat_available, retry

# ==================== 日志 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("12306")


class TicketBot(BaseBot):
    """12306 抢票主程序（自适应轮询 + CDN 并发查询）"""

    # 12306 主机列表 (用于 Session 轮换)
    HOSTS = ["kyfw.12306.cn"]

    def __init__(self):
        super().__init__()

        # TicketBot 专属配置
        self.query_interval = config.QUERY_INTERVAL
        self.max_retries = config.MAX_RETRIES
        self.auto_submit = config.AUTO_SUBMIT

        # 替换成随机 host（与 Session 轮换机制配合）
        self.host = random.choice(self.HOSTS)
        self.base_url = f"https://{self.host}"

        # 自适应轮询状态
        self._poll_mode = "normal"       # normal | fast | backoff
        self._no_ticket_streak = 0       # 连续无票计数
        self._fast_empty_streak = 0      # 快速模式下连续无票计数

        # User-Agent / Session 轮换计数器
        self._query_count_since_rotate = 0
        self._query_count_since_session_rotate = 0

        # 查询页面是否已加载（避免每次 reload 页面）
        self._query_page_loaded = False
        self._query_count_since_refresh = 0
        self._page_refresh_interval = 50  # 每 50 次查询刷新一次页面，防会话过期

    # ============================================================
    #  查询余票（Selenium 浏览器驱动，绕过 WAF）
    # ============================================================

    def _build_poll_script(self, timeout_ms: int = 10000) -> str:
        """构建 XHR 数据异步轮询脚本（带超时）"""
        return (
            "var callback = arguments[arguments.length - 1];"
            "if (window.__capturedData) { callback(window.__capturedData); return; }"
            "var waited = 0;"
            "(function poll() {"
            "  if (window.__capturedData) { callback(window.__capturedData); }"
            f"  else if (waited >= {timeout_ms}) {{ callback(null); }}"
            "  else { waited += 30; setTimeout(poll, 30); }"
            "})();"
        )

    def query_tickets(self) -> list:
        """
        通过 Selenium 浏览器查询余票。

        首次调用加载完整页面，后续调用仅触发 searchTicket() AJAX，
        每 _page_refresh_interval 次刷新一次页面防止会话过期。
        """
        if not self._init_query_driver():
            return []

        driver = self._query_driver

        # 清除上次的捕获数据
        try:
            driver.execute_script("window.__capturedData = null;")
        except Exception:
            pass

        self._last_query_has_api_error = False
        refresh = (self._query_count_since_refresh >= self._page_refresh_interval)

        try:
            if not self._query_page_loaded or refresh:
                if refresh:
                    logger.debug("周期性刷新查询页面")
                    self._query_count_since_refresh = 0
                self._navigate_query_page()
                self._query_page_loaded = True
                raw = self._wait_for_xhr(driver, timeout_ms=10000)
                if not raw:
                    logger.debug("自动查询未触发，尝试手动触发...")
                    raw = self._manual_query(driver, timeout_ms=8000)
            else:
                self._query_count_since_refresh += 1
                raw = self._manual_query(driver, timeout_ms=5000)
        except Exception as e:
            logger.debug(f"浏览器查询异常: {e}")
            self._last_query_has_api_error = True
            try:
                driver.quit()
            except Exception:
                pass
            self._query_driver = None
            self._query_page_loaded = False
            return []

        if raw is None:
            logger.info("浏览器查询超时（无响应）")
            self._last_query_has_api_error = True
            return []

        # 解析原始 JSON
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("查询响应不是有效 JSON")
            self._last_query_has_api_error = True
            return []

        if not data.get("status"):
            logger.debug(f"查询接口返回异常: {data.get('messages', '')}")
            self._last_query_has_api_error = True
            return []

        raw_tickets = data.get("data", {}).get("result", [])
        if not raw_tickets:
            return []

        tickets = self._parse_tickets(raw_tickets)
        if self.preferred_trains:
            tickets = self._sort_by_preference(tickets)
        return tickets

    def _wait_for_xhr(self, driver, timeout_ms: int = 10000) -> str:
        """等待 XHR 捕获数据，返回原始 JSON 字符串或 None（超时/异常）"""
        try:
            return driver.execute_async_script(self._build_poll_script(timeout_ms))
        except Exception:
            return None

    def _manual_query(self, driver, timeout_ms: int = 8000) -> str:
        """
        通过浏览器内 fetch() 直接调用 12306 查票 API。

        绕开 searchTicket()（SPA 中可能不可用），利用浏览器已有会话和 Cookie
        直接 AJAX 查询。
        """
        try:
            cache_buster = int(time.time() * 1000)
            params = (
                f"leftTicketDTO.train_date={self.train_date}"
                f"&leftTicketDTO.from_station={self.from_station_code}"
                f"&leftTicketDTO.to_station={self.to_station_code}"
                f"&purpose_codes=ADULT&_{cache_buster}"
            )
            # 尝试当前 API 端点 queryI，失败则降级到 queryZ
            driver.execute_script(f"""
                window.__capturedData = null;
                fetch('/otn/leftTicket/queryI?{params}')
                    .then(function(r) {{ if (!r.ok) throw Error(r.status); return r.text(); }})
                    .then(function(t) {{ window.__capturedData = t; }})
                    .catch(function() {{
                        fetch('/otn/leftTicket/queryZ?{params}')
                            .then(function(r) {{ return r.text(); }})
                            .then(function(t) {{ window.__capturedData = t; }});
                    }});
            """)
            return driver.execute_async_script(self._build_poll_script(timeout_ms))
        except Exception as e:
            logger.debug(f"手动查询失败: {e}")
            return None

    # ============================================================
    #  显示车次
    # ============================================================

    def display_tickets(self, tickets: list):
        """以表格形式显示车次信息"""
        if not tickets:
            logger.info("暂无可用车次信息")
            return

        from prettytable import PrettyTable

        table = PrettyTable()
        table.field_names = [
            "车次", "出发站", "到达站", "出发", "到达", "历时",
        ] + self.seat_types

        for ticket in tickets:
            row = [
                ticket["train_no"],
                ticket["from_station_name"],
                ticket["to_station_name"],
                ticket["depart_time"],
                ticket["arrive_time"],
                ticket["duration"],
            ]
            for seat in self.seat_types:
                val = ticket["seats"].get(seat, "无")
                if is_seat_available(val):
                    row.append(f"\033[32m{val}\033[0m")  # 绿色
                else:
                    row.append("无")
            table.add_row(row)

        print()
        print(table)
        print()

    # ============================================================
    #  提交订单
    # ============================================================

    def submit_order(self, ticket: dict) -> bool:
        """提交车次订单"""
        logger.info(
            f"正在提交订单: {ticket['train_no']} "
            f"{ticket['depart_time']}-{ticket['arrive_time']}"
        )

        secret = urllib.parse.unquote(ticket.get("secret_str", ""))

        data = {
            "secretStr": secret,
            "train_date": ticket["date"],
            "back_train_date": ticket["date"],
            "tour_flag": "dc",
            "purpose_codes": "ADULT",
            "query_from_station_name": ticket["from_station_name"],
            "query_to_station_name": ticket["to_station_name"],
            "undefined": "",
        }

        try:
            resp = self.session.post(
                f"{self.base_url}/otn/leftTicket/submitOrderRequest",
                data=data, timeout=10,
            )
            result = resp.json()
            if result.get("status"):
                logger.info("[OK] 订单提交成功！")
                return True
            else:
                logger.warning(f"订单提交失败: {result.get('messages', '未知错误')}")
                return False
        except Exception as e:
            logger.error(f"订单提交异常: {e}")
            return False

    # ============================================================
    #  获取订单 Token
    # ============================================================

    def get_order_token(self) -> bool:
        """获取订单确认所需的 token"""
        try:
            resp = self.session.get(
                f"{self.base_url}/otn/confirmPassenger/initDc", timeout=10,
            )

            patterns = [
                (r"globalRepeatSubmitToken\s*=\s*'([^']+)'", "repeat_submit_token"),
                (r"key_check_isChange\s*=\s*'([^']+)'", "key_check"),
                (r"leftTicketStr\s*=\s*'([^']+)'", "left_ticket_str"),
                (r"train_location\s*=\s*'([^']+)'", "train_location"),
            ]
            for pattern, attr in patterns:
                match = re.search(pattern, resp.text)
                if match:
                    setattr(self, attr, match.group(1))
                else:
                    setattr(self, attr, "")

            if not self.repeat_submit_token:
                logger.error("未获取到 submit token")
                return False

            logger.debug(f"获取到 repeatSubmitToken: {self.repeat_submit_token}")
            return True

        except Exception as e:
            logger.error(f"获取订单 token 失败: {e}")
            return False

    # ============================================================
    #  检查乘车人
    # ============================================================

    def check_passengers(self, early_check=False) -> list:
        """
        获取已添加的常用联系人并匹配配置中的乘车人。

        early_check=True: 启动时快速验证，无需 repeat_submit_token，仅记录日志。
        early_check=False: 下单时调用，需要 token，返回匹配的乘车人列表。
        """
        params = {"_": int(time.time() * 1000)}
        if not early_check:
            params["REPEAT_SUBMIT_TOKEN"] = self.repeat_submit_token

        try:
            resp = self.session.get(
                f"{self.base_url}/otn/confirmPassenger/getPassengerDTOs",
                params=params, timeout=10,
            )
            data = resp.json()

            if not data.get("status"):
                msg = data.get('messages', '未知错误')
                if early_check:
                    logger.warning(f"启动检查: 获取常用联系人失败（{msg}，放票时重试）")
                else:
                    logger.error(f"获取乘车人信息失败: {msg}")
                return [] if not early_check else []

            passengers = (data.get("data") or {}).get("normal_passengers") or []
            logger.info(f"查询到 {len(passengers)} 位常用联系人")

            for ap in passengers:
                logger.debug(f"  API联系人: {ap.get('passenger_name')} / {ap.get('passenger_id_no')} / 核验: {ap.get('passenger_id_type_name', '?')}")

            matched = []
            for p in self.passengers:
                for api_p in passengers:
                    if self._passenger_matches(api_p, p):
                        if early_check:
                            logger.info(f"  [OK] 乘车人 {p.get('name')} 已找到")
                        matched.append(api_p)
                        break
                else:
                    if early_check:
                        logger.warning(f"  乘车人 {p.get('name')} 未在12306常用联系人中，请检查是否已添加并通过核验")
                    else:
                        logger.warning(f"未在常用联系人中找到: {p.get('name')}")

            return matched

        except Exception as e:
            if early_check:
                logger.warning(f"启动检查乘车人异常: {e}（放票时重试）")
            else:
                logger.error(f"获取乘车人失败: {e}")
            return []

    # ============================================================
    #  确认订单
    # ============================================================

    def confirm_order(self, matched_passengers: list, ticket: dict) -> bool:
        """确认并提交订单（含铺位偏好尝试）"""
        # 选择座位类型
        chosen_seat = None
        for seat in self.seat_types:
            val = ticket["seats"].get(seat, "无")
            if is_seat_available(val):
                chosen_seat = seat
                break

        if not chosen_seat:
            logger.warning(f"没有符合座位要求的票: {self.seat_types}")
            return False

        seat_code = self.SEAT_CODE_MAP.get(chosen_seat, "O")
        logger.info(f"选择座位: {chosen_seat}, 乘车人: {len(matched_passengers)} 人")

        passenger_str = self._build_passenger_str(matched_passengers, seat_code)
        old_passenger_str = self._build_old_passenger_str(matched_passengers)

        # checkOrderInfo
        check_data = {
            "cancel_flag": "2",
            "bed_level_order_num": "000000000000000000000000000000",
            "passengerTicketStr": passenger_str,
            "oldPassengerStr": old_passenger_str,
            "tour_flag": "dc",
            "randCode": "",
            "whatsSelect": "1",
            "sessionId": "",
            "sig": "",
            "REPEAT_SUBMIT_TOKEN": self.repeat_submit_token,
        }

        try:
            resp = self.session.post(
                f"{self.base_url}/otn/confirmPassenger/checkOrderInfo",
                data=check_data, timeout=10,
            )
            result = resp.json()
            if not result.get("status"):
                logger.warning(f"订单检查失败: {result.get('messages', '')}")
                return False
            logger.info("[OK] 订单信息检查通过")
        except Exception as e:
            logger.error(f"订单检查异常: {e}")
            return False

        # 按铺位优先级尝试提交
        berth_attempts = self._get_berth_attempts(chosen_seat)
        confirm_url = f"{self.base_url}/otn/confirmPassenger/confirmSingleForQueue"

        for attempt_idx, (try_code, label) in enumerate(berth_attempts):
            if attempt_idx > 0:
                logger.info(f"  ← {label} 失败，尝试下一优先级")

            confirm_data = {
                "passengerTicketStr": passenger_str,
                "oldPassengerStr": old_passenger_str,
                "randCode": "",
                "purpose_codes": "00",
                "key_check_isChange": self.key_check,
                "leftTicketStr": self.left_ticket_str,
                "train_location": self.train_location,
                "choose_seats": try_code,
                "seatDetailType": "000",
                "roomType": "00",
                "dwAll": "N",
                "whatsSelect": "1",
                "REPEAT_SUBMIT_TOKEN": self.repeat_submit_token,
            }

            try:
                resp = self.session.post(confirm_url, data=confirm_data, timeout=15)
                result = resp.json()

                if result.get("status") and result.get("data", {}).get("submitStatus"):
                    order_id = result["data"].get("orderId", "")
                    logger.info(f"[OK] 订单提交成功！订单号: {order_id} ({label})")
                    self.order_id = order_id
                    return True
                else:
                    logger.info(f"  {label} 确认失败: {result.get('messages', '')}")

            except Exception as e:
                logger.info(f"  {label} 提交异常: {e}")

        logger.warning(f"所有铺位尝试均失败 ({chosen_seat})")
        return False

    # ============================================================
    #  排队查询
    # ============================================================

    def wait_for_queue(self) -> bool:
        """排队等待, 直到订单完成或超时 (2分钟)"""
        logger.info("正在排队等待出票...")

        max_wait = 120
        start_time = time.time()

        while self.running and (time.time() - start_time) < max_wait:
            params = {
                "random": str(time.time()).replace(".", "")[:17],
                "tourFlag": "dc",
                "REPEAT_SUBMIT_TOKEN": self.repeat_submit_token,
                "_": int(time.time() * 1000),
            }

            try:
                resp = self.session.get(
                    f"{self.base_url}/otn/confirmPassenger/queryOrderWaitTime",
                    params=params, timeout=10,
                )
                data = resp.json()
                if not data.get("status"):
                    continue

                result = data.get("data", {})
                wait_count = result.get("waitCount", -1)
                order_id = result.get("orderId", "")

                if wait_count == 0:
                    logger.info(f"[OK] 出票成功！订单号: {order_id}")
                    self.order_id = order_id
                    return True

                if wait_count < 0:
                    logger.info("排队中...")

                logger.debug(f"排队等待人数: {wait_count}")

            except Exception:
                pass

            time.sleep(2)

        logger.warning("排队超时")
        return False

    # ============================================================
    #  自适应轮询
    # ============================================================

    def _get_sleep_interval(self, found_matching: bool, api_error: bool = False) -> float:
        """
        根据当前状态和查询结果计算下一次等待间隔。

        状态机:
          normal ───有匹配票───→ fast
             ↑                      │
             │       连续N次无票     │
             │         ←────────────┘
             │
             └──连续无票──→ backoff
             ↑                │
             └──有票──────────┘
        """
        if found_matching:
            if self._poll_mode != "fast":
                logger.info("⚡ 发现可用余票，切换为快速轮询模式")
                self._poll_mode = "fast"
            self._fast_empty_streak = 0
            self._no_ticket_streak = 0
            return round(config.FAST_INTERVAL + random.uniform(-0.02, 0.02), 3)

        # ── 无可用票 ──
        if api_error:
            if self._poll_mode == "fast":
                return round(config.FAST_INTERVAL + random.uniform(-0.02, 0.02), 3)
            base = config.QUERY_INTERVAL
            return round(base + base * random.uniform(-0.15, 0.15), 2)

        self._no_ticket_streak += 1

        if self._poll_mode == "fast":
            self._fast_empty_streak += 1
            if self._fast_empty_streak >= config.FAST_MODE_TIMEOUT:
                logger.info("快速模式连续无票，回到正常轮询")
                self._poll_mode = "normal"
                self._fast_empty_streak = 0
            return round(config.FAST_INTERVAL + random.uniform(-0.02, 0.02), 3)

        if self._no_ticket_streak >= config.BACKOFF_STREAK:
            if self._poll_mode != "backoff":
                logger.info(f"🔄 已连续 {self._no_ticket_streak} 次无票，进入退避模式")
                self._poll_mode = "backoff"

        if self._poll_mode == "backoff":
            extra_steps = self._no_ticket_streak - config.BACKOFF_STREAK
            backoff = min(
                config.QUERY_INTERVAL + 0.3 * extra_steps,
                config.MAX_INTERVAL,
            )
            return round(backoff + random.uniform(-0.15, 0.15), 2)

        base = config.QUERY_INTERVAL
        jitter = base * random.uniform(-0.15, 0.15)
        return round(base + jitter, 2)

    # ============================================================
    #  User-Agent / Session 轮换
    # ============================================================

    def _rotate_user_agent(self):
        """随机切换 User-Agent"""
        new_ua = random.choice(self.USER_AGENTS)
        self.session.headers["User-Agent"] = new_ua
        logger.debug(
            f"切换 User-Agent 至 "
            f"{new_ua.split('Chrome/')[1].split()[0] if 'Chrome/' in new_ua else 'Safari'}"
        )

    def _rotate_session(self):
        """
        重建 requests.Session，保留登录 Cookies，重置连接指纹。
        降低长时间连续请求被服务端关联的风险。
        """
        old_cookies = self.session.cookies.get_dict()
        old_session = self.session
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.session.headers["User-Agent"] = random.choice(self.USER_AGENTS)

        for name, value in old_cookies.items():
            self.session.cookies.set(name, value, domain=".12306.cn")

        old_session.close()  # 释放旧连接池

        self.host = random.choice(self.HOSTS)
        self.base_url = f"https://{self.host}"
        self._query_count_since_rotate = 0

        logger.info(f"🔄 Session 轮换完成，当前节点: {self.host}")

    # ============================================================
    #  配置校验 (覆盖基类)
    # ============================================================

    def validate_config(self) -> bool:
        """启动前校验配置，不合法尽早报错"""
        errors = self._validate_config_base()

        if not self.passengers:
            if self.auto_submit:
                errors.append("AUTO_SUBMIT=True 但 PASSENGERS 为空")
            else:
                logger.warning("PASSENGERS 为空，仅查询模式无法下单")

        if not errors:
            return True

        self._print_config_errors(errors)
        return False

    # ============================================================
    #  维护时段检测
    # ============================================================

    def _check_maintenance_mode(self):
        """
        检查是否在 12306 维护时段 (23:00~06:00)
        是则自动暂停到次日 06:00
        """
        now = datetime.now()
        hour = now.hour
        if hour >= 23 or hour < 6:
            next_6am = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if hour >= 23:
                next_6am += timedelta(days=1)
            sleep_sec = (next_6am - now).total_seconds()
            logger.info(f"12306 维护时段 (23:00-06:00)，暂停 {sleep_sec/60:.0f} 分钟")
            logger.info(f"   将于 {next_6am.strftime('%H:%M')} 恢复查询")
            time.sleep(sleep_sec)
            return True
        return False

    # ============================================================
    #  购买工具方法
    # ============================================================

    def _find_available_tickets(self, tickets: list) -> list:
        """
        过滤有符合座位要求余票的车次。

        若配置了 PREFERRED_TRAINS，则只返回首选车次（非首选不碰）。
        """
        available = []
        for t in tickets:
            # 配置了首选车次时，只操作首选车次
            if self.preferred_trains and t["train_no"] not in self.preferred_trains:
                continue
            for seat in self.seat_types:
                val = t["seats"].get(seat, "无")
                if is_seat_available(val):
                    available.append(t)
                    break
        return available

    def _attempt_purchase(self, tickets: list):
        """
        尝试购买车票（每步最多重试一次，防网络抖动）
        """
        logger.info("=" * 50)
        logger.info(f"发现 {len(tickets)} 个可用车次, 尝试购买...")

        # 把浏览器的最新 Cookie 同步回来（浏览器中的 JSESSIONID 才是最新的）
        self._sync_browser_cookies_to_session()

        cached_matched = None
        for ticket in tickets:
            train_no = ticket['train_no']
            logger.info(f"尝试购买: {train_no} {ticket['depart_time']}-{ticket['arrive_time']}")

            # ---- 1. submitOrder (重试 ×1) ----
            if not retry(2, lambda: self.submit_order(ticket), label="submit"):
                logger.warning(f"  {train_no} 提交订单失败, 尝试下一车次")
                continue

            time.sleep(0.3)

            # ---- 2. getOrderToken (重试 ×1) ----
            if not retry(2, self.get_order_token, label="获取 token"):
                logger.error(f"  {train_no} 获取订单 token 失败")
                continue

            # ---- 3. checkPassengers (重试 ×1) ----
            # 复用之前查到的乘车人（同一会话内数据不变），避免重复 HTTP 请求
            if cached_matched is None:
                cached_matched = retry(2, self.check_passengers, label="获取乘车人")
            matched = cached_matched
            if not matched:
                logger.error(f"  {train_no} 未找到匹配的乘车人")
                continue

            # ---- 4. confirmOrder (重试 ×1) ----
            if not retry(2, lambda: self.confirm_order(matched, ticket), label="confirm"):
                logger.error(f"  {train_no} 确认订单失败")
                continue

            # ---- 5. waitForQueue ----
            if self.wait_for_queue():
                logger.info("🎉 🎉 🎉 抢票成功！ 🎉 🎉 🎉")
                if self.sound_alert:
                    self._play_sound()
                self._show_order_info()
                return True
            else:
                logger.warning(f"  {train_no} 排队出票失败")
                self._cancel_order()

        logger.error("所有可用车次均购买失败")
        return False

    def _cancel_order(self):
        """取消未支付订单"""
        if not self.order_id:
            return
        try:
            self.session.post(
                f"{self.base_url}/otn/confirmPassenger/cancelOrder",
                data={
                    "orderId": self.order_id,
                    "REPEAT_SUBMIT_TOKEN": self.repeat_submit_token,
                },
                timeout=10,
            )
            logger.info("已取消订单")
        except Exception:
            pass

    def _show_order_info(self):
        """显示订单信息"""
        if not self.order_id:
            return
        print()
        print("=" * 60)
        logger.info(f"订单号: {self.order_id}")
        logger.info("请尽快登录 12306 官网支付！")
        print("=" * 60)

    # ============================================================
    #  互动模式：你查票选车次点预订，脚本自动下单
    #  (_check_booking / _clear_booking / _auto_confirm 继承自 BaseBot)
    # ============================================================

    def _auto_purchase(self) -> bool:
        """
        用户点【预订】后的自动下单流程：
        getOrderToken → checkPassengers → checkOrderInfo → confirmSingleForQueue
        """
        # 1. 获取订单 token
        if not retry(2, self.get_order_token, label="获取订单token"):
            logger.error("获取订单 token 失败")
            return False

        # 2. 获取并匹配乘车人
        matched = retry(2, self.check_passengers, label="获取乘车人")
        if not matched:
            logger.error("未找到匹配的乘车人")
            return False

        # 3. 预构建乘车人字符串（缓存起来，多个座位类型尝试复用）
        old_passenger_str = self._build_old_passenger_str(matched)

        # 4. 按配置的座位类型优先级尝试
        for seat in self.seat_types:
            if retry(2, lambda s=seat: self._auto_confirm(
                    s, matched, old_passenger_str), label=f"确认{seat}"):
                # 5. 排队等待出票
                if self.wait_for_queue():
                    return True
                else:
                    logger.warning(f"  {seat} 排队出票失败")
                    self._cancel_order()
            time.sleep(0.3)

        logger.error("所有座位类型尝试均失败")
        return False

    def run_interactive(self):
        """
        互动模式主循环：
          你手动查票选车次 → 点【预订】 → 脚本自动完成后续所有操作
        """
        print()
        print("=" * 60)
        print("    12306 互动购票")
        print("=" * 60)
        print(f"  出发站:  {self.from_station}")
        print(f"  到达站:  {self.to_station}")
        print(f"  出发日期: {self.train_date}")
        print(f"  首选车次: {self.preferred_trains or '全部车次'}")
        print(f"  座位类型: {self.seat_types}")
        print(f"  乘车人:   {[p['name'] for p in self.passengers]}")
        print("=" * 60)
        print()

        # 1. 加载车站信息
        if not self.load_station_codes():
            return

        # 2. 登录
        if not self.login():
            return

        # 3. 配置校验
        if not self.validate_config():
            return

        # 4. 提前缓存乘车人
        if self.passengers:
            self.check_passengers(early_check=True)

        # 5. 打开浏览器
        if not self._init_query_driver():
            logger.error("浏览器启动失败")
            return

        try:
            self._navigate_query_page()

            # 打印操作提示
            print()
            print("  " + "╔" + "═" * 56 + "╗")
            print("  ║  👆 请在浏览器中【手动查票】                    ║")
            print("  ║  👆 找到心仪车次后点击【预订】                 ║")
            print("  ║  🤖 脚本自动完成：乘车人选择 → 提交 → 排队   ║")
            print("  ║  ❌ Ctrl+C 随时退出                            ║")
            print("  ╚" + "═" * 56 + "╝")
            print()

            # 6. 监控循环：等待用户点【预订】
            while self.running:
                if self._check_booking():
                    self._sync_browser_cookies_to_session()
                    print()
                    logger.info("🎯 检测到【预订】，开始自动下单...")

                    success = self._auto_purchase()

                    if success:
                        logger.info("🎉 🎉 🎉 抢票成功！ 🎉 🎉 🎉")
                        if self.sound_alert:
                            self._play_sound()
                        self._show_order_info()
                        self._send_notify(
                            "抢票成功",
                            f"{self.from_station}→{self.to_station} "
                            f"{self.train_date} 订单号: {self.order_id}",
                        )
                    else:
                        logger.warning("⛔ 本次下单失败，请重新选择车次")

                    # 清除检测标记，准备下一次
                    self._clear_booking()
                    logger.info("可以再次选择车次点【预订】")
                    print()

                time.sleep(0.2)

        except KeyboardInterrupt:
            print()
            logger.info("用户手动中断")

        finally:
            if self._query_driver:
                try:
                    self._query_driver.quit()
                except Exception:
                    pass
            logger.info("程序结束")

    def run(self):
        """抢票主循环"""
        print()
        print("=" * 60)
        print("    12306 自动抢票脚本")
        print("=" * 60)
        print(f"  出发站: {self.from_station}")
        print(f"  到达站: {self.to_station}")
        print(f"  出发日期: {self.train_date}")
        print(f"  首选车次: {self.preferred_trains or '全部车次'}")
        print(f"  座位类型: {self.seat_types}")
        print(f"  乘车人: {[p['name'] for p in self.passengers]}")
        print(f"  自动提交: {'开启' if self.auto_submit else '关闭 (仅查询)'}")
        print("=" * 60)
        print()

        # 1. 加载车站信息
        if not self.load_station_codes():
            logger.error("加载车站信息失败，程序退出")
            return

        # 2. 登录
        if not self.login():
            logger.error("登录失败，程序退出")
            return

        # 3. 配置校验
        if not self.validate_config():
            return

        # 4. 提前检查乘车人（脚本启动立即验证，避免等到有票了才发现问题）
        if self.passengers:
            self.check_passengers(early_check=True)
        else:
            logger.warning("未配置乘车人，仅查询模式")

        # 5. 开始循环查询
        query_count = 0
        retries = 0
        self.running = True

        logger.info("开始查询余票...")
        logger.info(
            f"自适应轮询: 快速={config.FAST_INTERVAL}s | "
            f"正常={config.QUERY_INTERVAL}s | "
            f"退避≤{config.MAX_INTERVAL}s"
        )

        try:
            while self.running:
                if self.max_retries > 0 and retries >= self.max_retries:
                    logger.info(f"已达最大重试次数 ({self.max_retries}), 停止查询")
                    break

                # 维护时段暂停
                self._check_maintenance_mode()

                query_count += 1
                now = datetime.now().strftime("%H:%M:%S")
                print(f"\n{'='*50}")
                logger.info(f"第 {query_count} 次查询 [{now}]")

                tickets = self.query_tickets()

                found_matching = False

                if tickets:
                    self.display_tickets(tickets)
                    available_tickets = self._find_available_tickets(tickets)
                    found_matching = bool(available_tickets)

                    if available_tickets and self.auto_submit and self.passengers:
                        success = self._attempt_purchase(available_tickets)
                        if success:
                            self._send_notify(
                                "抢票成功",
                                f"{self.from_station}→{self.to_station} "
                                f"{self.train_date} 订单号: {self.order_id}",
                            )
                            break
                        else:
                            logger.info("本轮购买失败，继续轮询等待余票...")
                    elif not self.auto_submit:
                        logger.info("仅查询模式, 不自动提交")
                    elif not self.passengers:
                        logger.warning("无乘车人信息, 无法提交订单")
                    else:
                        logger.info("当前车次无满足座位要求的余票, 继续查询...")
                else:
                    if self._poll_mode != "normal":
                        logger.info("当前无余票")

                retries += 1

                # UA 轮换 (每 30 次)
                self._query_count_since_rotate += 1
                self._query_count_since_session_rotate += 1
                if self._query_count_since_rotate >= 30:
                    self._rotate_user_agent()
                    self._query_count_since_rotate = 0

                # Session 轮换 (每 200 次)
                if self._query_count_since_session_rotate >= 200:
                    self._rotate_session()
                    self._query_count_since_session_rotate = 0

                # 自适应等待
                if self.running:
                    interval = self._get_sleep_interval(
                        found_matching,
                        api_error=self._last_query_has_api_error,
                    )
                    mode_tag = {
                        "fast": "⚡", "backoff": "🐢", "normal": "  ",
                    }.get(self._poll_mode, "  ")
                    logger.debug(
                        f"{mode_tag} 模式={self._poll_mode}, "
                        f"无票计数={self._no_ticket_streak}, "
                        f"等待={interval}s"
                    )
                    time.sleep(interval)

            if not self.running:
                logger.info("用户已停止查询")

        except KeyboardInterrupt:
            print()
            logger.info("用户手动中断")
            self.running = False

        finally:
            if self._query_driver:
                try:
                    self._query_driver.quit()
                except Exception:
                    pass
            logger.info(f"共查询 {query_count} 次, 程序结束")


def main():
    import sys
    if "--interactive" in sys.argv:
        bot = TicketBot()
        bot.run_interactive()
    else:
        bot = TicketBot()
        bot.run()


if __name__ == "__main__":
    main()
