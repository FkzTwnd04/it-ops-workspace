---
name: vpn-troubleshooting
description: VPN 无法连接、频繁断线、连上后访问不了内网的排查流程
---

# VPN 故障排查

## 排查顺序
1. 用 query_system_logs 查 VPN 网关日志，关键词 certificate、DPD、ip pool。
2. 日志出现 "client certificate expired"：用户客户端证书过期，指导用户在自助门户重新下载证书，
   不需要重启网关。
3. 日志出现 "ip pool usage" 超过 95%：地址池耗尽，属于网关侧问题。
   - 影响多人时按 P2 处理，需要 restart_service 重启 vpn-gateway 释放僵死会话（高危，需审批）。
4. 日志出现 "DPD timeout" 且只影响单个用户：多为本地网络不稳定，指导用户切换网络后重连。
5. 连上 VPN 但访问不了内网：检查账号是否在 vpn-users 组（get_user_account），不在组里走权限申请。

## 注意
- 重启网关会断开所有在线用户，工作时间内需说明影响范围。
