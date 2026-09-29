# backend/agents/ticket/mock_ops.py
# 模拟运维环境：日志、服务状态、AD 账号。不触达任何真实系统，接入真实环境时替换本文件即可。
#
# 模拟方式：按工单描述的现象生成"与现象一致"的系统状态（报账号锁定的用户，AD 里就是锁定的；
# 只有一个人打印卡住，打印服务就是正常的，只是队列里有卡死任务）。Agent 仍需自己查询、判断和选择操作，
# 模拟环境只保证"查到的状态不自相矛盾"。环境按工单隔离且有状态：解锁 / 重启 / 授权之后再查会看到变化。

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_MULTI = r"多人|多名|好几个|同事|部门|大家|整个|大面积|至少|其他人|也一样|也是"


@dataclass
class _Env:
    profile: set[str]
    locked: bool = False
    expired: bool = False
    in_vpn_group: bool = True
    degraded: dict[str, bool] = field(default_factory=dict)
    print_stuck: bool = False


_envs: dict[str, _Env] = {}


def _profile(text: str) -> set[str]:
    p: set[str] = set()
    multi = bool(re.search(_MULTI, text))
    if re.search(r"锁定|锁住|锁了|被锁", text):
        p.add("locked")
    if re.search(r"过期|expired|到期", text, re.I):
        p.add("expired")
    if re.search(r"vpn", text, re.I):
        if "证书" in text:
            p.add("vpn_cert")
        elif "内网" in text or "公司系统" in text or "内部系统" in text:
            p.add("vpn_no_group")
        elif multi or "分配" in text or "地址" in text:
            p.add("vpn_pool")
    if re.search(r"邮件|邮箱|outlook", text, re.I):
        if re.search(r"满|容量|配额|存储", text):
            p.add("mail_quota")
        elif multi and re.search(r"发不出|发件箱|延迟|积压|收不到|卡", text):
            p.add("mail_queue")
    if "打印" in text:
        if re.search(r"乱码|驱动|字符", text):
            p.add("print_driver")
        else:
            p.add("print_stuck")
    if re.search(r"erp", text, re.I):
        p.add("erp_pool")
    return p


def env_for(ticket_id: str, ticket_text: str) -> _Env:
    env = _envs.get(ticket_id)
    if env is None:
        p = _profile(ticket_text)
        env = _Env(
            profile=p,
            locked="locked" in p,
            expired="expired" in p,
            in_vpn_group="vpn_no_group" not in p,
            degraded={"vpn-gateway": "vpn_pool" in p, "smtp-relay": "mail_queue" in p,
                      "erp-app": "erp_pool" in p},
            print_stuck="print_stuck" in p,
        )
        _envs[ticket_id] = env
    return env


def _service_for(host: str) -> str:
    h = (host or "").lower()
    if "vpn" in h:
        return "vpn-gateway"
    if any(m in h for m in ("mail", "exch", "smtp")):
        return "smtp-relay"
    if any(m in h for m in ("print", "prt")):
        return "spooler"
    if "erp" in h:
        return "erp-app"
    if any(m in h for m in ("dc", "ad", "ldap")):
        return "netlogon"
    return "sysmon"


def query_logs(env: _Env, host: str, username: str, keyword: str = "", minutes: int = 60) -> list[str]:
    service = _service_for(host)
    p = env.profile
    lines: list[str] = []
    if service == "vpn-gateway":
        if "vpn_cert" in p:
            lines.append(f"WARN  vpn-gateway  tls handshake failed: client certificate expired (user={username})")
        if env.degraded.get("vpn-gateway"):
            lines += ["WARN  vpn-gateway  ip pool usage 97% (242/250)",
                      "ERROR vpn-gateway  address allocation failed: pool exhausted"]
        if "vpn_no_group" in p:
            lines.append(f"INFO  vpn-gateway  user={username} assigned guest subnet 10.99.0.0/24 (no vpn-users group)")
    elif service == "smtp-relay":
        if env.degraded.get("smtp-relay"):
            lines += ["ERROR smtp-relay   queue length 1843 exceeds threshold 500",
                      "WARN  smtp-relay   delivery deferred: 451 4.7.0 temporary server error"]
        if "mail_quota" in p:
            lines.append(f"WARN  exchange     mailbox quota 99% for user={username}")
    elif service == "spooler":
        if env.print_stuck:
            lines.append("ERROR spooler      job 4412 stuck in state PRINTING for 45m")
        if "print_driver" in p:
            lines.append("WARN  spooler      driver HP-UPD-7.0 crashed on client render")
    elif service == "erp-app":
        if env.degraded.get("erp-app"):
            lines += ["ERROR erp-app      connection pool exhausted (active=100, max=100)",
                      "WARN  erp-app      slow query 8.4s: SELECT * FROM fi_voucher ..."]
    elif service == "netlogon":
        if env.locked:
            lines.append(f"WARN  dc01         account locked out: user={username} after 5 failed logons")
        if env.expired:
            lines.append(f"INFO  dc01         password expired for user={username} (last set 92 days ago, policy 90d)")

    if not lines:
        lines.append(f"INFO  {service:<12} no error events in the last {minutes} minutes")
    now = datetime.now(timezone.utc)
    stamped = [f"{now - timedelta(minutes=3 * (i + 1)):%Y-%m-%d %H:%M:%S} {host} {ln}" for i, ln in enumerate(lines)]
    if keyword:
        filtered = [ln for ln in stamped if keyword.lower() in ln.lower()]
        stamped = filtered or stamped
    return stamped


def service_status(env: _Env, host: str, service: str = "") -> dict:
    service = service or _service_for(host)
    degraded = env.degraded.get(service, False)
    return {"host": host, "service": service, "state": "degraded" if degraded else "running"}


def account_status(env: _Env, username: str) -> dict:
    return {
        "username": username,
        "locked": env.locked,
        "password_expired": env.expired,
        "failed_logons_24h": 5 if env.locked else 0,
        "groups": ["domain users"] + (["vpn-users"] if env.in_vpn_group else []),
    }


def apply_action(env: _Env, tool_name: str, args: dict) -> None:
    """执行运维操作后更新模拟环境，后续查询能看到效果。"""
    if tool_name == "unlock_account":
        env.locked = False
    elif tool_name == "reset_password":
        env.locked = env.expired = False
    elif tool_name == "clear_print_queue":
        env.print_stuck = False
    elif tool_name == "restart_service":
        env.degraded[args.get("service", "")] = False
    elif tool_name == "grant_permission" and "vpn" in str(args.get("resource", "")).lower():
        env.in_vpn_group = True


def reset_for_tests() -> None:
    _envs.clear()
