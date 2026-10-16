# 12306 自动抢票脚本

基于 Python 的 12306 自动抢票工具，提供 **Web 控制台**（推荐）与命令行脚本两种使用方式。

## 🚄 Web 抢票控制台（推荐）

**最简单的方式：双击 `12306.exe`**（独立运行，无需安装 Python）—— 弹出图形启动器，一键启动/停止服务、查看后台日志、自动打开网页。源码用户可双击 `启动.bat`（效果相同）。

<details>
<summary>或用命令行启动</summary>

```bash
pip install -r requirements.txt   # 需要 fastapi / uvicorn / json5
python -m server                  # 自动打开 http://127.0.0.1:8765
```

</details>

浏览器里完成全部操作：扫码登录、配置行程、监控捡漏、定时狙击、手动一键预订。

- **扫码登录**：网页内直接显示 12306 二维码（纯 API，不弹浏览器窗口）
- **监控捡漏**：三态自适应轮询（快速/正常/退避），有票自动下单
- **定时狙击**：服务器校时 + 放票前预热 + 开售瞬间提交
- **手动预订**：余票表格每行【预订】按钮，一键下单
- **试运行模式**：走到排队查询即停，不真实下单——用于验证协议链路
- **实时推送**：余票表、日志、下单进度经 WebSocket 实时刷新；成功后声音 + 浏览器通知 + ServerChan/PushDeer/Bark 推送
- 服务仅绑定 `127.0.0.1`，配置保存在 `data/web_config.json`

> ⚠️ 2026 年 12306 风控已大幅升级（慢速队列/拒绝出票）。本工具默认轮询间隔保守，请勿调得过激；账号被判定恶意抢票可能被限制出票。订单结果未知时（超时等），务必先登录 12306 核对，切勿重复下单。

## 命令行脚本（旧版，保留）

> ⚠️ 已知问题：旧 CLI 脚本的下单协议已过期（缺 `getQueueCount` 步骤、乘车人字符串为旧格式、`config.SEAT_INDICES` 座位索引错位、查询端点 `queryI` 已失效），`confirmSingleForQueue` 会被服务端静默拒绝。请优先使用 Web 控制台（`server/` 为修正后的完整协议实现）。

基于 Python 的 12306 自动抢票脚本，支持手动扫码登录、自动查询余票、自动提交订单。

## 功能特点

- ✅ **手动登录** - 浏览器弹出登录页面，扫码登录，安全性高
- ✅ **Cookie 持久化** - 登录信息自动保存，下次无需重新登录
- ✅ **自动查询** - 定时查询指定车次余票
- ✅ **自动提交** - 检测到余票后自动提交订单
- ✅ **多座位选择** - 支持按优先级选择座位类型
- ✅ **多乘车人** - 支持为多位乘车人同时购票
- ✅ **表格展示** - 清晰的余票信息表格
- ✅ **声音提醒** - 抢票成功后播放提示音

## 环境要求

- Python 3.8+
- Chrome / Edge / Firefox 浏览器
- 对应的浏览器驱动 (ChromeDriver / EdgeDriver / GeckoDriver)

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 安装浏览器驱动

**方式一：使用 Chrome**
- 下载 [ChromeDriver](https://chromedriver.chromium.org/) (版本需与 Chrome 一致)
- 将 `chromedriver.exe` 放在 Python 安装目录或项目目录下

**方式二：使用 Edge**
- Edge 浏览器通常已自带 EdgeDriver，无需额外安装

**方式三：使用 Firefox**
- 下载 [GeckoDriver](https://github.com/mozilla/geckodriver/releases)

### 3. 修改配置

编辑 `config.py` 文件：

```python
# 修改出发日期
TRAIN_DATE = "2026-06-15"

# 修改出发站和到达站
FROM_STATION = "北京"
TO_STATION = "上海"

# 修改乘车人信息
PASSENGERS = [
    {"name": "张三", "id_number": "110101199001011234"},
    {"name": "李四", "id_number": "110101199001015678"},
]
```

### 4. 运行脚本

```bash
python cli/12306_ticket.py
```

## 使用流程

1. 运行脚本后，会自动弹出浏览器窗口
2. 在浏览器中使用 12306 APP 扫码登录
3. 登录后脚本会自动获取会话信息并关闭浏览器
4. 开始自动查询余票
5. 发现符合条件的余票后自动提交订单
6. 订单成功后播放提示音，显示订单号

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `TRAIN_DATE` | 出发日期 (YYYY-MM-DD) | - |
| `FROM_STATION` | 出发站 | 北京 |
| `TO_STATION` | 到达站 | 上海 |
| `PREFERRED_TRAINS` | 首选车次列表 (留空=全部) | [] |
| `SEAT_TYPES` | 座位类型优先级 | ["二等座","一等座","商务座"] |
| `PASSENGERS` | 乘车人列表 | [] |
| `AUTO_SUBMIT` | 自动提交订单 | True |
| `QUERY_INTERVAL` | 查询间隔(秒) | 1.5 |
| `MAX_RETRIES` | 最大重试次数(0=无限) | 0 |
| `SOUND_ALERT` | 声音提醒 | True |

## 注意事项

1. **登录有效期**：Cookie 有效期约 7 天，过期后需重新登录
2. **查询频率**：建议查询间隔不低于 1 秒，避免被封 IP
3. **购票时间**：12306 通常在每日 8:00-18:00 之间放票
4. **支付时限**：订单成功后需在 45 分钟内完成支付
5. **法律责任**：本脚本仅用于个人学习和研究，请遵守相关法律法规

## 常见问题

### Q: 浏览器无法正常打开？
A: 确保已安装对应浏览器的 WebDriver，并已添加到系统 PATH

### Q: 查询不到车次？
A: 确认车站名称是否正确 (如 "上海虹桥" 不是 "上海")，确认出发日期未过期

### Q: 提交订单失败？
A: 可能是 12306 接口变动，或需要先登录 12306 APP 完成实名核验

### Q: 被封 IP 了怎么办？
A: 降低查询频率 (QUERY_INTERVAL 调大)，更换 IP 后等待一段时间

## 项目结构

```
12306/
├── server/               # ★ Web 抢票服务（推荐，2026 修正协议，纯 HTTP）
│   ├── __main__.py       #   启动入口: python -m server
│   ├── app.py            #   FastAPI: REST + WebSocket + 静态页托管
│   ├── railway_client.py #   RailwayClient — QR登录/端点发现查询/完整下单链
│   ├── task_manager.py   #   监控/狙击/手动下单任务 + EventBus(WS广播)
│   ├── config_store.py   #   data/web_config.json 配置读写
│   ├── protocol.py       #   协议常量（座位索引/提交码/字段解析）
│   └── paths.py          #   运行时路径统一管理（data/ 目录）
├── web/
│   └── index.html        # Web 控制台（单文件 Vue3，暗色主题）
├── cli/                  # 旧版 CLI（原样保留，协议已过期仅供参考）
│   ├── 12306_ticket.py   #   python cli/12306_ticket.py
│   ├── 12306_sniper.py   #   python cli/12306_sniper.py
│   ├── common.py         #   共享基类 BaseBot（Selenium 方案）
│   └── config.py         #   CLI 配置
├── tests/
│   └── test_protocol.py  # 协议单元测试: python tests/test_protocol.py
├── data/                 # 运行时产物（gitignored）
│   ├── logs/             #   web.log / 12306_ticket.log / 12306_sniper.log
│   ├── web_config.json   #   Web 配置
│   ├── web_session.pkl   #   Web 登录会话
│   └── 12306_cookies.pkl #   CLI 登录会话
├── requirements.txt      # Python 依赖
└── README.md / USAGE.md / CLAUDE.md
```

## 免责声明

本项目**仅供个人学习与技术研究使用**，不得用于任何商业用途或倒卖车票等牟利行为。

- 使用者须遵守 12306 网站用户协议及相关法律法规，因使用本项目产生的任何后果（包括但不限于账号被限制、进入慢速队列、订单异常等）由使用者自行承担
- 12306 已部署风控系统识别自动化抢票行为（2026 年起对第三方抢票渠道有明确的限流与拒绝出票措施），请保持保守的查询频率，勿滥用
- 本项目**不提供代购、代付服务，不保证出票**；购票结果取决于 12306 官方余票与排队机制
- 项目按 MIT 协议以"现状"提供，不作任何明示或暗示的担保

如仅需购票，推荐直接使用「铁路 12306」官方 App 的**候补购票**功能。
