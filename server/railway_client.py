"""
RailwayClient — 12306 纯 HTTP 协议客户端（无 Selenium）

协议以 2026-09 实测可用的开源实现为参照，并针对本项目 2026-06 的失败日志
做了修正（缺 getQueueCount / initDc 方法错误 / 乘车人字符串格式过期 /
查询端点硬编码过期 / 座位索引错列）。

安全语义（防重复下单）：
  - confirmSingleForQueue 发出之后，任何歧义结果（超时/非JSON/结构异常）
    → OrderResultUnknown，必须人工去 12306 核对订单
  - 只有服务端明确的 status=false / submitStatus=false 才是 OrderRejected，
    可以安全换候选重试
  - dry_run=True 时走到 getQueueCount 成功即返回，不真正确认排队
"""

import base64
import logging
import pickle
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from . import protocol as P
from .paths import WEB_SESSION_FILE

logger = logging.getLogger("web12306")

CST = timezone(timedelta(hours=8))


# ==================== 异常与结果类型 ====================

class RailwayError(Exception):
    """基础异常"""


class NotLoggedInError(RailwayError):
    """会话未登录/已失效"""


class QueryError(RailwayError):
    """查询失败（可重试，不涉及订单）"""


class OrderRejected(RailwayError):
    """服务端明确拒绝（未创建订单，可安全换候选重试）"""


class OrderResultUnknown(RailwayError):
    """结果未知（可能已创建订单！必须人工核对，禁止自动重试）"""


@dataclass
class BookResult:
    status: str            # success | rejected | unknown | dry_run_ok
    stage: str = ""        # 失败/停止时所处阶段
    order_id: str = ""
    message: str = ""
    queue_info: Dict[str, Any] = field(default_factory=dict)


# ==================== 客户端 ====================

class RailwayClient:
    def __init__(self, cookie_file: str = None, timeout: int = 10):
        self.timeout = timeout
        self.cookie_file = str(cookie_file or WEB_SESSION_FILE)
        self.session = requests.Session()
        self.session.headers.update(P.HEADERS)
        self.username = ""
        self._query_endpoint = ""          # 动态发现的查询端点，如 queryG
        self._endpoint_lock = threading.Lock()
        self._stations: Dict[str, str] = {}
        self._clock_offset = 0.0           # 服务器时间 - 本地时间（秒）
        self.clock_rtt = -1.0
        self._login_ok = False             # 登录态缓存（削减关键路径 RTT）
        self._login_checked_at = 0.0
        self._pax_cache: Tuple[float, str, List[Dict[str, Any]]] = (0.0, "", [])
        self._load_cookies()

    # ------------------------------------------------------------
    #  会话与 Cookie
    # ------------------------------------------------------------

    def _load_cookies(self):
        try:
            with open(self.cookie_file, "rb") as f:
                data = pickle.load(f)
            for name, value in data.get("cookies", {}).items():
                self.session.cookies.set(name, value, domain=".12306.cn")
            self.username = data.get("username", "")
            logger.debug(f"已从 {self.cookie_file} 恢复会话")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"加载会话文件失败: {e}")

    def _save_cookies(self):
        try:
            with open(self.cookie_file, "wb") as f:
                pickle.dump({
                    "cookies": self.session.cookies.get_dict(),
                    "username": self.username,
                    "timestamp": time.time(),
                }, f)
        except Exception as e:
            logger.warning(f"保存会话失败: {e}")

    def prime(self):
        """预热会话：取基础 cookie，否则 WAF 对 API 请求返回 HTML 错误页"""
        for url in (
            f"{P.BASE_URL}/otn/login/conf",
            f"{P.BASE_URL}/otn/index12306/getLoginBanner",
            f"{P.BASE_URL}/passport/web/auth/uamtk-static",
            f"{P.BASE_URL}/otn/leftTicket/init",
        ):
            try:
                self.session.get(url, timeout=self.timeout)
            except Exception:
                pass

    def check_login(self, max_age: float = 0.0) -> bool:
        """
        POST /otn/login/checkUser → data.flag。

        max_age>0 时启用缓存：距上次成功检查不足 max_age 秒则直接返回缓存值，
        省去关键路径（狙击爆发前）的一次 RTT。缓存只对"已登录"生效——
        一旦真实检查失败立即失效，避免用旧登录态误判。
        """
        now = time.time()
        if max_age and self._login_ok and (now - self._login_checked_at) < max_age:
            return True
        try:
            resp = self.session.post(
                f"{P.BASE_URL}/otn/login/checkUser",
                data={"_json_att": ""}, timeout=self.timeout,
            )
            payload = resp.json()
            ok = (isinstance(payload, dict) and payload.get("status") is True
                  and (payload.get("data") or {}).get("flag") is True)
        except Exception:
            ok = False
        self._login_ok = ok
        self._login_checked_at = time.time()
        return ok

    def warm_connection(self):
        """
        狙击爆发前预热：对查询端点发一次轻量请求，确保 TCP+TLS 连接处于
        keep-alive 热状态，避免放票瞬间首个请求承担握手延迟。
        """
        try:
            with self._endpoint_lock:
                endpoint = self._query_endpoint or "query"
            # 用一个必然快速的探测（今日已过日期→服务端秒回空/错误，但连接被焐热）
            self.session.get(
                f"{P.BASE_URL}/otn/leftTicket/{endpoint}",
                params={
                    "leftTicketDTO.train_date": "2000-01-01",
                    "leftTicketDTO.from_station": "BJP",
                    "leftTicketDTO.to_station": "SHH",
                    "purpose_codes": "ADULT",
                },
                timeout=5,
            )
        except Exception:
            pass

    def fetch_username(self) -> str:
        """尽力获取用户名（登录后）"""
        try:
            resp = self.session.get(
                f"{P.BASE_URL}/otn/index/initMy12306", timeout=self.timeout)
            m = re.search(r'"user_name":"([^"]+)"', resp.text)
            if m:
                self.username = m.group(1)
                self._login_ok = True
                self._login_checked_at = time.time()
        except Exception:
            pass
        return self.username or "已登录用户"

    # ------------------------------------------------------------
    #  二维码登录（纯 API，无浏览器）
    # ------------------------------------------------------------

    def _post_json_safe(self, url: str, data: Dict[str, Any], step: str,
                        ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        POST 并安全解析 JSON。返回 (payload|None, 诊断串)。
        诊断串在失败时描述 HTTP 状态 / 302→error.html / 非 JSON 等，便于定位。
        """
        try:
            resp = self.session.post(url, data=data, timeout=self.timeout,
                                     allow_redirects=False)
        except requests.RequestException as e:
            return None, f"{step} 网络异常 {type(e).__name__}"
        code = resp.status_code
        loc = resp.headers.get("Location", "")
        if code in (301, 302, 303, 307, 308):
            tag = "→error.html(WAF/未授权)" if "error.html" in loc else f"→{loc[:60]}"
            return None, f"{step} 被重定向 {code} {tag}"
        if not (200 <= code < 300):
            return None, f"{step} HTTP {code}"
        ctype = resp.headers.get("Content-Type", "")
        text = resp.text.lstrip()
        if text.startswith("<") or "html" in ctype.lower():
            return None, f"{step} 返回 HTML（疑似 WAF 拦截，会话 cookie 可能不足）"
        try:
            payload = resp.json()
        except ValueError:
            return None, f"{step} 响应非 JSON: {text[:60]!r}"
        if not isinstance(payload, dict):
            return None, f"{step} 响应非对象"
        return payload, ""

    def create_qr(self) -> Tuple[str, str]:
        """返回 (base64 png, uuid)"""
        self.prime()
        payload, diag = self._post_json_safe(
            f"{P.BASE_URL}/passport/web/create-qr64", {"appid": "otn"}, "create-qr64")
        if payload is None:
            logger.error(f"获取二维码失败: {diag}")
            raise RailwayError(f"获取二维码失败: {diag}")
        if str(payload.get("result_code")) != "0":
            msg = payload.get("result_message") or payload
            logger.error(f"获取二维码被拒: {msg}")
            raise RailwayError(f"获取二维码失败: {msg}")
        image, uuid_ = payload.get("image"), payload.get("uuid")
        if not image or not uuid_:
            raise RailwayError("12306 未返回二维码图片或 UUID")
        base64.b64decode(image, validate=True)  # 校验合法性
        logger.info(f"二维码已生成 uuid={str(uuid_)[:12]}...")
        return image, str(uuid_)

    def check_qr(self, uuid_: str) -> Tuple[str, str]:
        """
        轮询扫码状态。result_code:
          0=待扫码  1=已扫码待手机确认  2=已确认(可完成登录)  3=二维码过期
        """
        payload, diag = self._post_json_safe(
            f"{P.BASE_URL}/passport/web/checkqr",
            {"uuid": uuid_, "appid": "otn"}, "checkqr")
        if payload is None:
            return "", diag          # 前端按 error 处理
        return (str(payload.get("result_code", "")),
                str(payload.get("result_message", "")))

    def complete_login(self) -> Tuple[bool, str]:
        """
        扫码确认后完成登录: uamtk → uamauthclient。
        uamtk 认证 cookie 可能有传播延迟，失败时重试数次。
        """
        token = ""
        last_diag = ""
        for attempt in range(4):
            payload, diag = self._post_json_safe(
                f"{P.BASE_URL}/passport/web/auth/uamtk", {"appid": "otn"}, "uamtk")
            if payload is not None and str(payload.get("result_code")) == "0":
                token = payload.get("newapptk") or ""
                if token:
                    break
                last_diag = "uamtk 未返回 newapptk"
            else:
                last_diag = diag or (
                    f"uamtk result_code={payload.get('result_code')} "
                    f"{payload.get('result_message')}" if payload else diag)
            logger.debug(f"uamtk 第{attempt+1}次未就绪: {last_diag}")
            time.sleep(0.6)
        if not token:
            logger.error(f"uamtk 获取 token 失败: {last_diag}")
            return False, f"获取登录令牌失败: {last_diag}"

        payload, diag = self._post_json_safe(
            f"{P.BASE_URL}/otn/uamauthclient", {"tk": token}, "uamauthclient")
        if payload is None:
            logger.error(f"uamauthclient 失败: {diag}")
            return False, f"登录校验失败: {diag}"
        if str(payload.get("result_code")) == "0":
            self.username = str(payload.get("username") or "")
            self._login_ok = True
            self._login_checked_at = time.time()
            self._save_cookies()
            logger.info(f"登录成功: {self.username}")
            return True, self.username or "OK"
        msg = str(payload.get("result_message") or payload)
        logger.error(f"uamauthclient 被拒: {msg}")
        return False, msg


    # ------------------------------------------------------------
    #  服务器校时
    # ------------------------------------------------------------

    def sync_clock(self, samples: int = 7) -> float:
        """
        采样服务器 Date 响应头估算钟差（服务器时间 - 本地时间，秒）。

        算法：
          - 用轻量端点 /otn/login/conf（小 JSON），避免大页面下载时间污染 RTT
          - 首个样本视为 TCP/TLS 握手预热，丢弃
          - 取 RTT 最低的样本：RTT 越低路径越对称，offset 估算越准
          - Date 头精度为秒，用 "响应时刻 - RTT/2" 近似服务器发响应瞬间
        """
        url = f"{P.BASE_URL}/otn/login/conf"
        best_rtt, best_offset = None, 0.0
        for i in range(samples + 1):          # +1: 首个样本做握手预热
            try:
                t0 = time.time()
                resp = self.session.get(url, timeout=5, stream=True)
                t1 = time.time()
                resp.close()
                if i == 0:
                    continue                   # 丢弃握手样本
                date_hdr = resp.headers.get("Date")
                if not date_hdr:
                    continue
                server_ts = parsedate_to_datetime(date_hdr).timestamp()
                rtt = t1 - t0
                offset = server_ts + rtt / 2 - t1
                if best_rtt is None or rtt < best_rtt:
                    best_rtt, best_offset = rtt, offset
            except Exception:
                continue
        if best_rtt is not None:
            self._clock_offset = best_offset
            self.clock_rtt = best_rtt
            logger.info(
                f"服务器校时完成: 钟差 {best_offset:+.2f}s (最低RTT {best_rtt*1000:.0f}ms)")
        else:
            logger.warning("服务器校时失败，使用本地时间")
        return self._clock_offset

    def server_now(self) -> datetime:
        return datetime.now().astimezone() + timedelta(seconds=self._clock_offset)

    # ------------------------------------------------------------
    #  车站
    # ------------------------------------------------------------

    def load_stations(self, force: bool = False) -> Dict[str, str]:
        """站名 → 三字码（缓存）"""
        if self._stations and not force:
            return self._stations
        url = ("https://kyfw.12306.cn/otn/resources/js/framework/"
               "station_name.js?station_version=1.9211")
        try:
            resp = requests.get(url, headers=P.HEADERS, timeout=15)
            resp.encoding = "utf-8"
            text = resp.text
        except requests.RequestException as e:
            raise QueryError(f"车站数据下载失败: {type(e).__name__}")
        stations = {}
        for _, name, code in re.findall(r'@([a-z]+)\|([一-龥]+)\|([A-Z]+)\|', text):
            stations[name] = code
        if not stations:
            raise QueryError("车站数据解析为空")
        self._stations = stations
        return stations

    # ------------------------------------------------------------
    #  余票查询（端点动态发现）
    # ------------------------------------------------------------

    def _discover_endpoint(self, params: Dict[str, str]) -> str:
        """
        GET /otn/leftTicket/query（不跟随重定向）：
          - 302 + Location/c_url → 当前有效端点（如 queryG）
          - 200 JSON → 直接用 query
        """
        try:
            resp = self.session.get(
                f"{P.BASE_URL}/otn/leftTicket/query",
                params=params, timeout=self.timeout, allow_redirects=False,
            )
        except Exception as e:
            raise QueryError(f"端点发现请求失败: {e}")
        endpoint = "query"
        loc = resp.headers.get("Location", "")
        m = re.search(r'leftTicket/(query\w*)', loc)
        if m:
            endpoint = m.group(1)
        else:
            try:
                payload = resp.json()
                c_url = payload.get("c_url", "")
                m = re.search(r'(query\w*)', c_url)
                if m:
                    endpoint = m.group(1)
                elif payload.get("status") is True:
                    endpoint = "query"
            except ValueError:
                pass
        with self._endpoint_lock:
            if endpoint != self._query_endpoint:
                logger.info(f"查询端点发现: leftTicket/{endpoint}")
            self._query_endpoint = endpoint
        return endpoint

    def query_tickets(
        self, train_date: str, from_station: str, to_station: str,
    ) -> List[Dict[str, Any]]:
        """
        查询余票。from/to 可为站名或三字码。
        失败抛 QueryError；返回 [] 表示无车次。
        """
        stations = self.load_stations()
        from_code = from_station if re.fullmatch(r'[A-Z]+', from_station) \
            else stations.get(from_station, "")
        to_code = to_station if re.fullmatch(r'[A-Z]+', to_station) \
            else stations.get(to_station, "")
        if not from_code or not to_code:
            raise QueryError(f"未知车站: {from_station if not from_code else to_station}")

        params = {
            "leftTicketDTO.train_date": train_date,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": "ADULT",
        }

        with self._endpoint_lock:
            endpoint = self._query_endpoint
        if not endpoint:
            endpoint = self._discover_endpoint(params)

        last_error = ""
        for attempt in range(3):
            try:
                resp = self.session.get(
                    f"{P.BASE_URL}/otn/leftTicket/{endpoint}",
                    params=params, timeout=self.timeout,
                )
                ct = resp.headers.get("Content-Type", "")
                if "json" not in ct and resp.text.lstrip().startswith(("<", "﻿<")):
                    # WAF/错误页 → 重新预热与端点发现
                    last_error = "查询被 WAF 拦截（返回 HTML）"
                    self.prime()
                    endpoint = self._discover_endpoint(params)
                    continue
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("c_url"):
                    # 端点又轮换了
                    endpoint = self._discover_endpoint(params)
                    continue
                if not isinstance(payload, dict) or payload.get("status") is not True:
                    msg = P.message_from_payload(payload if isinstance(payload, dict) else {})
                    last_error = msg or "服务端未确认查询成功"
                    continue
                data = payload.get("data") or {}
                results = data.get("result")
                if not isinstance(results, list):
                    last_error = "余票响应缺少 result 列表"
                    continue
                station_map = data.get("map") or {}
                return P.parse_tickets(results, station_map, train_date)
            except (ValueError, KeyError) as e:
                last_error = f"余票响应解析失败: {type(e).__name__}"
                endpoint = self._discover_endpoint(params)
            except requests.RequestException as e:
                last_error = f"网络异常: {type(e).__name__}"
        raise QueryError(last_error or "查询失败")

    # ------------------------------------------------------------
    #  乘车人
    # ------------------------------------------------------------

    def get_passengers(self) -> List[Dict[str, Any]]:
        """POST getPassengerDTOs（2026 协议为 POST + _json_att）"""
        try:
            resp = self.session.post(
                f"{P.BASE_URL}/otn/confirmPassenger/getPassengerDTOs",
                data={"_json_att": ""}, timeout=self.timeout,
            )
            payload = resp.json()
        except Exception as e:
            raise RailwayError(f"读取乘车人失败: {type(e).__name__}")
        if not isinstance(payload, dict) or payload.get("status") is not True:
            raise NotLoggedInError("读取乘车人失败，请检查登录状态")
        data = payload.get("data") or {}
        passengers = data.get("normal_passengers")
        if not isinstance(passengers, list):
            raise RailwayError("乘车人列表格式无效")
        return [dict(item) for item in passengers if isinstance(item, dict)]

    def match_passengers(
        self, wanted: List[Dict[str, Any]],
        max_age: float = 240.0, force: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        按配置（姓名+证件号，可为脱敏号）从账户联系人中匹配，保持 wanted 顺序。

        结果缓存 max_age 秒（同一批 wanted）：监控/狙击循环中反复下单时
        省去 getPassengerDTOs 的 RTT。force=True 强制刷新（狙击预热用）。
        """
        key = "|".join(f"{w.get('name')}:{w.get('id_number')}" for w in wanted)
        ts, cached_key, cached = self._pax_cache
        if (not force and cached_key == key and cached
                and time.time() - ts < max_age):
            return cached
        all_passengers = self.get_passengers()
        matched = []
        for w in wanted:
            for ap in all_passengers:
                if P.passenger_matches(ap, w):
                    matched.append(ap)
                    break
        if matched:
            self._pax_cache = (time.time(), key, matched)
        return matched

    # ------------------------------------------------------------
    #  下单链
    # ------------------------------------------------------------

    def _post_json(self, url: str, data: Dict[str, Any], stage: str,
                   unknown_on_error: bool = True) -> Dict[str, Any]:
        """
        POST 并要求 JSON 响应。
        unknown_on_error=True: 歧义结果抛 OrderResultUnknown（confirm 之后必须如此）；
        False: 抛 OrderRejected（confirm 之前服务端尚未创建订单，可安全重试）。
        """
        ambiguous = OrderResultUnknown if unknown_on_error else OrderRejected
        try:
            resp = self.session.post(url, data=data, timeout=self.timeout)
            payload = resp.json()
        except Exception as e:
            raise ambiguous(f"{stage} 响应无法确认（{type(e).__name__}）")
        if not isinstance(payload, dict):
            raise ambiguous(f"{stage} 返回非对象 JSON")
        return payload

    def submit_order_request(self, ticket: Dict[str, Any]):
        """步骤1: submitOrderRequest。明确失败 → OrderRejected；歧义 → 可重试"""
        boarding = ticket.get("query_date") or ticket.get("start_train_date")
        data = {
            "secretStr": ticket["secret_str"],
            "train_date": boarding,
            "back_train_date": boarding,
            "tour_flag": "dc",
            "purpose_codes": "ADULT",
            "query_from_station_name": ticket["from_station"],
            "query_to_station_name": ticket["to_station"],
            "undefined": "",
        }
        try:
            resp = self.session.post(
                f"{P.BASE_URL}/otn/leftTicket/submitOrderRequest",
                data=data, timeout=self.timeout,
            )
            payload = resp.json()
        except Exception as e:
            # 尚未占座，网络异常可安全重试
            raise OrderRejected(f"submitOrderRequest 网络异常: {type(e).__name__}")
        if not isinstance(payload.get("status"), bool):
            raise OrderRejected("submitOrderRequest 返回缺少 status")
        if payload["status"]:
            return
        raise OrderRejected(P.message_from_payload(payload) or "提交订单被拒绝")

    def init_dc(self) -> Tuple[str, Dict[str, Any]]:
        """
        步骤2: POST initDc（旧代码用 GET——协议错误）。
        返回 (globalRepeatSubmitToken, ticketInfoForPassengerForm 解析结果)。
        解析失败降级为 regex 单字段提取（DTO 为空 dict）。
        """
        try:
            resp = self.session.post(
                f"{P.BASE_URL}/otn/confirmPassenger/initDc",
                data={"_json_att": ""}, timeout=self.timeout,
            )
            html = resp.text
        except Exception as e:
            raise OrderRejected(f"initDc 请求失败: {type(e).__name__}")

        m = re.search(r"globalRepeatSubmitToken\s*=\s*'([^']+)'", html)
        if not m:
            raise OrderRejected("initDc 未返回 REPEAT_SUBMIT_TOKEN（会话可能失效）")
        token = m.group(1)

        ticket_info: Dict[str, Any] = {}
        obj_text = P.extract_js_object(html, "ticketInfoForPassengerForm")
        if obj_text:
            try:
                ticket_info = P.parse_js_object(obj_text)
            except ValueError:
                ticket_info = {}
        if not ticket_info:
            # 降级：单字段 regex（getQueueCount 会用 ticket 字段兜底）
            for pattern, key in (
                (r"key_check_isChange\s*=\s*'([^']+)'", "key_check_isChange"),
                (r"leftTicketStr\s*=\s*'([^']+)'", "leftTicketStr"),
                (r"train_location\s*=\s*'([^']+)'", "train_location"),
            ):
                fm = re.search(pattern, html)
                if fm:
                    ticket_info[key] = fm.group(1)
        if not ticket_info.get("key_check_isChange"):
            raise OrderRejected("initDc 缺少 key_check_isChange")
        return token, ticket_info

    def check_order_info(
        self, passenger_ticket_str: str, old_passenger_str: str, token: str,
    ) -> Dict[str, Any]:
        """步骤3: checkOrderInfo（新增 scene/_json_att 参数）"""
        data = {
            "cancel_flag": "2",
            "bed_level_order_num": "0" * 30,
            "passengerTicketStr": passenger_ticket_str,
            "oldPassengerStr": old_passenger_str,
            "tour_flag": "dc",
            "randCode": "",
            "whatsSelect": "1",
            "sessionId": "",
            "sig": "",
            "scene": "nc_login",
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        payload = self._post_json(
            f"{P.BASE_URL}/otn/confirmPassenger/checkOrderInfo", data,
            "checkOrderInfo", unknown_on_error=False)
        if not isinstance(payload.get("status"), bool):
            raise OrderRejected("checkOrderInfo 返回缺少 status")
        rdata = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if payload["status"] and rdata.get("submitStatus") is True:
            return rdata
        raise OrderRejected(P.message_from_payload(payload) or "订单信息检查被拒绝")

    def get_queue_count(
        self, ticket: Dict[str, Any], ticket_info: Dict[str, Any],
        seat_code: str, token: str,
    ) -> Dict[str, Any]:
        """
        步骤4: getQueueCount —— 旧代码完全缺失此步骤，
        是 confirmSingleForQueue 静默失败的关键原因之一。
        """
        query_dto = ticket_info.get("queryLeftTicketRequestDTO") or {}
        # 校验订单页车次与候选一致，防止陈旧确认页导致串单
        for key, label in (("train_no", "车次编号"),
                           ("station_train_code", "车次"),
                           ("from_station_telecode", "出发站"),
                           ("to_station_telecode", "到达站")):
            actual, expected = query_dto.get(key), ticket.get(key)
            if actual not in (None, "") and expected not in (None, "") and actual != expected:
                raise OrderResultUnknown(f"订单页{label}与本次候选不一致，请核对 12306 订单")

        boarding = ticket.get("query_date") or ticket.get("start_train_date")
        order_dto = ticket_info.get("orderRequestDTO") or {}
        raw_date = order_dto.get("train_date")
        if isinstance(raw_date, dict) and isinstance(raw_date.get("time"), (int, float)):
            server_date = datetime.fromtimestamp(
                raw_date["time"] / 1000, CST).strftime("%Y-%m-%d")
            if server_date != boarding:
                raise OrderResultUnknown(
                    f"订单页乘车日期 {server_date} 与查询日期 {boarding} 不一致")
            boarding = server_date

        data = {
            "train_date": P.format_queue_date(boarding),
            "train_no": query_dto.get("train_no") or ticket.get("train_no", ""),
            "stationTrainCode": (query_dto.get("station_train_code")
                                 or ticket.get("station_train_code", "")),
            "seatType": seat_code,
            "fromStationTelecode": (query_dto.get("from_station_telecode")
                                    or ticket.get("from_station_telecode", "")),
            "toStationTelecode": (query_dto.get("to_station_telecode")
                                  or ticket.get("to_station_telecode", "")),
            "leftTicket": ticket_info.get("leftTicketStr") or ticket.get("left_ticket", ""),
            "purpose_codes": "00",
            "train_location": ticket_info.get("train_location")
                              or ticket.get("location_code", ""),
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        try:
            resp = self.session.post(
                f"{P.BASE_URL}/otn/confirmPassenger/getQueueCount",
                data=data, timeout=self.timeout,
            )
            payload = resp.json()
        except Exception as e:
            raise OrderRejected(f"getQueueCount 请求异常: {type(e).__name__}")
        if not isinstance(payload, dict) or not payload.get("status"):
            raise OrderRejected(
                P.message_from_payload(payload if isinstance(payload, dict) else {})
                or "getQueueCount 被拒绝")
        return payload.get("data") if isinstance(payload.get("data"), dict) else {}

    def confirm_single_for_queue(
        self, passenger_ticket_str: str, old_passenger_str: str,
        ticket_info: Dict[str, Any], left_ticket: str, token: str,
        choose_seats: str = "", seat_detail_type: str = "000",
    ):
        """
        步骤5: confirmSingleForQueue（新增 _json_att/is_jy；choose_seats 为
        座位格子偏好，默认空；seatDetailType 为铺位偏好三位数字，默认 000）。

        ★ 本请求发出后订单可能已在服务端创建：任何歧义 → OrderResultUnknown。
        """
        data = {
            "passengerTicketStr": passenger_ticket_str,
            "oldPassengerStr": old_passenger_str,
            "randCode": "",
            "purpose_codes": "00",
            "key_check_isChange": ticket_info.get("key_check_isChange", ""),
            "leftTicketStr": left_ticket,
            "train_location": ticket_info.get("train_location", ""),
            "choose_seats": choose_seats,
            "seatDetailType": seat_detail_type,
            "whatsSelect": "1",
            "roomType": "00",
            "dwAll": "N",
            "is_jy": "N",
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": token,
        }
        try:
            resp = self.session.post(
                f"{P.BASE_URL}/otn/confirmPassenger/confirmSingleForQueue",
                data=data, timeout=self.timeout,
            )
        except Exception as e:
            raise OrderResultUnknown(
                f"confirmSingleForQueue 响应超时/异常（{type(e).__name__}），订单可能已创建")
        try:
            payload = resp.json()
        except ValueError:
            raise OrderResultUnknown("confirmSingleForQueue 返回非 JSON，订单结果无法确认")
        if not isinstance(payload.get("status"), bool):
            raise OrderResultUnknown("confirmSingleForQueue 缺少有效 status，订单结果无法确认")
        if payload["status"] is False:
            raise OrderRejected(P.message_from_payload(payload) or "确认排队被明确拒绝")
        rdata = payload.get("data")
        if not isinstance(rdata, dict) or not isinstance(rdata.get("submitStatus"), bool):
            raise OrderResultUnknown("confirmSingleForQueue data 结构异常，订单结果无法确认")
        if rdata["submitStatus"] is False:
            msg = P.message_from_payload(payload)
            if rdata.get("isNeedPay"):
                raise OrderRejected(msg or "确认失败")
            # submitStatus=false 且无消息 = 风控静默拒绝（旧脚本 100% 卡在这里）
            raise OrderRejected(msg or "确认排队被拒绝（无消息，疑似风控/参数不符）")
        return rdata

    def query_order_wait_time(
        self, token: str, wait_seconds: int = 120,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        步骤6: 轮询排队结果。
        返回 (success, message, data)。超时 → 结果未知（订单可能仍在排队）。
        """
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if stop_check and stop_check():
                return False, "用户中止等待（订单可能已生成，请去 12306 核对）", {}
            try:
                resp = self.session.get(
                    f"{P.BASE_URL}/otn/confirmPassenger/queryOrderWaitTime",
                    params={
                        "random": int(time.time() * 1000),
                        "tourFlag": "dc",
                        "_json_att": "",
                        "REPEAT_SUBMIT_TOKEN": token,
                    },
                    timeout=self.timeout,
                )
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("status") is True:
                    rdata = payload.get("data") or {}
                    wait_time = rdata.get("waitTime")
                    if wait_time == 0:
                        return True, "出票成功", rdata
                    if isinstance(wait_time, int) and wait_time < 0:
                        msg = (rdata.get("msg") or rdata.get("errMsg")
                               or P.message_from_payload(payload) or "出票失败")
                        return False, msg, rdata
                    logger.debug(f"排队中 waitTime={wait_time}")
            except Exception:
                pass
            time.sleep(2)
        return False, "__TIMEOUT__", {}

    # ------------------------------------------------------------
    #  完整下单编排
    # ------------------------------------------------------------

    def book(
        self,
        ticket: Dict[str, Any],
        seat_type: str,
        passengers: List[Dict[str, Any]],
        dry_run: bool = False,
        choose_seats: str = "",
        seat_detail_type: str = "000",
        progress: Optional[Callable[[str, str], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> BookResult:
        """
        完整下单链。passengers 必须是 getPassengerDTOs 返回的 API 原始对象
        （含 allEncStr）。progress(stage, message) 用于前端进度展示。
        """
        def report(stage: str, msg: str = ""):
            if progress:
                progress(stage, msg)

        train_code = ticket.get("station_train_code", "?")
        try:
            # 登录态 20s 内已验证过则跳过 checkUser（狙击关键路径省一次 RTT）；
            # 若实际已失效，submitOrderRequest 会明确失败并走 OrderRejected 分支
            if not self.check_login(max_age=20):
                return BookResult("rejected", "login", message="登录已失效，请重新扫码")

            seat_code = P.resolve_submit_seat_code(
                seat_type, ticket.get("station_train_code", ""))
            pstr, opstr = P.build_passenger_strings(passengers, seat_code)

            report("submit", f"提交订单请求 {train_code} {seat_type}")
            self.submit_order_request(ticket)

            report("initDc", "获取订单确认 token")
            token, ticket_info = self.init_dc()

            report("checkOrderInfo", "校验订单信息")
            self.check_order_info(pstr, opstr, token)

            report("getQueueCount", "查询排队人数")
            queue_data = self.get_queue_count(ticket, ticket_info, seat_code, token)
            queue_msg = ""
            if isinstance(queue_data.get("ticket"), str):
                queue_msg = f"，余票 {queue_data['ticket']}"
            if isinstance(queue_data.get("countT"), (int, str)):
                queue_msg += f"，排队 {queue_data['countT']} 人"
            logger.info(f"getQueueCount 通过: {train_code} {seat_type}{queue_msg}")

            if dry_run:
                report("dry_run", "试运行模式：已验证到 getQueueCount，不提交确认")
                return BookResult("dry_run_ok", "getQueueCount",
                                  message=f"协议链路验证通过{queue_msg}",
                                  queue_info=queue_data)

            if stop_check and stop_check():
                return BookResult("rejected", "stopped", message="用户取消（未确认排队）")

            report("confirm", "确认排队下单")
            left_ticket = ticket_info.get("leftTicketStr") or ticket.get("left_ticket", "")
            self.confirm_single_for_queue(
                pstr, opstr, ticket_info, left_ticket, token,
                choose_seats=choose_seats, seat_detail_type=seat_detail_type,
            )

            report("queue", "等待出票...")
            ok, msg, rdata = self.query_order_wait_time(
                token, stop_check=stop_check)
            if ok:
                order_id = rdata.get("orderId", "")
                report("success", f"出票成功，订单号 {order_id}")
                return BookResult("success", "queue", order_id=order_id,
                                  message="出票成功", queue_info=rdata)
            if msg == "__TIMEOUT__":
                return BookResult(
                    "unknown", "queue",
                    message="排队超时，订单可能仍在处理——请立即登录 12306 核对，勿重复下单",
                    queue_info=rdata)
            return BookResult("rejected", "queue", message=msg, queue_info=rdata)

        except OrderResultUnknown as e:
            logger.error(f"[{train_code}] 订单结果未知: {e}")
            return BookResult("unknown", "confirm",
                              message=f"{e}——请立即登录 12306 核对订单，勿重复下单")
        except OrderRejected as e:
            logger.info(f"[{train_code}] 下单被拒绝: {e}")
            return BookResult("rejected", "order", message=str(e))
        except NotLoggedInError as e:
            return BookResult("rejected", "login", message=str(e))
        except Exception as e:
            logger.exception(f"[{train_code}] 下单异常")
            return BookResult("unknown", "error",
                              message=f"未预期异常 {type(e).__name__}: {e}——请核对 12306 订单")
