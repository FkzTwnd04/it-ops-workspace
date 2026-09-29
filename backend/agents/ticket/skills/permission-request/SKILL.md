---
name: permission-request
description: 共享目录、业务系统、VPN 等权限申请的处理流程
---

# 权限申请

## 标准流程
1. 从工单中确认三要素：申请人账号、目标资源、需要的权限级别（read / write / admin）。
   任一要素缺失，resolved 填 false，在结论里列出需要用户补充的信息。
2. 默认按最小权限授予：只需要查看的给 read，需要编辑的给 write；admin 权限一律转人工。
3. 调用 grant_permission 授权（高危，需审批），reason 中写明业务用途。
4. 授权后提醒用户注销重新登录，权限才会生效。
