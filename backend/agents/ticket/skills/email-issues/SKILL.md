---
name: email-issues
description: 邮件发不出去、收不到邮件、邮箱容量满的处理流程
---

# 邮箱故障处理

## 标准流程
1. 用 query_system_logs 查邮件服务器日志，关键词 queue、deferred、quota。
2. 邮箱容量超过 95%：指导用户清理已删除邮件和大附件，或申请扩容（走权限申请流程）。
3. 发信队列积压（queue length 超过阈值）：属于服务端问题，影响多人时按 P2 处理，
   需要 restart_service 重启 smtp-relay（高危，需审批），重启后积压邮件会自动重发。
4. 只有单个用户收不到外部邮件：检查垃圾邮件文件夹和收件规则，再检查发件方是否被拦截。
