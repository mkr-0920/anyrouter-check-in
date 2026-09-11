"""mihomo 控制接口：枚举节点、查看/切换分组的当前选择，并按账号轮转出口节点。"""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx

from utils.proxy import fetch_exit_ip

# mihomo 里非「真实节点」的条目类型：分组不能当作出口来选
GROUP_TYPES = frozenset({'Selector', 'URLTest', 'Fallback', 'LoadBalance', 'Relay'})

API_TIMEOUT = 5.0
EXIT_IP_TIMEOUT = 10.0
DEFAULT_GROUP = 'CHECKIN'
# 单次轮转最多探测几个候选：每个候选都要真实出网（最坏 5s 超时），
# 节点大面积失联时无上界会把整轮时间预算耗光
MAX_SCAN_PER_ROTATE = 8


class MihomoError(Exception):
	"""mihomo 控制接口调用失败。"""


class MihomoApi:
	"""mihomo external-controller 的极简客户端。"""

	def __init__(self, base_url: str, secret: str = '', *, client: httpx.Client | None = None) -> None:  # nosec B107
		self.base_url = base_url.rstrip('/')
		self._secret = secret
		self._client = client or httpx.Client(timeout=API_TIMEOUT)

	def _headers(self) -> dict[str, str]:
		if not self._secret:
			return {}
		return {'Authorization': f'Bearer {self._secret}'}

	def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
		try:
			response = self._client.request(method, f'{self.base_url}{path}', headers=self._headers(), **kwargs)
		except httpx.HTTPError as exc:
			raise MihomoError(f'{method} {path} failed: {exc}') from exc
		if response.status_code >= 400:
			raise MihomoError(f'{method} {path} failed: HTTP {response.status_code}')
		return response

	def proxies(self) -> dict:
		"""返回 /proxies 的完整内容（节点与分组都在里面）。"""
		payload = self._request('GET', '/proxies').json()
		if not isinstance(payload, dict):
			raise MihomoError('/proxies returned a non-object payload')
		return payload

	def _group_entry(self, group: str) -> dict:
		entry = self.proxies().get(group)
		if not isinstance(entry, dict):
			raise MihomoError(f'unknown proxy group: {group}')
		return entry

	def group_members(self, group: str) -> list[str]:
		"""返回分组里可手动选择的真实节点，排除嵌套的分组。"""
		proxies = self.proxies()
		entry = proxies.get(group)
		if not isinstance(entry, dict):
			raise MihomoError(f'unknown proxy group: {group}')
		return [name for name in entry.get('all', []) if proxies.get(name, {}).get('type') not in GROUP_TYPES]

	def current_node(self, group: str) -> str | None:
		"""返回分组当前选中的成员名。"""
		now = self._group_entry(group).get('now')
		return now if isinstance(now, str) else None

	def select_node(self, group: str, name: str) -> None:
		"""把分组切换到指定节点。"""
		self._request('PUT', f'/proxies/{group}', json={'name': name})


def load_mihomo_api() -> MihomoApi | None:
	"""从环境变量读取 mihomo 控制接口；未配置时返回 None。"""
	base_url = os.getenv('CHECKIN_MIHOMO_API', '').strip()
	if not base_url:
		return None
	return MihomoApi(base_url, os.getenv('CHECKIN_MIHOMO_SECRET', '').strip())


def _default_client_factory(proxy_url: str) -> httpx.Client:
	return httpx.Client(proxy=proxy_url, timeout=EXIT_IP_TIMEOUT)


class NodeRotator:
	"""按账号轮转出口节点，同一轮内尽量不重复使用同一个出口 IP。

	出口 IP 会被 WAF 按请求量限流，所以「换账号 = 换 IP」比在同一个 IP 上重试更有效。
	节点池绕完一圈后按「循环复用」处理并明确告警——否则「IP 不够」和「节点都好」
	在日志里长得一模一样。
	"""

	def __init__(
		self,
		api: MihomoApi,
		group: str,
		proxy_url: str,
		*,
		client_factory: Callable[[str], httpx.Client] = _default_client_factory,
	) -> None:
		self._api = api
		self._group = group
		self._proxy_url = proxy_url
		self._client_factory = client_factory
		self._candidates: list[str] | None = None
		self._cursor = 0
		self._used_ips: set[str] = set()

	@classmethod
	def from_env(cls) -> NodeRotator | None:
		"""按环境变量构建；未配置代理或 mihomo 控制接口时返回 None（功能整体关闭）。"""
		api = load_mihomo_api()
		proxy_url = os.getenv('CHECKIN_PROXY_URL', '').strip()
		if api is None or not proxy_url:
			return None
		return cls(api, DEFAULT_GROUP, proxy_url)

	def _load_candidates(self) -> list[str]:
		if self._candidates is None:
			try:
				self._candidates = self._api.group_members(self._group)
			except MihomoError as exc:
				print(f'[WARN] 无法读取节点列表，本轮不做出口轮转: {exc}')
				self._candidates = []
		return self._candidates

	def _probe_exit_ip(self) -> str | None:
		with self._client_factory(self._proxy_url) as client:
			return fetch_exit_ip(client)

	def rotate(self, label: str) -> str | None:
		"""为下一次尝试挑选并切换节点，返回出口 IP。

		任何一步失败都只降级为「沿用当前出口」，绝不抛异常——代理切换的问题
		不该变成跳过签到。返回 None 表示没能切换，调用方照常继续。
		"""
		candidates = self._load_candidates()
		if not candidates:
			return None

		fallback: tuple[int, str, str] | None = None
		for offset in range(min(len(candidates), MAX_SCAN_PER_ROTATE)):
			index = (self._cursor + offset) % len(candidates)
			node = candidates[index]
			try:
				self._api.select_node(self._group, node)
			except MihomoError as exc:
				print(f'[WARN] {label}: 切换节点 {node} 失败: {exc}')
				continue

			exit_ip = self._probe_exit_ip()
			if exit_ip is None:
				print(f'[WARN] {label}: 节点 {node} 不可用，换下一个')
				continue

			if exit_ip not in self._used_ips:
				self._commit(index, exit_ip, label, node)
				return exit_ip

			if fallback is None:
				fallback = (index, node, exit_ip)

		if fallback is None:
			print(f'[WARN] {label}: 没有可用节点，沿用当前出口')
			return None

		index, node, exit_ip = fallback
		print(f'[WARN] {label}: 节点池已绕回一圈，开始复用出口 IP {exit_ip}（{node}）')
		self._commit(index, exit_ip, label, node)
		return exit_ip

	def _commit(self, index: int, exit_ip: str, label: str, node: str) -> None:
		self._cursor = (index + 1) % len(self._candidates or [node])
		self._used_ips.add(exit_ip)
		print(f'[INFO] {label}: 出口节点 {node}（{exit_ip}）')
