"""
12306 抢票脚本共享基类 - 封装登录、车站查询、车次解析、推送通知等共同逻辑

两个机器人脚本（12306_ticket.py / 12306_sniper.py）共享此基类，
避免代码重复，降低维护成本。

使用方法:
    from common import BaseBot

    class MyBot(BaseBot):
        def __init__(self):
            super().__init__()
            # 子类专属初始化
"""

import logging
import os
import pickle
import re
import time
import urllib.parse
from datetime import datetime, timedelta

import requests

import config

logger = logging.getLogger("12306")


# ==================== 工具函数 ====================

def retry(times, fn, delay=0.2, label=""):
    """
    通用重试工具：执行 fn() 最多 times 次。

    第一次返回真值时停止并返回该值。
    全部失败则返回最后一个值（通常是 None / [] / False）。
    异常被静默吞掉并在下一轮重试。
    """
    last_result = None
    for i in range(times):
        try:
            last_result = fn()
            if last_result:
                return last_result
        except Exception:
            last_result = None
        if i < times - 1:
            if label:
                logger.info(f"  {label} 失败，立即重试...")
            time.sleep(delay)
    return last_result


def is_seat_available(val: str) -> bool:
    """
    判断 12306 余票值是否表示"有票"。

    12306 API 返回格式:
      ""或"*"或"无"  = 无票
      "0"            = 0 张 (无票)
      "1"~"999"      = 有 X 张
      "有"           = 有余票 (不显示具体数量)
    """
    if not val or val in ("无", "", "*", "0"):
        return False
    try:
        return int(val) > 0
    except ValueError:
        return val == "有"


class BaseBot:
    """12306 脚本共享基类，提供登录、车站、解析、推送等基础设施"""

    # 请求头
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Origin": "https://kyfw.12306.cn",
        "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
        "X-Requested-With": "XMLHttpRequest",
    }

    # User-Agent 池，轮换使用降低被识别为脚本的概率
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    ]

    # 座位类型 → 12306 编码
    SEAT_CODE_MAP = {
        "商务座": "9", "特等座": "P", "一等座": "M", "二等座": "O",
        "高级软卧": "6", "软卧": "4", "硬卧": "3",
        "软座": "2", "硬座": "1", "无座": "1", "动卧": "F",
    }

    # XHR 拦截脚本：在页面加载前注入，捕获 leftTicket/query 和 submitOrderRequest 的 AJAX 响应
    _XHR_CAPTURE_SCRIPT = """
        window.__capturedData = null;
        window.__capturedBooking = null;
        (function() {
            var origOpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url) {
                this.__url = (typeof url === 'string') ? url : (url ? url.url : '');
                return origOpen.apply(this, arguments);
            };
            var origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.send = function() {
                this.addEventListener('load', function() {
                    if (this.__url) {
                        try {
                            if (this.__url.indexOf('leftTicket/query') !== -1) {
                                window.__capturedData = this.responseText;
                            }
                            if (this.__url.indexOf('submitOrderRequest') !== -1) {
                                window.__capturedBooking = this.responseText;
                            }
                        } catch(e) {}
                    }
                });
                return origSend.apply(this, arguments);
            };
        })();
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)

        # ── 从 config 读取公共配置 ──
        self.train_date = config.TRAIN_DATE
        self.from_station = config.FROM_STATION
        self.to_station = config.TO_STATION
        self.preferred_trains = config.PREFERRED_TRAINS
        self.seat_types = config.SEAT_TYPES
        self.passengers = config.PASSENGERS
        self.sound_alert = config.SOUND_ALERT

        # ── 车站编码 ──
        self.station_codes = {}
        self.from_station_code = ""
        self.to_station_code = ""

        # ── 登录状态 ──
        self.logged_in = False
        self.username = ""
        self.cookie_file = "12306_cookies.pkl"

        # ── 网络 ──
        self.host = "kyfw.12306.cn"
        self.base_url = "https://kyfw.12306.cn"

        # ── 运行状态 ──
        self.running = True
        self.order_id = ""
        self.repeat_submit_token = ""
        self.key_check = ""
        self.left_ticket_str = ""
        self.train_location = ""
        self._last_query_has_api_error = False
        self._query_driver = None

    # ============================================================
    #  车站信息
    # ============================================================

    def load_station_codes(self) -> bool:
        """从 12306 官方接口加载车站编码映射表"""
        logger.info("正在加载车站信息...")
        url = (
            "https://kyfw.12306.cn/otn/resources/js/framework/"
            "station_name.js?station_version=1.9211"
        )
        try:
            resp = requests.get(url, headers=self.HEADERS, timeout=15)
            resp.encoding = "utf-8"
            pattern = r'@([a-z]+)\|([一-龥]+)\|([A-Z]+)\|'
            matches = re.findall(pattern, resp.text)

            for _, name, code in matches:
                self.station_codes[name] = code

            self.from_station_code = self.station_codes.get(self.from_station, "")
            self.to_station_code = self.station_codes.get(self.to_station, "")

            if not self.from_station_code:
                logger.error(f"未找到出发站: {self.from_station}")
                return False
            if not self.to_station_code:
                logger.error(f"未找到到达站: {self.to_station}")
                return False

            logger.info(
                f"车站加载完成: {self.from_station}({self.from_station_code}) "
                f"-> {self.to_station}({self.to_station_code})"
            )
            return True

        except Exception as e:
            logger.error(f"车站信息加载失败: {e}")
            return False

    def _code_to_station(self, code: str) -> str:
        """根据车站编码获取车站名称"""
        for name, c in self.station_codes.items():
            if c == code:
                return name
        return code

    # ============================================================
    #  登录
    # ============================================================

    def login(self) -> bool:
        """
        手动登录流程:
          1. 尝试加载已保存的 cookies
          2. 失效则打开浏览器让用户扫码登录
          3. 保存 cookies 供后续使用
        """
        logger.info("=" * 50)
        logger.info("登录流程开始")

        # 先尝试使用已保存的 cookies
        if os.path.exists(self.cookie_file):
            logger.info("发现已保存的登录信息，尝试恢复会话...")
            if self._load_cookies_and_verify():
                logger.info(f"[OK] 登录恢复成功！当前用户: {self.username}")
                return True
            else:
                logger.warning("登录信息已过期，需要重新登录")
                os.remove(self.cookie_file)

        logger.info("需要进行手动登录（浏览器扫码）")
        return self._manual_login()

    def _manual_login(self) -> bool:
        """打开浏览器让用户手动扫码登录"""
        try:
            from selenium import webdriver
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
        except ImportError:
            logger.error(
                "需要安装 selenium 才能进行手动登录。\n"
                "请运行: pip install selenium\n"
                "同时需要下载 ChromeDriver 或 EdgeDriver"
            )
            return False

        logger.info("正在打开浏览器进行手动登录...")
        logger.info("请在弹出的浏览器窗口中扫码登录")
        logger.info("提示: 登录成功后 cookies 会被保存，下次可自动恢复")

        driver = None
        try:
            for _, browser_name in [
                ("edge", "Edge"), ("chrome", "Chrome"), ("firefox", "Firefox"),
            ]:
                try:
                    if browser_name == "Edge":
                        from selenium.webdriver.edge.options import Options
                        options = Options()
                        options.add_argument("--disable-blink-features=AutomationControlled")
                        options.add_experimental_option("excludeSwitches", ["enable-automation"])
                        options.add_experimental_option("useAutomationExtension", False)
                        driver = getattr(webdriver, browser_name)(options=options)
                    elif browser_name == "Chrome":
                        from selenium.webdriver.chrome.options import Options
                        options = Options()
                        options.add_argument("--disable-blink-features=AutomationControlled")
                        options.add_experimental_option("excludeSwitches", ["enable-automation"])
                        options.add_experimental_option("useAutomationExtension", False)
                        driver = getattr(webdriver, browser_name)(options=options)
                    elif browser_name == "Firefox":
                        from selenium.webdriver.firefox.options import Options
                        options = Options()
                        driver = getattr(webdriver, browser_name)(options=options)
                    logger.info(f"使用 {browser_name} 浏览器")
                    break
                except Exception:
                    continue

            if driver is None:
                logger.error("未找到可用的浏览器驱动，请安装 ChromeDriver 或 EdgeDriver")
                return False

            # 打开登录页面
            driver.get("https://kyfw.12306.cn/otn/resources/login.html")

            # 等待用户扫码登录 (最多等5分钟)
            wait = WebDriverWait(driver, 300, poll_frequency=1)
            logger.info("请扫码登录... 等待中...")

            try:
                wait.until(EC.url_contains("otn/view/index.html"))
                logger.info("[OK] 检测到登录成功！")

                # 获取 cookies
                selenium_cookies = driver.get_cookies()
                self.session.cookies.clear()
                for cookie in selenium_cookies:
                    if 'sameSite' in cookie:
                        del cookie['sameSite']
                    if cookie.get('httpOnly'):
                        del cookie['httpOnly']
                    self.session.cookies.set(
                        cookie['name'], cookie['value'],
                        domain=cookie.get('domain', ''),
                        path=cookie.get('path', '/'),
                    )

                self._save_cookies()
                self._fetch_username()
                logger.info(f"[OK] 登录成功！欢迎: {self.username}")
                self.logged_in = True
                return True

            except Exception as e:
                logger.error(f"登录超时或失败: {e}")
                return False

        except Exception as e:
            logger.error(f"浏览器操作失败: {e}")
            return False

        finally:
            if driver:
                time.sleep(1)
                driver.quit()

    def _save_cookies(self):
        """保存登录 cookies 到文件"""
        try:
            with open(self.cookie_file, "wb") as f:
                pickle.dump(
                    {
                        "cookies": self.session.cookies.get_dict(),
                        "username": self.username,
                        "timestamp": time.time(),
                    },
                    f,
                )
            logger.info(f"登录信息已保存到 {self.cookie_file}")
        except Exception as e:
            logger.warning(f"保存 cookies 失败: {e}")

    def _load_cookies_and_verify(self) -> bool:
        """加载 cookies 并验证是否有效"""
        try:
            with open(self.cookie_file, "rb") as f:
                data = pickle.load(f)

            # 检查是否过期 (7天, 仅提示不强制)
            saved_time = data.get("timestamp", 0)
            if time.time() - saved_time > 7 * 24 * 3600:
                logger.info("Cookies 已超过7天，可能需要重新登录")

            self.username = data.get("username", "")

            for name, value in data.get("cookies", {}).items():
                self.session.cookies.set(name, value, domain=".12306.cn")

            if self._check_login_status():
                self.logged_in = True
                self._fetch_username()
                return True
            return False

        except Exception as e:
            logger.warning(f"加载 cookies 失败: {e}")
            return False

    def _check_login_status(self) -> bool:
        """验证当前会话是否已登录"""
        url = f"{self.base_url}/otn/login/checkUser"
        params = {"_": int(time.time() * 1000)}
        try:
            resp = self.session.get(url, params=params, timeout=10)
            return resp.json().get("data", {}).get("flag", False)
        except Exception:
            return False

    def _fetch_username(self):
        """获取当前登录用户名"""
        url = f"{self.base_url}/otn/index/initMy12306"
        try:
            resp = self.session.get(url, timeout=10)
            match = re.search(r'"user_name":"([^"]+)"', resp.text)
            if match:
                self.username = match.group(1)
            else:
                match = re.search(r'<a[^>]*id="login_user"[^>]*>([^<]+)<', resp.text)
                self.username = match.group(1).strip() if match else "未知用户"
        except Exception:
            self.username = "未知用户"

    # ============================================================
    #  车站 Cookies
    # ============================================================

    def _set_station_cookies(self):
        """设置车站 cookies (12306 前端需要)"""
        for name, value in [
            ("_jc_save_fromStation",
             urllib.parse.quote(f"{self.from_station},{self.from_station_code}")),
            ("_jc_save_toStation",
             urllib.parse.quote(f"{self.to_station},{self.to_station_code}")),
            ("_jc_save_fromDate", self.train_date),
            ("_jc_save_toDate", self.train_date),
            ("_jc_save_wfdc_flag", "dc"),
        ]:
            self.session.cookies.set(name, value, domain=".12306.cn")

    # ============================================================
    #  车次解析
    # ============================================================

    def _parse_tickets(self, raw_tickets: list) -> list:
        """
        解析 12306 管道分隔的车次数据
        字段索引定义见 config.SEAT_INDICES
        """
        seat_indices = config.SEAT_INDICES

        tickets = []
        for raw in raw_tickets:
            fields = raw.split("|")
            if len(fields) < 35:
                continue
            try:
                ticket = {
                    "secret_str": fields[0],
                    "train_no": fields[3],
                    "from_station": fields[6],
                    "to_station": fields[7],
                    "from_station_name": fields[1] if fields[1] else self._code_to_station(fields[6]),
                    "to_station_name": fields[2] if fields[2] else self._code_to_station(fields[7]),
                    "depart_time": fields[8],
                    "arrive_time": fields[9],
                    "duration": fields[10],
                    "start_station": fields[4],
                    "end_station": fields[5],
                    "date": fields[13],
                }
                ticket["seats"] = {}
                for seat_name, idx in seat_indices.items():
                    if idx < len(fields):
                        value = fields[idx]
                        ticket["seats"][seat_name] = (
                            value if value and value not in ("", "*") else "无"
                        )
                # 打印所有车次原始字段（10~35），排查索引漂移
                logger.info(f"[{ticket['train_no']}] 原始字段[10~35]: "
                           f"{'|'.join(fields[10:36])}")
                tickets.append(ticket)
            except Exception as e:
                logger.debug(f"解析车次数据失败: {e}")
                continue
        return tickets

    # ============================================================
    #  车次排序
    # ============================================================

    def _sort_by_preference(self, tickets: list) -> list:
        """首选车次排前，其余排后"""
        preferred = [t for t in tickets if t["train_no"] in self.preferred_trains]
        others = [t for t in tickets if t["train_no"] not in self.preferred_trains]
        if not preferred:
            logger.info(
                f"首选车次 {self.preferred_trains} 当前均无余票, "
                f"查询到 {len(others)} 个其他车次"
            )
        return preferred + others

    # ============================================================
    #  铺位偏好
    # ============================================================

    def _get_berth_attempts(self, chosen_seat: str) -> list:
        """
        生成铺位尝试列表，按配置优先级排列，最后兜底随机。
        返回 [(choose_seats值, 日志标签), ...]
        """
        base = self.SEAT_CODE_MAP.get(chosen_seat, "O")

        berth_suffixes = {
            "硬卧": {"上铺": "U", "中铺": "M", "下铺": "L"},
            "软卧": {"上铺": "U", "下铺": "L"},
            "高级软卧": {"上铺": "U", "下铺": "L"},
            "动卧": {"上铺": "U", "下铺": "L"},
        }

        if chosen_seat not in berth_suffixes:
            return [(base, chosen_seat)]

        pref_list = getattr(config, "BERTH_PREFERENCE", None) or []
        sleeper_map = berth_suffixes[chosen_seat]

        attempts = []
        for pref in pref_list:
            suffix = sleeper_map.get(pref)
            if suffix:
                attempts.append((base + suffix, f"{chosen_seat}{pref}"))

        attempts.append((base, f"{chosen_seat}随机"))
        return attempts

    # ============================================================
    #  确认订单提交 (共享)
    # ============================================================

    def _confirm_submit(self, confirm_data: dict) -> bool:
        """
        提交 confirmSingleForQueue 请求。
        成功时设置 self.order_id 并返回 True。
        """
        try:
            resp = self.session.post(
                f"{self.base_url}/otn/confirmPassenger/confirmSingleForQueue",
                data=confirm_data, timeout=15,
            )
            result = resp.json()
            if result.get("status") and result.get("data", {}).get("submitStatus"):
                self.order_id = result["data"].get("orderId", "")
                return True
        except Exception:
            pass
        return False

    # ============================================================
    #  乘车人匹配与字符串构建
    # ============================================================

    @staticmethod
    def _passenger_matches(api_passenger: dict, config_passenger: dict) -> bool:
        """
        判断 API 返回的乘车人是否匹配配置中的乘车人。

        12306 API 返回的身份证号已脱敏(如 4290***********01X)，
        因此分两步校验:
        1. 姓名必须完全一致
        2. 身份证号首4位+末4位一致（兼顾完整匹配和脱敏匹配）
        """
        name_ok = (api_passenger.get("passenger_name", "").strip()
                   == config_passenger.get("name", "").strip())
        if not name_ok:
            return False

        api_id = api_passenger.get("passenger_id_no", "").strip()
        cfg_id = config_passenger.get("id_number", "").strip()
        # 先尝试完整匹配（万一 API 某天返回完整号）
        if api_id == cfg_id:
            return True
        # 脱敏匹配: API 返回的形如 4290***********01X,
        # 取出可见部分（去掉 *）后比较首尾
        api_visible = api_id.replace('*', '')
        if len(api_visible) >= 4:
            # 比较可见部分的前几位和后几位是否与完整号对应位置一致
            # 例如 api_visible="429001X" → 前4="4290" ✓, 后几位 cfg_id 以 "01X" 结尾 ✓
            return (api_visible[:4] == cfg_id[:4]
                    and cfg_id.endswith(api_visible[4:]))
        return False

    def _build_passenger_str(self, passengers: list, seat_code: str) -> str:
        """构建乘车人信息字符串: 座位类型|0|姓名|证件类型|证件号|手机号|Y|N|null"""
        parts = []
        for p in passengers:
            parts.append(
                f"{seat_code}|0|{p.get('passenger_name', '')}|"
                f"{p.get('passenger_id_type_code', '')}|{p.get('passenger_id_no', '')}|"
                f"{p.get('mobile_no', '')}|"
                f"{'Y' if p.get('passenger_type', '1') == '1' else 'N'}|N|null"
            )
        return "|".join(parts)

    def _build_old_passenger_str(self, passengers: list) -> str:
        """构建已添加乘车人字符串: 姓名|证件类型|证件号|乘客类型"""
        parts = []
        for p in passengers:
            parts.append(
                f"{p.get('passenger_name', '')}|"
                f"{p.get('passenger_id_type_code', '')}|"
                f"{p.get('passenger_id_no', '')}|"
                f"{p.get('passenger_type', '1')}"
            )
        return "|".join(parts)

    # ============================================================
    #  Selenium 查询浏览器（共享）
    # ============================================================

    def _init_query_driver(self):
        """初始化 Selenium 浏览器（查询专用，与登录的浏览器分开）"""
        if self._query_driver:
            try:
                self._query_driver.current_url  # 探活
                return True
            except Exception:
                try:
                    self._query_driver.quit()
                except Exception:
                    pass
                self._query_driver = None

        from selenium import webdriver

        for browser_name in ["Edge", "Chrome"]:
            try:
                if browser_name == "Edge":
                    from selenium.webdriver.edge.options import Options
                    options = Options()
                    options.add_argument("--disable-blink-features=AutomationControlled")
                    options.add_experimental_option("excludeSwitches", ["enable-automation"])
                    options.add_experimental_option("useAutomationExtension", False)
                    driver = webdriver.Edge(options=options)
                elif browser_name == "Chrome":
                    from selenium.webdriver.chrome.options import Options
                    options = Options()
                    options.add_argument("--disable-blink-features=AutomationControlled")
                    options.add_experimental_option("excludeSwitches", ["enable-automation"])
                    options.add_experimental_option("useAutomationExtension", False)
                    driver = webdriver.Chrome(options=options)

                # 注入 XHR 拦截脚本（在每个新 document 中自动执行）
                driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": self._XHR_CAPTURE_SCRIPT,
                })

                logger.info(f"查询浏览器: {browser_name}")
                self._query_driver = driver
                return True

            except Exception as e:
                logger.debug(f"{browser_name} 浏览器初始化失败: {e}")
                continue

        logger.error("未能启动查询浏览器（请安装 Edge 或 Chrome 驱动）")
        return False

    def _inject_session_cookies_to_browser(self):
        """
        将 requests.Session 中的登录 Cookie 注入 Selenium 浏览器。

        必须在 _navigate_query_page() 之后调用——此时浏览器已在 kyfw.12306.cn 域名上，
        add_cookie() 无需额外导航，不会破坏已加载的页面状态。
        """
        if not self._query_driver or not self.session.cookies:
            return
        # 注意：不要在此处调用 driver.get()，会导航走破坏页面状态！
        for name, value in self.session.cookies.get_dict().items():
            try:
                self._query_driver.add_cookie({
                    'name': name,
                    'value': value,
                    'domain': '.12306.cn',
                })
            except Exception:
                pass

    def _sync_browser_cookies_to_session(self):
        """
        将 Selenium 浏览器的最新 Cookie 同步回 requests.Session。

        用户在浏览器中操作（查票、点预订）后，12306 可能会更新会话 Cookie，
        需要同步回来，后续 API 调用才能使用相同的会话。
        """
        if not self._query_driver:
            return
        try:
            for cookie in self._query_driver.get_cookies():
                self.session.cookies.set(
                    cookie['name'], cookie['value'],
                    domain=cookie.get('domain', '.12306.cn'),
                    path=cookie.get('path', '/'),
                )
        except Exception:
            pass

    def _navigate_query_page(self):
        """导航到 12306 余票查询页面"""
        # 注意: 逗号必须保留作为站名和编码的分隔符，
        # urllib.parse.quote() 默认会编码逗号，需用 safe 参数保留
        fs_text = urllib.parse.quote(
            f"{self.from_station},{self.from_station_code}", safe=',')
        ts_text = urllib.parse.quote(
            f"{self.to_station},{self.to_station_code}", safe=',')
        init_url = (
            f"https://kyfw.12306.cn/otn/leftTicket/init"
            f"?linktypeid=dc&fs={fs_text}&ts={ts_text}"
            f"&date={self.train_date}&flag=N,N,N"
        )

        self._query_driver.get(init_url)

        # 注入登录 Cookie（此时已在 kyfw.12306.cn 域名上）
        self._inject_session_cookies_to_browser()

        # 刷新页面让 Cookie 生效（浏览器重发请求时会带上登录 Cookie）
        try:
            self._query_driver.refresh()
        except Exception:
            pass

    # ============================================================
    #  互动模式共享方法（你点预订，脚本下单）
    # ============================================================

    def _check_booking(self) -> bool:
        """检查用户是否在浏览器中点击了【预订】"""
        try:
            raw = self._query_driver.execute_script(
                "return window.__capturedBooking;"
            )
            return bool(raw)
        except Exception:
            return False

    def _clear_booking(self):
        """清除预订检测标记，准备检测下一次点击"""
        try:
            self._query_driver.execute_script(
                "window.__capturedBooking = null;"
            )
        except Exception:
            pass

    def _auto_confirm(self, seat_type: str, matched: list,
                      old_passenger_str: str) -> bool:
        """
        对指定座位类型完成 checkOrderInfo → confirmSingleForQueue。
        用于互动模式（你点预订后脚本自动下单）。
        """
        seat_code = self.SEAT_CODE_MAP.get(seat_type, "O")
        passenger_str = self._build_passenger_str(matched, seat_code)

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
                logger.info(f"  {seat_type} 订单检查失败: "
                            f"{result.get('messages', '')}")
                return False
        except Exception as e:
            logger.info(f"  {seat_type} 检查异常: {e}")
            return False

        # 按铺位优先级尝试提交
        berth_attempts = self._get_berth_attempts(seat_type)
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

            if self._confirm_submit(confirm_data):
                logger.info(f"  [OK] 提交成功！订单号: {self.order_id} ({label})")
                return True

            logger.info(f"  {label} 提交失败")

        logger.warning(f"  {seat_type} 所有铺位尝试均失败")
        return False

    # ============================================================
    #  推送通知
    # ============================================================

    def _send_notify(self, title: str, content: str):
        """发送推送通知 (ServerChan / PushDeer / Bark)"""
        channel = config.PUSH_NOTIFY
        key = config.PUSH_KEY
        if not channel or not key:
            return

        logger.info(f"发送推送通知 ({channel})...")

        try:
            if channel == "serverchan":
                resp = requests.post(
                    f"https://sctapi.ftqq.com/{key}.send",
                    data={"title": title, "desp": content},
                    timeout=10,
                )
                resp.raise_for_status()
                result = resp.json()
                if result.get("code") != 0:
                    logger.warning(f"ServerChan 推送失败: {result.get('message', '')}")
                else:
                    logger.info("[OK] 推送成功 (ServerChan)")

            elif channel == "pushdeer":
                resp = requests.post(
                    "https://api2.pushdeer.com/message/push",
                    data={"pushkey": key, "text": f"{title}\n{content}"},
                    timeout=10,
                )
                resp.raise_for_status()
                result = resp.json()
                if result.get("code") != 0:
                    logger.warning(f"PushDeer 推送失败: {result.get('error', '')}")
                else:
                    logger.info("[OK] 推送成功 (PushDeer)")

            elif channel == "bark":
                safe_title = urllib.parse.quote(title)
                safe_content = urllib.parse.quote(content)
                resp = requests.get(
                    f"https://api.day.app/{key}/{safe_title}/{safe_content}",
                    timeout=10,
                )
                resp.raise_for_status()
                logger.info("[OK] 推送成功 (Bark)")

            else:
                logger.warning(f"未知推送渠道: {channel} (支持: serverchan, pushdeer, bark)")

        except Exception as e:
            logger.warning(f"推送通知失败: {e}")

    # ============================================================
    #  声音提醒
    # ============================================================

    def _play_sound(self):
        """播放提示音"""
        try:
            import winsound
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                time.sleep(0.5)
        except Exception:
            try:
                print("\a")
            except Exception:
                pass

    # ============================================================
    #  配置校验基方法
    # ============================================================

    def _validate_config_base(self) -> list:
        """
        公共配置校验，返回错误列表。
        子类调用后追加自己的校验，最后通过 _print_config_errors 输出。
        """
        errors = []

        if not self.from_station or not self.to_station:
            errors.append("FROM_STATION / TO_STATION 未配置")
        if self.from_station == self.to_station:
            errors.append("FROM_STATION 与 TO_STATION 相同")

        if not self.seat_types:
            errors.append("SEAT_TYPES 为空，请至少配置一种座位类型")

        # 子类自行判断 PASSENGERS 可否为空
        for p in self.passengers:
            if not p.get("name") or not p.get("id_number"):
                errors.append(f"乘车人信息不完整: {p}")

        try:
            datetime.strptime(self.train_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            errors.append(f"TRAIN_DATE 格式无效: {self.train_date}（应为 YYYY-MM-DD）")

        return errors

    def _print_config_errors(self, errors: list):
        """统一输出配置错误"""
        logger.error("=" * 50)
        logger.error("❌ 配置校验失败，请修复以下问题:")
        for i, err in enumerate(errors, 1):
            logger.error(f"  {i}. {err}")
        logger.error("=" * 50)
