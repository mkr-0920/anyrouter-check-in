import httpx

from scripts.probe_nodes import probe_nodes
from utils.mihomo import MihomoApi

NODES = ['香港 01', '美国 02', '日本 03']
PROXY_URL = 'http://127.0.0.1:7890'

MEMBERS = {
	'CHECKIN': {'type': 'Selector', 'now': 'AUTO', 'all': ['AUTO', *NODES]},
	'AUTO': {'type': 'URLTest', 'now': NODES[0], 'all': NODES},
	**{node: {'type': 'Shadowsocks', 'name': node} for node in NODES},
}

# GET /proxies 的响应外层信封
PROXIES_RESPONSE = {'proxies': MEMBERS}


def _mihomo(handler=None) -> MihomoApi:
	def default_handler(request: httpx.Request) -> httpx.Response:
		if request.method == 'PUT':
			return httpx.Response(204)
		return httpx.Response(200, json=PROXIES_RESPONSE)

	return MihomoApi(
		'http://127.0.0.1:9090',
		'secret',
		client=httpx.Client(transport=httpx.MockTransport(handler or default_handler)),
	)


def _client_factory(exit_ips: list[str | None]):
	"""按顺序为每个节点返回一个独立 client；None 表示该节点不可达。"""
	remaining = list(exit_ips)
	proxies_used: list[str] = []

	def factory(proxy_url: str) -> httpx.Client:
		proxies_used.append(proxy_url)
		exit_ip = remaining.pop(0) if remaining else None

		def handler(request: httpx.Request) -> httpx.Response:
			if exit_ip is None:
				raise httpx.ConnectError('unreachable')
			return httpx.Response(200, text=exit_ip)

		return httpx.Client(transport=httpx.MockTransport(handler))

	return factory, proxies_used


def test_probe_records_an_exit_ip_per_node():
	factory, _ = _client_factory(['1.1.1.1', '2.2.2.2', '3.3.3.3'])

	report = probe_nodes(_mihomo(), 'CHECKIN', PROXY_URL, client_factory=factory)

	assert [probe.node for probe in report.probes] == NODES
	assert [probe.exit_ip for probe in report.probes] == ['1.1.1.1', '2.2.2.2', '3.3.3.3']
	assert report.distinct_ips == ['1.1.1.1', '2.2.2.2', '3.3.3.3']


def test_probe_collapses_nodes_that_share_an_exit_ip():
	factory, _ = _client_factory(['1.1.1.1', '1.1.1.1', '2.2.2.2'])

	report = probe_nodes(_mihomo(), 'CHECKIN', PROXY_URL, client_factory=factory)

	assert report.distinct_ips == ['1.1.1.1', '2.2.2.2']


def test_probe_records_unreachable_nodes_without_counting_them():
	factory, _ = _client_factory(['1.1.1.1', None, '3.3.3.3'])

	report = probe_nodes(_mihomo(), 'CHECKIN', PROXY_URL, client_factory=factory)

	assert report.probes[1].exit_ip is None
	assert report.distinct_ips == ['1.1.1.1', '3.3.3.3']


def test_probe_continues_after_a_failed_switch():
	def handler(request: httpx.Request) -> httpx.Response:
		if request.method == 'PUT' and '美国 02' in request.content.decode():
			return httpx.Response(500)
		if request.method == 'PUT':
			return httpx.Response(204)
		return httpx.Response(200, json=PROXIES_RESPONSE)

	factory, _ = _client_factory(['1.1.1.1', '3.3.3.3'])

	report = probe_nodes(_mihomo(handler), 'CHECKIN', PROXY_URL, client_factory=factory)

	assert len(report.probes) == 3
	assert report.probes[1].exit_ip is None
	assert report.probes[1].error is not None
	assert report.probes[2].exit_ip == '3.3.3.3'


def test_probe_uses_a_fresh_client_per_node():
	# 复用连接会把隧道钉在旧节点上，测出来会全是同一个出口 IP
	factory, proxies_used = _client_factory(['1.1.1.1', '2.2.2.2', '3.3.3.3'])

	probe_nodes(_mihomo(), 'CHECKIN', PROXY_URL, client_factory=factory)

	assert proxies_used == [PROXY_URL] * len(NODES)
