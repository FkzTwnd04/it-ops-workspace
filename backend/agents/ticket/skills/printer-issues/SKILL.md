---
name: printer-issues
description: 打印机无法打印、打印任务卡住、打印乱码的处理流程
---

# 打印故障处理

## 标准流程
1. 用 query_system_logs 查打印服务器日志，关键词 spooler、stuck、driver。
2. 任务卡在 PRINTING 状态：执行 clear_print_queue（低风险），让用户重新提交打印。
3. 驱动崩溃（driver crashed）：清空队列后仍失败，指导用户删除打印机并从打印服务器重新添加，
   让系统自动安装统一驱动。
4. 碳粉低于 5%：提示用户联系行政更换耗材，不属于 IT 故障。
5. 多人同时无法打印且 spooler 为 degraded：restart_service 重启 spooler（高危，需审批）。
