"""v0.11.14 · 存储连接器主机白名单 + SSRF 校验。

项目管理员可创建连接器并填任意 endpoint/host，等于把"服务端发起连接"的能力下放，
因此用**超管主机白名单**收口：项目管理员只能连白名单内 host/endpoint。

校验在连接器创建、test、import 三处都要走（白名单可能在创建后被超管收紧，存量连接
在 test/import 时需复检）。DNS 解析为真实 IP 后再校验，缓解 DNS rebinding。

判定规则（解析 host → 逐个 IP 校验）：
  - HARD_BLOCK（loopback / link-local(含云元数据 169.254.169.254) / multicast / 未指定）：
    **永远拒绝**，即使被白名单覆盖。worker 在容器内，宿主机经 docker 网关 IP 可达，
    无需也不应放行 loopback。
  - 其余 IP：当 IP 落在某条白名单 CIDR/IP 条目内，**或** host 精确/后缀匹配某条白名单
    域名条目且该 IP 为公网地址 → 放行；否则拒绝。

白名单条目形态（存 system_settings.connector_host_allowlist，list[str]）：
  - CIDR / 单 IP：``10.0.3.0/24`` / ``192.168.1.50``（内网服务器走这里显式放行）
  - 精确域名：``oss-cn-hangzhou.aliyuncs.com``
  - 后缀域名（前导点）：``.aliyuncs.com`` 匹配任意子域

残留风险：boto3/paramiko 实际连接时会重新解析 DNS，真正的 IP pin 需按解析 IP 直连
（受 SNI / SFTP host key 制约），列为后续加固；当前在 test/import 紧邻连接处校验已大幅收敛。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.system_setting import SystemSetting
from app.services.system_settings_service import SystemSettingsService


class ConnectorHostDenied(Exception):
    """目标 host 未通过白名单 / SSRF 校验。"""


class ConnectorAllowlistInvalid(ValueError):
    """连接器白名单条目语法无效。"""


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_ALLOWLIST_ENTRIES = 256
_MAX_ALLOWLIST_ENTRY_LENGTH = 253


def _normalize_allowlist_entry(entry: str) -> str:
    value = entry.strip()
    if not value:
        return ""
    if len(value) > _MAX_ALLOWLIST_ENTRY_LENGTH:
        raise ConnectorAllowlistInvalid(
            f"白名单条目不能超过 {_MAX_ALLOWLIST_ENTRY_LENGTH} 个字符"
        )
    if "://" in value:
        raise ConnectorAllowlistInvalid("白名单条目不能包含 URL scheme")
    if "*" in value:
        raise ConnectorAllowlistInvalid(
            "白名单条目不支持通配符 *，请使用前导点后缀域名"
        )

    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass

    if "/" in value:
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ConnectorAllowlistInvalid(f"无效 CIDR: {value}") from exc

    if ":" in value:
        raise ConnectorAllowlistInvalid("域名条目不能包含端口")
    if "\\" in value:
        raise ConnectorAllowlistInvalid("域名条目不能包含路径")

    suffix = value.startswith(".")
    domain = (value[1:] if suffix else value).rstrip(".")
    if not domain:
        raise ConnectorAllowlistInvalid("后缀域名不能为空")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ConnectorAllowlistInvalid(f"无效域名: {value}") from exc
    if len(ascii_domain) > _MAX_ALLOWLIST_ENTRY_LENGTH:
        raise ConnectorAllowlistInvalid(
            f"域名不能超过 {_MAX_ALLOWLIST_ENTRY_LENGTH} 个字符"
        )
    labels = ascii_domain.split(".")
    if any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ConnectorAllowlistInvalid(f"无效域名: {value}")
    return f".{ascii_domain}" if suffix else ascii_domain


def normalize_allowlist(entries: list[str]) -> list[str]:
    """校验、规范化并按首次出现顺序去重白名单条目。"""
    if len(entries) > _MAX_ALLOWLIST_ENTRIES:
        raise ConnectorAllowlistInvalid(f"白名单最多 {_MAX_ALLOWLIST_ENTRIES} 条")
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        value = _normalize_allowlist_entry(entry)
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def extract_host(endpoint_or_host: str) -> str:
    """从 endpoint URL 或裸 host[:port] 取出 host。"""
    raw = (endpoint_or_host or "").strip()
    if not raw:
        raise ConnectorHostDenied("目标地址为空")
    # 带 scheme 的 endpoint：用 urlparse
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname
        if not host:
            raise ConnectorHostDenied(f"无法解析 host: {endpoint_or_host}")
        return host
    # 裸 host[:port]——剥端口（注意 IPv6 字面量带 [])
    if raw.startswith("[") and "]" in raw:
        return raw[1 : raw.index("]")]
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


def _resolve_ips(host: str) -> list[ipaddress._BaseAddress]:
    """host → 去重 IP 列表。host 本身是 IP 字面量时直接返回。"""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ConnectorHostDenied(f"DNS 解析失败: {host} ({e})") from e
    ips: list[ipaddress._BaseAddress] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            ips.append(ipaddress.ip_address(addr))
    if not ips:
        raise ConnectorHostDenied(f"DNS 无解析结果: {host}")
    return ips


def _is_hard_blocked(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_loopback
        or ip.is_link_local  # 含 169.254.169.254 云元数据
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _host_matches_name_entries(host: str, allowlist: list[str]) -> bool:
    h = host.lower().rstrip(".")
    for entry in allowlist:
        e = entry.strip().lower()
        if not e or "/" in e:
            continue
        # 纯 IP 条目交给 CIDR 分支
        try:
            ipaddress.ip_address(e)
            continue
        except ValueError:
            pass
        if e.startswith("."):
            if h == e[1:] or h.endswith(e):
                return True
        elif h == e:
            return True
    return False


def _ip_in_cidr_entries(ip: ipaddress._BaseAddress, allowlist: list[str]) -> bool:
    for entry in allowlist:
        e = entry.strip()
        if not e:
            continue
        try:
            net = ipaddress.ip_network(e, strict=False)
        except ValueError:
            continue
        if ip.version == net.version and ip in net:
            return True
    return False


def assert_host_allowed(host_or_endpoint: str, allowlist: list[str]) -> None:
    """校验目标地址是否被白名单允许；不允许则抛 ConnectorHostDenied。"""
    resolve_allowed_ips(host_or_endpoint, allowlist)


def resolve_allowed_ips(host_or_endpoint: str, allowlist: list[str]) -> list[str]:
    """Return the exact validated addresses for clients that can pin a socket."""
    if not allowlist:
        raise ConnectorHostDenied(
            "未配置连接器主机白名单，请联系超级管理员先配置允许的主机"
        )
    host = extract_host(host_or_endpoint)
    name_ok = _host_matches_name_entries(host, allowlist)
    addresses = _resolve_ips(host)
    for ip in addresses:
        if _is_hard_blocked(ip):
            raise ConnectorHostDenied(
                f"目标解析到受限地址（loopback/link-local 等），拒绝: {host} → {ip}"
            )
        if _ip_in_cidr_entries(ip, allowlist):
            continue
        if name_ok and ip.is_global:
            continue
        raise ConnectorHostDenied(
            f"目标不在白名单内: {host} → {ip}（需超管将其加入连接器主机白名单）"
        )
    return [str(ip) for ip in addresses]


async def get_allowlist(db: AsyncSession) -> list[str]:
    raw = await SystemSettingsService.get(db, "connector_host_allowlist")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


async def get_allowlist_state(
    db: AsyncSession,
) -> tuple[list[str], Literal["database", "environment"]]:
    """返回当前有效白名单及其来源，显式空数组仍视为 DB override。"""
    entries = await get_allowlist(db)
    row = await db.scalar(
        select(SystemSetting).where(SystemSetting.key == "connector_host_allowlist")
    )
    source: Literal["database", "environment"] = (
        "database" if row is not None and row.value_json is not None else "environment"
    )
    return entries, source


async def assert_connection_target_allowed(
    db: AsyncSession, host_or_endpoint: str
) -> None:
    """便捷封装：读白名单 + 校验。create/test/import 入口统一调用。"""
    allowlist = await get_allowlist(db)
    assert_host_allowed(host_or_endpoint, allowlist)
