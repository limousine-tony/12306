"""
12306 协议常量与纯函数工具

协议细节以 2026-09 实测可用的开源实现为参照（Tuan-Space/12306FairTicket），
并在 2026-10-16 对本机网络做了端点实测验证。

与旧 CLI 脚本（common.py/config.py）的关键协议差异——旧脚本下单失败的根因：
  1. 缺 getQueueCount 步骤（checkOrderInfo 之后、confirmSingleForQueue 之前必须调用）
  2. initDc 必须 POST（带 _json_att），且要解析 ticketInfoForPassengerForm
  3. checkOrderInfo 需要 scene=nc_login、_json_att 参数
  4. confirmSingleForQueue 需要 _json_att、is_jy；choose_seats 现为座位格子
     （如 1A2F），默认空串；铺位偏好走 seatDetailType（三位 0-5 数字），默认 000
  5. passengerTicketStr/oldPassengerStr 2026 格式为逗号分隔字段、下划线分隔条目，
     且需携带 getPassengerDTOs 返回的 allEncStr
  6. 查询端点动态轮换：/otn/leftTicket/query 会 302 或返回 c_url 指向当前
     有效端点（2026-10-16 实测为 queryG），不能硬编码 queryI/queryZ
  7. getPassengerDTOs / checkUser 均为 POST（带 _json_att）
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple

BASE_URL = "https://kyfw.12306.cn"

# 请求头须与 2026-09 实测可用的参考实现完全一致：
#   ★ 必须带 Origin（passport 登录端点 uamtk 校验 Origin，缺失会 302→error.html）
#   ★ 不要带 X-Requested-With（会触发 WAF 对登录/下单接口的差异化拦截）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/otn/leftTicket/init?linktypeid=dc",
    "Origin": BASE_URL,
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# 座位类型 → 余票字段索引
# ★ 2026-10-16 修正：采用与 py12306 / 12306FairTicket（2026-09 实测可用）一致的
#   规范映射，并用本项目日志中的原始字段数据交叉验证（G3471: [30]=有 [31]=1 [32]=5；
#   Z161: [29]=11 → 硬座）。config.py 里 2026-06-13 的"校准"整体错了一列，
#   导致在 K/Z 车上把其他席别计数误读为二等座余票（幽灵票 → 下单必被拒）。
SEAT_INDICES = {
    "商务座": 32, "特等座": 25, "一等座": 31, "二等座": 30,
    "高级软卧": 21, "软卧": 23, "硬卧": 28, "动卧": 33,
    "软座": 24, "硬座": 29, "无座": 26,
}

# 座位类型 → 12306 提交码（无座按车型区分，见 resolve_submit_seat_code；
# 一等卧 I / 二等卧 J 与软卧/硬卧共享显示列，v1 暂不支持自动区分）
SEAT_CODE_MAP = {
    "商务座": "9", "特等座": "P", "一等座": "M", "二等座": "O",
    "高级软卧": "6", "软卧": "4", "硬卧": "3",
    "软座": "2", "硬座": "1", "无座": "1", "动卧": "F",
}

HIGH_SPEED_PREFIXES = ("G", "D", "C")

# getQueueCount 的 train_date 需要 JS Date 字符串格式
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

# 出现这些消息说明订单已终局失败（不是风控静默拒绝），可安全换候选
TERMINAL_ORDER_FAILURES = (
    "没有足够的票", "余票不足", "出票失败", "占座失败",
    "排队失败", "订单提交失败", "取消排队",
)


# ==================== 通用工具 ====================

def is_seat_available(val: str) -> bool:
    """余票值是否表示有票（同 common.is_seat_available）"""
    if not val or val in ("无", "", "*", "0", "--"):
        return False
    try:
        return int(val) > 0
    except ValueError:
        return val == "有"


def format_queue_date(train_date: str) -> str:
    """'2026-10-20' → 'Tue Oct 20 2026 00:00:00 GMT+0800 (中国标准时间)'"""
    parsed = datetime.strptime(train_date, "%Y-%m-%d")
    return (
        f"{WEEKDAY_NAMES[parsed.weekday()]} {MONTH_NAMES[parsed.month]} "
        f"{parsed.day:02d} {parsed.year} 00:00:00 GMT+0800 (中国标准时间)"
    )


def message_from_payload(payload: Dict[str, Any]) -> str:
    """从 12306 响应中提取人类可读的错误消息"""
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        return str(messages[0])
    if isinstance(messages, str) and messages:
        return messages
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("errMsg", "msg", "message"):
            if data.get(key):
                return str(data[key])
    for key in ("message", "result_message"):
        if payload.get(key):
            return str(payload[key])
    return ""


def is_terminal_order_failure(message: Any) -> bool:
    text = str(message or "")
    return any(marker in text for marker in TERMINAL_ORDER_FAILURES)


def resolve_submit_seat_code(seat_label: str, station_train_code: str) -> str:
    """无座在高铁动车上提交码为 O，普速为 1；其余按 SEAT_CODE_MAP"""
    if seat_label != "无座":
        return SEAT_CODE_MAP.get(seat_label, "O")
    code = (station_train_code or "").upper()
    return "O" if code.startswith(HIGH_SPEED_PREFIXES) else "1"


# ==================== initDc JS 对象解析 ====================

def extract_js_object(text: str, variable_name: str) -> str:
    """从 HTML/JS 中按花括号配对提取 var 对象字面量（引号感知）"""
    start = text.find(variable_name)
    if start == -1:
        return ""
    brace_start = text.find("{", start)
    if brace_start == -1:
        return ""
    depth = 0
    quote = ""
    escaped = False
    for index in range(brace_start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start: index + 1]
    return ""


def _replace_bare_undefined(text: str) -> str:
    """把字符串外的裸 undefined 替换为 null（JSON5 兼容）"""
    result: List[str] = []
    index = 0
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            result.append(char)
            if char == "\\" and index + 1 < len(text):
                index += 1
                result.append(text[index])
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            result.append(char)
            index += 1
            continue
        if text.startswith("undefined", index):
            before = text[index - 1] if index else " "
            after = text[index + 9] if index + 9 < len(text) else " "
            if not (before.isalnum() or before in "_$") and not (after.isalnum() or after in "_$"):
                result.append("null")
                index += 9
                continue
        result.append(char)
        index += 1
    return "".join(result)


def parse_js_object(text: str) -> Dict[str, Any]:
    """解析 initDc 内嵌 JS 对象：先严格 JSON，再 JSON5（若已安装），最后报错"""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            import json5  # 可选依赖
            parsed = json5.loads(_replace_bare_undefined(text))
        except ImportError:
            parsed = json.loads(_replace_bare_undefined(text))
        except (TypeError, ValueError) as exc:
            raise ValueError("initDc 的 JS 对象不是可解析的 JSON/JSON5") from exc
    if not isinstance(parsed, dict):
        raise ValueError("initDc 的 JS 对象根节点必须是对象")
    return parsed


# ==================== 乘车人 ====================

def build_passenger_strings(
    passengers: List[Dict[str, Any]], seat_code: str,
) -> Tuple[str, str]:
    """
    构建 2026 版提交字符串。

    passengerTicketStr: 每条 "席别,0,票种,姓名,证件类型,证件号,手机号,N[,allEncStr]"，
                        条目间以 "_" 连接
    oldPassengerStr:    每条 "姓名,证件类型,证件号,乘客类型_"，直接连接
    """
    ticket_list = []
    old_list = []
    for p in passengers:
        passenger_type = p.get("passenger_type") or "1"
        ticket_type = p.get("ticket_type") or passenger_type
        name = p.get("passenger_name") or ""
        id_type = p.get("passenger_id_type_code") or ""
        id_no = p.get("passenger_id_no") or ""
        mobile = p.get("mobile_no") or ""
        all_enc = p.get("allEncStr") or p.get("allEncstr") or ""
        fields = [seat_code, "0", ticket_type, name, id_type, id_no, mobile, "N"]
        if all_enc:
            fields.append(all_enc)
        ticket_list.append(",".join(str(item) for item in fields))
        old_list.append(f"{name},{id_type},{id_no},{passenger_type}_")
    return "_".join(ticket_list), "".join(old_list)


def _visible_match(full_id: str, visible: str) -> bool:
    """脱敏号可见部分（去*后 = 前缀+后缀）与完整号比对"""
    if len(visible) >= 4:
        return full_id.startswith(visible[:4]) and full_id.endswith(visible[4:])
    return full_id.startswith(visible) or full_id.endswith(visible)


def passenger_matches(api_passenger: Dict[str, Any], wanted: Dict[str, Any]) -> bool:
    """
    判断 API 乘车人是否匹配目标（算法源自 common.BaseBot._passenger_matches，
    并补全反向场景：配置为脱敏号而 API 返回完整号）。

    wanted 的 id_number 可以是完整号或脱敏号；缺省时仅按姓名匹配。
    """
    name_ok = (api_passenger.get("passenger_name", "").strip()
               == wanted.get("name", "").strip())
    if not name_ok:
        return False

    api_id = api_passenger.get("passenger_id_no", "").strip()
    cfg_id = (wanted.get("id_number") or "").strip()
    if not cfg_id:
        return True
    if api_id == cfg_id:
        return True

    api_visible = api_id.replace("*", "")
    cfg_visible = cfg_id.replace("*", "")
    api_masked = "*" in api_id
    cfg_masked = "*" in cfg_id
    if api_masked and not cfg_masked:
        # API 脱敏、配置完整（旧 CLI 场景）
        return len(api_visible) >= 4 and _visible_match(cfg_id, api_visible)
    if cfg_masked and not api_masked:
        # 配置脱敏（网页勾选保存的就是脱敏号）、API 完整
        return len(cfg_visible) >= 4 and _visible_match(api_id, cfg_visible)
    # 双方均脱敏：可见部分一致即可
    return bool(api_visible) and api_visible == cfg_visible


# ==================== 余票解析 ====================

def parse_tickets(
    raw_results: List[str], station_map: Dict[str, str], query_date: str,
) -> List[Dict[str, Any]]:
    """
    解析 leftTicket/query 的管道分隔结果。

    索引映射以 12306FairTicket._parse_tickets（2026-09 可用）为准：
    [0]=secretStr(URL编码) [1]=按钮文本 [2]=train_no [3]=车次 [4]/[5]=始发/终到站码
    [6]/[7]=乘车站码 [8]/[9]/[10]=发/到/历时 [11]=可否网购 [12]=leftTicket(编码串)
    [13]=发车日期 [15]=location_code [34]=yp_info [35]=席别汇总
    """
    tickets = []
    for raw in raw_results:
        fields = raw.split("|")
        if len(fields) < 35:
            continue
        try:
            import urllib.parse

            def item(index: int) -> str:
                return fields[index] if index < len(fields) else ""

            train_date = item(13)
            if len(train_date) == 8:
                train_date = f"{train_date[:4]}-{train_date[4:6]}-{train_date[6:]}"
            from_code = item(6)
            to_code = item(7)
            seats = {}
            for seat_name, idx in SEAT_INDICES.items():
                value = item(idx)
                seats[seat_name] = value if value else "无"

            tickets.append({
                "secret_str": urllib.parse.unquote(item(0)),
                "button_text": item(1),
                "train_no": item(2),
                "station_train_code": item(3),
                "start_station_telecode": item(4),
                "end_station_telecode": item(5),
                "from_station_telecode": from_code,
                "to_station_telecode": to_code,
                "from_station": station_map.get(from_code, from_code),
                "to_station": station_map.get(to_code, to_code),
                "depart_time": item(8),
                "arrive_time": item(9),
                "duration": item(10),
                "can_buy_online": item(11) == "Y" or item(1) == "预订",
                "left_ticket": item(12),
                "start_train_date": train_date,
                "query_date": query_date,
                "location_code": item(15),
                "from_station_no": item(16),
                "to_station_no": item(17),
                "yp_info": item(34),
                "seat_summary": item(35),
                "seats": seats,
            })
        except Exception:
            continue
    return tickets
