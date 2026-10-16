"""
12306 Web 抢票服务包

纯 HTTP 协议实现（无 Selenium），由 FastAPI 提供 Web 界面。

模块:
  protocol        — 协议常量与纯函数工具（座位映射/字段解析/乘车人字符串）
  railway_client  — RailwayClient: 登录/查询/完整下单链
  config_store    — web_config.json 配置读写
  task_manager    — 后台任务（监控/狙击/手动下单）与事件广播
  app             — FastAPI 入口 (python -m server.app)
"""
