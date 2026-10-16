"""
协议层单元测试 — 无第三方依赖

运行:  python tests/test_protocol.py

覆盖 12306 协议中所有"改错一个字符就下单失败"的纯函数：
乘车人字符串格式 / 队列日期格式 / initDc JS 对象解析 / 余票字段映射 /
座位索引（防幽灵票回归）/ 提交码 / 脱敏证件号匹配四象限。
测试数据取自 2026-06-13 真实日志与 2026-10-16 实时查询。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import protocol as P  # noqa: E402

PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    assert cond, f"{name} 失败: {detail}"
    PASSED += 1
    print(f"  ✓ {name}")


# ---------- 1. 乘车人字符串（2026 格式） ----------

def test_passenger_strings():
    p = {"passenger_name": "张三", "passenger_id_type_code": "1",
         "passenger_id_no": "4201**1X", "mobile_no": "138****0000",
         "passenger_type": "1", "allEncStr": "abc123"}
    pstr, opstr = P.build_passenger_strings([p], "O")
    check("passengerTicketStr 逗号+下划线格式",
          pstr == "O,0,1,张三,1,4201**1X,138****0000,N,abc123", pstr)
    check("oldPassengerStr 格式", opstr == "张三,1,4201**1X,1_", opstr)
    # 无 allEncStr 时不带尾字段
    p2 = dict(p); p2.pop("allEncStr")
    pstr2, _ = P.build_passenger_strings([p2], "O")
    check("缺 allEncStr 时省略尾字段", pstr2 == "O,0,1,张三,1,4201**1X,138****0000,N", pstr2)


# ---------- 2. getQueueCount 日期格式 ----------

def test_queue_date():
    got = P.format_queue_date("2026-10-20")
    check("JS Date 字符串格式",
          got == "Tue Oct 20 2026 00:00:00 GMT+0800 (中国标准时间)", got)


# ---------- 3. initDc JS 对象解析 ----------

def test_js_object():
    html = ('var ticketInfoForPassengerForm={"a":{"b":"}"},"key_check_isChange":"K123"};'
            'var other=1;')
    obj = P.extract_js_object(html, "ticketInfoForPassengerForm")
    parsed = P.parse_js_object(obj)
    check("花括号配对提取（含字符串内 } ）",
          parsed.get("key_check_isChange") == "K123" and parsed["a"]["b"] == "}", parsed)
    obj2 = P.parse_js_object('{"x":undefined,"y":"undefined"}')
    check("裸 undefined → null，字符串内不动",
          obj2["x"] is None and obj2["y"] == "undefined", obj2)


# ---------- 4. 余票解析 + 座位索引（防幽灵票回归） ----------

def test_parse_tickets():
    # G3471 真实字段（2026-06-13 日志）: [30]=有(二等) [31]=1(一等) [32]=5(商务)
    head = "SECRET%2BENC|预订|G3471x|G3471|VAP|KQW|WHN|KQW|07:44|15:35|"
    tail = "07:51|Y|ENC|20260627|3|N2|01|13|1|0|||||||无||||有|1|5||90M0O0W0|9MOO"
    t = P.parse_tickets([head + tail], {"WHN": "武汉", "KQW": "贵阳北"}, "2026-10-20")[0]
    check("G 车席位: 二等=有 一等=1 商务=5",
          t["seats"]["二等座"] == "有" and t["seats"]["一等座"] == "1"
          and t["seats"]["商务座"] == "5", t["seats"])
    check("secret_str 已 URL 解码", t["secret_str"] == "SECRET+ENC", t["secret_str"])
    check("left_ticket/location/query_date",
          t["left_ticket"] == "ENC" and t["location_code"] == "N2"
          and t["query_date"] == "2026-10-20")
    check("can_buy_online（按钮=预订）", t["can_buy_online"] is True)

    # Z161 真实字段: [29]=11(硬座)。旧索引错位曾把此车误判为"二等座有票"→下单必败
    zh = "SEC|预订|Z161x|Z161|WHN|KQW|WHN|KQW|22:33|15:03|"
    zt = "16:30|Y|ENC2|20260627|3|P2|07|10|1|0||||无|||无||无|11|||||304010W0|3411"
    tz = P.parse_tickets([zh + zt], {}, "2026-10-20")[0]
    check("Z 车硬座=11", tz["seats"]["硬座"] == "11", tz["seats"])
    check("Z 车无二等座（幽灵票回归防护）", not P.is_seat_available(tz["seats"]["二等座"]))


# ---------- 5. 提交码 ----------

def test_seat_codes():
    check("无座高铁→O", P.resolve_submit_seat_code("无座", "G1525") == "O")
    check("无座普速→1", P.resolve_submit_seat_code("无座", "K107") == "1")
    check("二等座→O", P.resolve_submit_seat_code("二等座", "K107") == "O")
    check("软卧→4 / 硬卧→3", P.SEAT_CODE_MAP["软卧"] == "4" and P.SEAT_CODE_MAP["硬卧"] == "3")


# ---------- 6. 脱敏证件号匹配四象限 ----------

def test_passenger_match():
    full, masked = "110101199001011234", "1101***********1234"
    api_m = {"passenger_name": "测试甲", "passenger_id_no": masked}
    api_f = {"passenger_name": "测试甲", "passenger_id_no": full}
    check("API脱敏 × 配置完整", P.passenger_matches(api_m, {"name": "测试甲", "id_number": full}))
    check("API完整 × 配置脱敏（网页勾选场景）",
          P.passenger_matches(api_f, {"name": "测试甲", "id_number": masked}))
    check("双脱敏", P.passenger_matches(api_m, {"name": "测试甲", "id_number": masked}))
    check("双完整", P.passenger_matches(api_f, {"name": "测试甲", "id_number": full}))
    check("尾号不符 → 拒绝",
          not P.passenger_matches(api_m, {"name": "测试甲", "id_number": "110101199001011235"}))
    check("姓名不符 → 拒绝", not P.passenger_matches(api_m, {"name": "其他人", "id_number": full}))
    check("无证件号 → 仅按姓名", P.passenger_matches(api_m, {"name": "测试甲", "id_number": ""}))


# ---------- 7. 有票判定 / 终局失败识别 ----------

def test_misc():
    check("is_seat_available", P.is_seat_available("有") and P.is_seat_available("5")
          and not P.is_seat_available("无") and not P.is_seat_available("0")
          and not P.is_seat_available("*") and not P.is_seat_available(""))
    check("终局失败消息识别",
          P.is_terminal_order_failure("没有足够的票") and P.is_terminal_order_failure("出票失败")
          and not P.is_terminal_order_failure("网络超时"))


if __name__ == "__main__":
    print("12306 协议单元测试\n" + "=" * 40)
    for fn in (test_passenger_strings, test_queue_date, test_js_object,
               test_parse_tickets, test_seat_codes, test_passenger_match, test_misc):
        print(f"\n[{fn.__name__}]")
        fn()
    print("\n" + "=" * 40)
    print(f"全部通过: {PASSED} 项断言 ✅")
