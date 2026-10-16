#!/usr/bin/env python3
"""
12306 放票瞬间抢票工具 (Sniper Mode)
====================================
专为"放票瞬间"场景优化，而非长时间捡漏。

与 12306_ticket.py 的核心区别:
  - 精确计时: 等待到目标放票时间才开始抢
  - 预热机制: 放票前预缓存 token 和乘车人信息，减少下单 RTT
  - 极速提交: 发现余票后跳过非关键接口，直连确认下单
  - 多车次降级: 首选失败后自动尝试下一车次，不重新查询

使用方法:
  1. 在 config.py 的 SNIPER_CONFIG 中设置放票时间
  2. python 12306_sniper.py

提示: 与 12306_ticket.py 共享同个 cookie 文件，登录一次即可
"""

import json
import logging
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

import config
from common import BaseBot, is_seat_available, retry

# ==================== 日志 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("12306_sniper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("sniper")


class SniperBot(BaseBot):
    """12306 放票瞬间抢票主程序"""

    def __init__(self):
        super().__init__()

        # 放票专属配置
        sniper_cfg = config.SNIPER_CONFIG
        self.release_time_str = sniper_cfg["RELEASE_TIME"]
        self.pre_warm_seconds = sniper_cfg.get("PRE_WARM_SECONDS", 30)
        self.sniper_interval = sniper_cfg.get("SNIPER_INTERVAL", 0.2)
        self.max_ticket_attempts = sniper_cfg.get("MAX_TICKET_ATTEMPTS", 10)
        self.fallback_other = sniper_cfg.get("FALLBACK_TO_OTHER_TRAINS", True)

        # 预缓存的乘车人信息
        self.cached_passengers = []
        self.cached_old_passenger_str = ""

    # ============================================================
    #  计时等待: 精确等待到放票时间前的预热时刻
    # ============================================================

    def wait_until_prewarm(self) -> datetime:
        """等待到放票时间的预热点，返回放票时间"""
        now = datetime.now()
        release = datetime.strptime(self.release_time_str, "%H:%M")
        release_time = now.replace(
            hour=release.hour, minute=release.minute, second=0, microsecond=0
        )

        if release_time < now:
            release_time += timedelta(days=1)
            logger.info(f"今日 {self.release_time_str} 已过，等待明天同一时间")

        pre_warm_time = release_time - timedelta(seconds=self.pre_warm_seconds)
        wait_sec = (pre_warm_time - datetime.now()).total_seconds()

        if wait_sec > 0:
            logger.info(
                f"距放票时间 {release_time.strftime('%H:%M:%S')} 还有 {wait_sec:.0f} 秒，"
                f"将于 {pre_warm_time.strftime('%H:%M:%S')} 开始预热"
            )
            while self.running and wait_sec > 0:
                time.sleep(min(10, wait_sec))
                wait_sec = (pre_warm_time - datetime.now()).total_seconds()

        return release_time

    # ============================================================
    #  预热阶段: 放票前持续刷新 token + 缓存乘车人
    # ============================================================

    def _fetch_token(self, timeout=10) -> bool:
        """获取 / 刷新 repeatSubmitToken 及相关字段"""
        try:
            resp = self.session.get(
                f"{self.base_url}/otn/confirmPassenger/initDc", timeout=timeout
            )
            for pattern, attr in [
                (r"globalRepeatSubmitToken\s*=\s*'([^']+)'", "repeat_submit_token"),
                (r"key_check_isChange\s*=\s*'([^']+)'", "key_check"),
                (r"leftTicketStr\s*=\s*'([^']+)'", "left_ticket_str"),
                (r"train_location\s*=\s*'([^']+)'", "train_location"),
            ]:
                m = re.search(pattern, resp.text)
                if m:
                    setattr(self, attr, m.group(1))
            return bool(self.repeat_submit_token)
        except Exception:
            return False

    def _cache_passengers(self) -> bool:
        """匹配并缓存乘车人信息（只需做一次）"""
        if not self.passengers:
            logger.warning("未配置乘车人！无法提交订单")
            return False

        try:
            resp = self.session.get(
                f"{self.base_url}/otn/confirmPassenger/getPassengerDTOs",
                params={"_": int(time.time() * 1000)},
                timeout=10,
            )
            data = resp.json()
            if not data.get("status"):
                logger.warning("获取乘车人失败，放票时重试")
                # 返回 False 但不清空已有缓存
                return bool(self.cached_passengers)

            all_passengers = (data.get("data") or {}).get("normal_passengers") or []
            logger.info(f"  查询到 {len(all_passengers)} 位常用联系人")
            for ap in all_passengers:
                logger.debug(f"  API联系人: {ap.get('passenger_name')} / {ap.get('passenger_id_no')} / 核验: {ap.get('passenger_id_type_name', '?')}")

            # 匹配前备份旧缓存，失败时还原
            old_cache = self.cached_passengers.copy() if self.cached_passengers else []
            self.cached_passengers = []
            for cfg_p in self.passengers:
                for api_p in all_passengers:
                    if self._passenger_matches(api_p, cfg_p):
                        self.cached_passengers.append(api_p)
                        logger.info(f"  [OK] 匹配乘车人: {api_p.get('passenger_name', '?')}")
                        break
                else:
                    logger.warning(f"  未在常用联系人中找到: {cfg_p.get('name')}")

            if not self.cached_passengers:
                logger.error("无匹配的乘车人，无法下单")
                self.cached_passengers = old_cache  # 还原旧缓存
                return False

            self.cached_old_passenger_str = self._build_old_passenger_str(
                self.cached_passengers
            )
            logger.info(f"[OK] 预构建乘车人字符串完成 ({len(self.cached_passengers)} 人)")
            return True

        except Exception as e:
            logger.error(f"预缓存乘车人异常: {e}")
            # 不清空已有缓存
            return False

    # ============================================================
    #  预热阶段: 放票前持续刷新 token + 缓存乘车人 + 预热浏览器
    # ============================================================

    def pre_warm(self, release_time: datetime):
        """
        放票预热：首次获取 token + 乘车人信息后，
        持续刷新 token 直到距放票只剩 3 秒。

        期间同时初始化查询浏览器并加载查票页面，
        并触发预热查询使 AJAX 缓存 / CDN 连接保持热状态。
        """
        logger.info("=" * 50)
        logger.info("🔥 预热阶段")

        if self._fetch_token():
            logger.info("[OK] 首次 token 获取成功")
        else:
            logger.warning("首次 token 获取失败，放票时会重试")

        # 如果还没有缓存乘车人（如下次启动直接到预热阶段），补查一次
        if not self.cached_passengers:
            self._cache_passengers()

        # 预热浏览器（加载查票页面，后续狙击只需点击查询按钮触发 AJAX）
        logger.info("正在加载查询浏览器...")
        if self._init_query_driver():
            self._navigate_query_page()

            # 等待自动查询完成 + 触发一次预热查询（让 AJAX 端点 / CDN 热起来）
            try:
                from selenium.webdriver.support.ui import WebDriverWait
                WebDriverWait(self._query_driver, 10, poll_frequency=0.1).until(
                    lambda d: d.execute_script("return window.__capturedData;")
                )
                self._query_driver.execute_script("window.__capturedData = null;")
                # 直接调用 searchTicket() 跳过按钮查找开销
                self._query_driver.execute_script("searchTicket()")
                logger.info("[OK] 查询浏览器已就绪（AJAX 预热完成）")
            except Exception:
                logger.warning("查询浏览器预热未完全完成，狙击时会自动补充")
        else:
            logger.warning("查询浏览器加载失败，狙击阶段将使用备用方案")

        if self.repeat_submit_token:
            logger.info("持续刷新 token（每 5 秒），保持新鲜直至放票...")

        while self.running:
            now = datetime.now()
            sec_to_release = (release_time - now).total_seconds()

            if sec_to_release <= 3:
                if 0 < sec_to_release <= 3:
                    logger.info(f"⏰ 距放票 {sec_to_release:.0f} 秒，预热结束")
                break

            if sec_to_release <= 10:
                logger.info(f"⏰ 距放票 {sec_to_release:.0f} 秒...")

            sleep_sec = min(5, sec_to_release - 3)
            time.sleep(sleep_sec)

            if not self._fetch_token():
                logger.debug("token 刷新失败，下次重试")

        logger.info("预热阶段结束，准备放票")

    # ============================================================
    #  狙击阶段: 高速轮询 + 极速下单
    # ============================================================

    def snipe(self, release_time: datetime):
        """放票时刻执行抢票"""
        now = datetime.now()
        wait_sec = (release_time - now).total_seconds()

        if wait_sec > 0:
            logger.info(f"⏰ 距放票还有 {wait_sec:.1f} 秒，进入待命状态")
            time.sleep(wait_sec)

        logger.info("🚀🚀🚀 放票时间到！开始抢票 🚀🚀🚀")
        logger.info(
            f"轮询间隔: {self.sniper_interval}s/次 | "
            f"最大尝试: {self.max_ticket_attempts} 轮"
        )

        tried_trains = set()
        last_print_time = 0

        for attempt in range(1, self.max_ticket_attempts + 1):
            if not self.running:
                break

            now_ts = time.time()
            if now_ts - last_print_time > 2:
                logger.info(f"⚡ 第 {attempt} 轮抢票中...")
                last_print_time = now_ts

            tickets = self._query_fast()

            if tickets is None:
                self._last_query_has_api_error = True
                time.sleep(self.sniper_interval)
                continue
            self._last_query_has_api_error = False

            if not tickets:
                time.sleep(self.sniper_interval)
                continue

            tickets = self._sort_by_preference(tickets)

            for ticket in tickets:
                train_no = ticket["train_no"]
                if train_no in tried_trains:
                    continue
                if not self._has_available_seat(ticket):
                    continue

                tried_trains.add(train_no)
                logger.info(
                    f"🎯 尝试提交: {train_no} "
                    f"{ticket['depart_time']}-{ticket['arrive_time']}"
                )

                if self._rapid_submit(ticket):
                    self._on_success()
                    return True

                logger.info(f"  {train_no} 提交失败，切换下一车次")

            if not self.fallback_other and all(
                t["train_no"] in tried_trains
                for t in tickets
                if t["train_no"] in self.preferred_trains
            ):
                logger.info("首选车次全部尝试完毕，不再尝试其他车次")
                break

            time.sleep(self.sniper_interval)

        logger.warning("❌ 抢票失败，已达最大尝试轮次")
        return False

    def _query_fast(self) -> list:
        """
        通过 Selenium 浏览器高速查询余票。

        预热阶段已加载查票页面，狙击时直接调用 searchTicket() 触发 AJAX，
        绕过 WAF 拦截同时保持最低延迟。
        Returns: 车次列表 / [] (无票) / None (API 异常)
        """
        if not self._query_driver:
            logger.warning("查询浏览器不可用，尝试重新启动...")
            if not self._init_query_driver():
                return None
            try:
                self._navigate_query_page()
                logger.info("查询浏览器已自动恢复")
            except Exception as e:
                logger.debug(f"浏览器恢复失败: {e}")
                return None

        driver = self._query_driver

        try:
            # 通过浏览器内 fetch() 直接调 API（绕开 searchTicket 不可用的问题）
            cache_buster = int(time.time() * 1000)
            params = (
                f"leftTicketDTO.train_date={self.train_date}"
                f"&leftTicketDTO.from_station={self.from_station_code}"
                f"&leftTicketDTO.to_station={self.to_station_code}"
                f"&purpose_codes=ADULT&_{cache_buster}"
            )
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

            # JS 侧异步等待：30ms 轮询，数据一到立即回调
            raw = driver.execute_async_script("""
                var callback = arguments[arguments.length - 1];
                if (window.__capturedData) {
                    callback(window.__capturedData);
                    return;
                }
                var waited = 0;
                (function poll() {
                    if (window.__capturedData) {
                        callback(window.__capturedData);
                    } else if (waited >= 5000) {
                        callback(null);
                    } else {
                        waited += 30;
                        setTimeout(poll, 30);
                    }
                })();
            """)

            if raw is None:
                return None  # 超时 = API 异常，非无票

            data = json.loads(raw)
            if not data.get("status"):
                return None
            raw_tickets = data.get("data", {}).get("result", [])
            return self._parse_tickets(raw_tickets) if raw_tickets else []

        except Exception as e:
            logger.debug(f"Selenium 查询异常: {e}")
            try:
                driver.quit()
            except Exception:
                pass
            self._query_driver = None
            # 尝试恢复驱动（为下一轮准备）
            try:
                if self._init_query_driver():
                    self._navigate_query_page()
                    logger.info("查询浏览器已自动恢复")
            except Exception:
                pass
            return None

    def _has_available_seat(self, ticket: dict) -> bool:
        """检查车次是否有匹配的座位类型"""
        for seat in self.seat_types:
            val = ticket["seats"].get(seat, "无")
            if is_seat_available(val):
                return True
        return False

    # ============================================================
    #  极速下单: 跳过非关键步骤，用预缓存数据直连确认
    # ============================================================

    def _refresh_init_dc(self) -> bool:
        """重新获取 initDc 页面上的 repeatSubmitToken 等字段（委托给 _fetch_token）"""
        return self._fetch_token(timeout=8)

    def _rapid_submit(self, ticket: dict) -> bool:
        """
        极速提交流程（每步最多重试一次，防网络抖动）

        1. submitOrderRequest  — 提交订单（必要）
        2. initDc              — 刷新 token（pre-warm 的可能已过期）
        3. checkOrderInfo      — 验证订单信息
        4. confirmSingleForQueue — 确认排队

        相比原脚本: 跳过 getPassengerDTOs（已预热缓存），直接拼接字符串
        """
        if not self.cached_passengers:
            logger.error("无乘车人信息，无法提交")
            return False

        # 选择座位类型
        chosen_seat = None
        for seat in self.seat_types:
            val = ticket["seats"].get(seat, "无")
            if is_seat_available(val):
                chosen_seat = seat
                break
        if not chosen_seat:
            return False

        seat_code = self.SEAT_CODE_MAP.get(chosen_seat, "O")
        secret = urllib.parse.unquote(ticket.get("secret_str", ""))

        # ---- 1. submitOrderRequest (重试 ×1) ----
        if not retry(2, lambda: self.session.post(
            f"{self.base_url}/otn/leftTicket/submitOrderRequest",
            data={
                "secretStr": secret,
                "train_date": ticket["date"],
                "back_train_date": ticket["date"],
                "tour_flag": "dc",
                "purpose_codes": "ADULT",
                "query_from_station_name": ticket["from_station_name"],
                "query_to_station_name": ticket["to_station_name"],
                "undefined": "",
            },
            timeout=8,
        ).json().get("status"), label="submitOrderRequest"):
            logger.warning("submitOrderRequest 失败")
            return False
        logger.debug("  submitOrderRequest OK")

        # ---- 2. initDc (刷新 token，重试 ×1) ----
        if not retry(2, self._refresh_init_dc, label="initDc"):
            logger.warning("initDc 失败")
            return False
        logger.debug("  initDc OK")

        # ---- 构建乘车人字符串 ----
        passenger_ticket_str = self._build_passenger_str(
            self.cached_passengers, seat_code
        )

        # ---- 3. checkOrderInfo (重试 ×1) ----
        check_data = {
            "cancel_flag": "2",
            "bed_level_order_num": "000000000000000000000000000000",
            "passengerTicketStr": passenger_ticket_str,
            "oldPassengerStr": self.cached_old_passenger_str,
            "tour_flag": "dc",
            "randCode": "",
            "whatsSelect": "1",
            "sessionId": "",
            "sig": "",
            "REPEAT_SUBMIT_TOKEN": self.repeat_submit_token,
        }
        if not retry(2, lambda: self.session.post(
            f"{self.base_url}/otn/confirmPassenger/checkOrderInfo",
            data=check_data, timeout=8,
        ).json().get("status"), label="checkOrderInfo"):
            logger.warning("checkOrderInfo 失败")
            return False
        logger.debug("  checkOrderInfo OK")

        # ---- 4. confirmSingleForQueue (铺位偏好尝试 × 重试1次) ----
        berth_attempts = self._get_berth_attempts(chosen_seat)

        for attempt_idx, (try_code, label) in enumerate(berth_attempts):
            if attempt_idx > 0:
                logger.info(f"  ← {label} 失败，尝试下一优先级")

            confirm_data = {
                "passengerTicketStr": passenger_ticket_str,
                "oldPassengerStr": self.cached_old_passenger_str,
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

            if retry(2, lambda: self._confirm_submit(confirm_data), label=label):
                logger.info(f"[OK] 订单提交成功！订单号: {self.order_id} ({label})")
                return True
            else:
                logger.info(f"  {label} 确认失败")

        logger.warning("confirmSingleForQueue 失败（所有铺位尝试均失败）")
        return False

    # ============================================================
    #  成功处理
    # ============================================================

    def _on_success(self):
        """抢票成功后的处理"""
        logger.info("")
        logger.info("🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉")
        logger.info("🎉     抢 票 成 功 ！    🎉")
        logger.info("🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉")
        logger.info(f"  订单号: {self.order_id}")
        logger.info(f"  {self.from_station} -> {self.to_station}")
        logger.info(f"  日期: {self.train_date}")
        logger.info("  请尽快登录 12306 官网完成支付（45分钟内）")
        logger.info("")

        self._send_notify(
            "抢票成功",
            f"{self.from_station}→{self.to_station} "
            f"{self.train_date} 订单号: {self.order_id}",
        )

        if self.sound_alert:
            self._play_sound()

    # ============================================================
    #  配置校验 (覆盖基类)
    # ============================================================

    def validate_config(self) -> bool:
        """启动前校验配置"""
        errors = self._validate_config_base()

        if not self.passengers:
            errors.append("PASSENGERS 为空，无法下单")

        try:
            datetime.strptime(self.release_time_str, "%H:%M")
        except (ValueError, TypeError):
            errors.append(f"RELEASE_TIME 格式无效: {self.release_time_str}")

        if not errors:
            return True

        self._print_config_errors(errors)
        return False

    # ============================================================
    #  维护时段检测
    # ============================================================

    def _check_maintenance_mode(self) -> bool:
        """检查是否在 12306 维护时段 (23:00~06:00)，是则退出"""
        now = datetime.now()
        hour = now.hour
        if hour >= 23 or hour < 6:
            logger.info("当前为 12306 维护时段 (23:00-06:00)，狙击脚本退出")
            logger.info("请等到 06:00 后再运行")
            sys.exit(0)
        return False

    # ============================================================
    #  主入口
    # ============================================================

    def run(self):
        """主流程"""
        print()
        print("=" * 60)
        print("    12306 放票瞬间抢票工具 · Sniper Mode")
        print("=" * 60)
        print(f"  出发:   {self.from_station} → {self.to_station}")
        print(f"  日期:   {self.train_date}")
        print(f"  放票:   {self.release_time_str}")
        print(f"  预热:   提前 {self.pre_warm_seconds}s")
        print(f"  轮询:   {self.sniper_interval}s/次")
        print(f"  车次:   {self.preferred_trains or '全部'}")
        print(f"  座位:   {self.seat_types}")
        print(f"  乘客:   {[p['name'] for p in self.passengers] or '未配置'}")
        print(f"  降级:   {'开启' if self.fallback_other else '关闭'}")
        print("=" * 60)
        print()

        # 1. 配置校验
        if not self.validate_config():
            return

        # 2. 检查维护时段
        self._check_maintenance_mode()

        # 3. 加载车站
        if not self.load_station_codes():
            return

        # 4. 登录
        if not self.login():
            return

        # 5. 提前检查乘车人（脚本启动立即验证，避免等到预热才发现问题）
        if self.passengers:
            if not self._cache_passengers():
                logger.error("乘车人匹配失败，将无法提交订单")
                # 不退出，让用户自行决定是否继续等待放票
        else:
            logger.warning("未配置乘车人，仅查询模式")

        # 6. 等待预热时刻
        release_time = self.wait_until_prewarm()

        # 6. 预热
        self.pre_warm(release_time)

        # 7. 狙击放票
        self.snipe(release_time)


    # ============================================================
    #  互动模式：你查票选车次点预订，脚本极速下单
    # ============================================================

    def run_interactive(self):
        """
        互动模式：你手动查票选车次 → 点【预订】 → 脚本极速下单。
        适合放票时刻：Token 持续刷新，点击预订瞬间提交。
        """
        print()
        print("=" * 60)
        print("    12306 Sniper · 互动模式")
        print("=" * 60)
        print(f"  出发:   {self.from_station} → {self.to_station}")
        print(f"  日期:   {self.train_date}")
        print(f"  车次:   {self.preferred_trains or '全部'}")
        print(f"  座位:   {self.seat_types}")
        print(f"  乘客:   {[p['name'] for p in self.passengers] or '未配置'}")
        print("=" * 60)
        print()

        # 1. 配置校验
        if not self.validate_config():
            return

        # 2. 检查维护时段
        self._check_maintenance_mode()

        # 3. 加载车站
        if not self.load_station_codes():
            return

        # 4. 登录
        if not self.login():
            return

        # 5. 缓存乘车人
        if self.passengers:
            if not self._cache_passengers():
                logger.error("乘车人匹配失败，将无法提交订单")
        else:
            logger.warning("未配置乘车人，仅查询模式")

        # 6. 打开浏览器
        if not self._init_query_driver():
            logger.error("浏览器启动失败")
            return

        self._navigate_query_page()

        print()
        print("  " + "╔" + "═" * 56 + "╗")
        print("  ║  👆 请在浏览器中【手动查票】                    ║")
        print("  ║  👆 找到车次后点击【预订】                     ║")
        print("  ║  🤖 Token 持续刷新中，点预订瞬间提交           ║")
        print("  ║  ❌ Ctrl+C 随时退出                            ║")
        print("  ╚" + "═" * 56 + "╝")
        print()

        # 7. 先预热一次 token
        self._fetch_token()

        # 8. 监控循环（100ms 轮询，每 5 秒后台刷新 token）
        last_token_refresh = time.time()
        first_booking_cleared = False
        try:
            # 用户可能已经提前打开了预订界面，
            # 先清掉启动时的陈旧信号，避免误触发
            self._clear_booking()
            first_booking_cleared = True

            while self.running:
                now = time.time()

                # 每 5 秒刷新一次 token，始终保鲜
                if now - last_token_refresh > 5:
                    self._fetch_token()
                    last_token_refresh = now

                # 检测用户是否点击了【预订】
                if self._check_booking():
                    self._sync_browser_cookies_to_session()
                    print()
                    logger.info("🎯 检测到【预订】，极速下单中...")

                    # 跳过 fetch_token（后台一直在刷新，就是最新的）
                    # 直接进入确认流程

                    if self.cached_passengers:
                        matched = self.cached_passengers
                        old_str = self.cached_old_passenger_str

                        success = False
                        for seat in self.seat_types:
                            if self._auto_confirm(seat, matched, old_str):
                                success = True
                                break

                        if success:
                            logger.info("🎉 🎉 🎉 抢票成功！🎉 🎉 🎉")
                            self._on_success()
                        else:
                            logger.warning("⛔ 下单失败，请重新选择车次")
                    else:
                        logger.error("无乘车人信息，无法下单")

                    self._clear_booking()
                    if success:
                        logger.info("订单已提交，请登录 12306 完成支付")
                        break
                    logger.info("可以再次选择车次点【预订】")
                    print()

                time.sleep(0.1)  # 100ms 高速轮询，点预订瞬间响应

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


def main():
    import sys as _sys
    if "--interactive" in _sys.argv:
        bot = SniperBot()
        try:
            bot.run_interactive()
        except KeyboardInterrupt:
            print()
            logger.info("用户手动中断")
        finally:
            if bot._query_driver:
                try:
                    bot._query_driver.quit()
                except Exception:
                    pass
    else:
        bot = SniperBot()
        try:
            bot.run()
        except KeyboardInterrupt:
            print()
            logger.info("用户手动中断")
            bot.running = False
        finally:
            if bot._query_driver:
                try:
                    bot._query_driver.quit()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
