"""代理配置：读取环境变量并供浏览器 / HTTP 客户端使用。"""

from __future__ import annotations

import os

import httpx

# 出口 IP 探测目标：纯文本返回，用于诊断「代理节点是否中途切换」
IP_ECHO_URL = 'https://api.ipify.org'
IP_ECHO_TIMEOUT = 5.0


def get_proxy_server(*, use_proxy: bool = True) -> str | None:
	"""按平台配置读取 CHECKIN_PROXY_URL；use_proxy=False 时不返回代理地址。"""
	if not use_proxy:
		return None
	server = os.getenv('CHECKIN_PROXY_URL', '').strip()
	return server or None


def get_playwright_proxy(*, use_proxy: bool = True) -> dict[str, str] | None:
	server = get_proxy_server(use_proxy=use_proxy)
	if not server:
		return None
	return {'server': server}


def fetch_exit_ip(client: httpx.Client) -> str | None:
	"""用给定客户端查询出口 IP，失败一律返回 None。

	纯诊断用途：任何异常都不能影响签到主流程。
	"""
	try:
		response = client.get(IP_ECHO_URL, timeout=IP_ECHO_TIMEOUT)
	except Exception:  # nosec B110 - 诊断失败不该中断签到
		return None
	if response.status_code != 200:
		return None
	address = response.text.strip()
	if not address or len(address) > 45 or any(char.isspace() for char in address):
		return None
	return address
