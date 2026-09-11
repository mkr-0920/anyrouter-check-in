"""mihomo 控制接口：枚举节点、查看/切换分组的当前选择，并按账号轮转出口节点。"""

from __future__ import annotations

import os
import random
from collections.abc import Callable

import httpx

from utils.proxy import fetch_exit_ip

# mihomo 里非「真实节点」的条目类型：分组不能当作出口来选
GROUP_TYPES = frozenset({'Selector', 'URLTest', 'Fallback', 'LoadBalance', 'Relay'})

API_TIMEOUT = 5.0
EXIT_IP_TIMEOUT = 10.0
DEFAULT_GROUP = 'CHECKIN'
# 单次轮转最多做几次出网探测（不是最多看几个候选）：每个探测最坏 5s 超时，
# 无上界会在节点大面积失联时耗光整轮时间预算
MAX_PROBES_PER_ROTATE = 8


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
		"""返回 /proxies 的条目表（节点与分组）。

		注意响应是包了一层的信封：{"proxies": {名字: 条目}}，而 /proxies/{name}
		才是直接返回条目本身。
		"""
		payload = self._request('GET', '/proxies').json()
		if not isinstance(payload, dict):
			raise MihomoError('/proxies returned a non-object payload')
		entries = payload.get('proxies')
		if not isinstance(entries, dict):
			raise MihomoError(f'/proxies payload has no "proxies" object, keys={sorted(payload)}')
		return entries

	def _group_entry(self, group: str, proxies: dict | None = None) -> dict:
		entries = self.proxies() if proxies is None else proxies
		entry = entries.get(group)
		if not isinstance(entry, dict):
			# 带上现场证据，否则分不清「分组名写错」和「响应结构变了」
			raise MihomoError(f'unknown proxy group: {group} (available: {sorted(entries)[:10]})')
		return entry

	def group_members(self, group: str) -> list[str]:
		"""返回分组里可手动选择的真实节点，排除嵌套的分组。"""
		proxies = self.proxies()
		entry = self._group_entry(group, proxies)
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


def _default_direct_ip_provider() -> str | None:
	"""不加代理查 runner 自己的出口 IP，用于识别伪节点。"""
	with httpx.Client(timeout=EXIT_IP_TIMEOUT) as client:
		return fetch_exit_ip(client)


class NodeRotator:
	"""按账号轮转出口节点，同一轮内尽量不重复使用同一个出口 IP。

	出口 IP 会被 WAF 按请求量限流，所以「换账号 = 换 IP」比在同一个 IP 上重试更有效。
	节点池绕完一圈后按「循环复用」处理并明确告警——否则「IP 不够」和「节点都好」
	在日志里长得一模一样。

	两处细节都来自实测：订阅按地区排列、同地区节点常共用出口 IP（东京1号/2号实测同
	IP），所以候选要打乱，否则探测预算会全烧在同一个 IP 簇里；机场还会往订阅里塞
	「剩余流量」这类伪节点，它们实际是直连（出口等于 runner 自己的 IP），必须排除。
	"""

	def __init__(
		self,
		api: MihomoApi,
		group: str,
		proxy_url: str,
		*,
		client_factory: Callable[[str], httpx.Client] = _default_client_factory,
		shuffle: Callable[[list[str]], None] = random.shuffle,
		direct_ip_provider: Callable[[], str | None] = _default_direct_ip_provider,
	) -> None:
		self._api = api
		self._group = group
		self._proxy_url = proxy_url
		self._client_factory = client_factory
		self._shuffle = shuffle
		self._direct_ip_provider = direct_ip_provider
		self._order: list[str] | None = None
		self._cursor = 0
		self._used_ips: set[str] = set()
		self._known_ips: dict[str, str] = {}
		self._unusable: set[str] = set()
		self._runner_ip: str | None = None
		self._runner_ip_resolved = False

	@classmethod
	def from_env(cls) -> NodeRotator | None:
		"""按环境变量构建；未配置代理或 mihomo 控制接口时返回 None（功能整体关闭）。"""
		api = load_mihomo_api()
		proxy_url = os.getenv('CHECKIN_PROXY_URL', '').strip()
		if api is None or not proxy_url:
			return None
		return cls(api, DEFAULT_GROUP, proxy_url)

	def _load_order(self) -> list[str]:
		if self._order is None:
			try:
				candidates = self._api.group_members(self._group)
			except MihomoError as exc:
				print(f'[WARN] 无法读取节点列表，本轮不做出口轮转: {exc}')
				candidates = []
			self._shuffle(candidates)
			self._order = candidates
		return self._order

	def _runner_exit_ip(self) -> str | None:
		if not self._runner_ip_resolved:
			self._runner_ip = self._direct_ip_provider()
			self._runner_ip_resolved = True
		return self._runner_ip

	def _probe_exit_ip(self) -> str | None:
		with self._client_factory(self._proxy_url) as client:
			return fetch_exit_ip(client)

	def rotate(self, label: str) -> str | None:
		"""为下一次尝试挑选并切换节点，返回出口 IP。

		任何一步失败都只降级为「沿用当前出口」，绝不抛异常——代理切换的问题
		不该变成跳过签到。返回 None 表示没能切换，调用方照常继续。
		"""
		order = self._load_order()
		if not order:
			return None

		runner_ip = self._runner_exit_ip()
		probes = 0
		fallback: tuple[int, str, str] | None = None

		for offset in range(len(order)):
			index = (self._cursor + offset) % len(order)
			node = order[index]

			if node in self._unusable:
				continue

			known_ip = self._known_ips.get(node)
			if known_ip is not None and known_ip in self._used_ips:
				# 已知出口已被占用：免费跳过，不浪费探测预算
				if fallback is None:
					fallback = (index, node, known_ip)
				continue

			if probes >= MAX_PROBES_PER_ROTATE:
				break
			probes += 1

			try:
				self._api.select_node(self._group, node)
			except MihomoError as exc:
				print(f'[WARN] {label}: 切换节点 {node} 失败: {exc}')
				continue

			exit_ip = self._probe_exit_ip()
			if exit_ip is None:
				print(f'[WARN] {label}: 节点 {node} 不可用，换下一个')
				self._unusable.add(node)
				continue

			self._known_ips[node] = exit_ip

			if runner_ip is not None and exit_ip == runner_ip:
				print(f'[WARN] {label}: 节点 {node} 的出口就是 runner 自己的 IP（实为直连），排除')
				self._unusable.add(node)
				continue

			if exit_ip not in self._used_ips:
				self._cursor = (index + 1) % len(order)
				self._used_ips.add(exit_ip)
				print(f'[INFO] {label}: 出口节点 {node}（{exit_ip}）')
				return exit_ip

			if fallback is None:
				fallback = (index, node, exit_ip)

		if fallback is None:
			print(f'[WARN] {label}: 没有可用节点，沿用当前出口')
			return None

		index, node, exit_ip = fallback
		print(f'[WARN] {label}: 节点池已绕回一圈，开始复用出口 IP {exit_ip}（{node}）')
		self._cursor = (index + 1) % len(order)
		self._used_ips.add(exit_ip)
		return exit_ip
