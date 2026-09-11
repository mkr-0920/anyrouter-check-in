import httpx

from utils.mihomo import MihomoError, NodeRotator

GROUP = 'CHECKIN'
PROXY_URL = 'http://127.0.0.1:7890'


class FakeApi:
	"""按节点名记录当前选择；切换失败可通过 failures 指定。"""

	def __init__(self, nodes: list[str], failures: set[str] | None = None) -> None:
		self.nodes = nodes
		self.failures = failures or set()
		self.selected: str | None = None
		self.select_calls: list[str] = []

	def group_members(self, group: str) -> list[str]:
		return list(self.nodes)

	def select_node(self, group: str, name: str) -> None:
		if name in self.failures:
			raise MihomoError(f'cannot select {name}')
		self.selected = name
		self.select_calls.append(name)


class BrokenApi(FakeApi):
	def group_members(self, group: str) -> list[str]:
		raise MihomoError('control api unreachable')


def _client_factory(api: FakeApi, exit_ips: dict[str, str | None]):
	"""出口 IP 取决于当前选中的节点——和真实代理的行为一致。"""

	def factory(proxy_url: str) -> httpx.Client:
		exit_ip = exit_ips.get(api.selected or '')

		def handler(request: httpx.Request) -> httpx.Response:
			if exit_ip is None:
				raise httpx.ConnectError('unreachable')
			return httpx.Response(200, text=exit_ip)

		return httpx.Client(transport=httpx.MockTransport(handler))

	return factory


def _identity(items: list[str]) -> None:
	"""不打乱——让测试里的扫描顺序可预测。"""


def _rotator(
	api: FakeApi,
	exit_ips: dict[str, str | None],
	*,
	direct_ip: str | None = None,
	shuffle=_identity,
) -> NodeRotator:
	return NodeRotator(
		api,
		GROUP,
		PROXY_URL,
		client_factory=_client_factory(api, exit_ips),
		shuffle=shuffle,
		direct_ip_provider=lambda: direct_ip,
	)


def test_rotate_selects_a_node_and_reports_its_exit_ip():
	api = FakeApi(['A', 'B'])

	rotator = _rotator(api, {'A': '1.1.1.1', 'B': '2.2.2.2'})

	assert rotator.rotate('Account 1') == '1.1.1.1'
	assert api.selected == 'A'
	assert rotator.rotate('Account 2') == '2.2.2.2'
	assert api.selected == 'B'


def test_rotate_prefers_nodes_whose_exit_ip_is_still_unused():
	api = FakeApi(['A', 'B', 'C'])
	# C 与 A 是同一个出口 IP（机场常见的「同机多端口」）
	rotator = _rotator(api, {'A': '1.1.1.1', 'B': '2.2.2.2', 'C': '1.1.1.1'})

	assert rotator.rotate('Account 1') == '1.1.1.1'
	assert rotator.rotate('Account 2') == '2.2.2.2'


def test_rotate_skips_nodes_it_cannot_reach():
	api = FakeApi(['A', 'B'])
	rotator = _rotator(api, {'A': None, 'B': '2.2.2.2'})

	assert rotator.rotate('Account 1') == '2.2.2.2'
	assert api.selected == 'B'


def test_rotate_survives_a_failed_switch():
	api = FakeApi(['A', 'B'], failures={'A'})
	rotator = _rotator(api, {'A': '1.1.1.1', 'B': '2.2.2.2'})

	assert rotator.rotate('Account 1') == '2.2.2.2'


def test_rotate_reuses_an_exit_ip_once_the_pool_is_exhausted(capsys):
	api = FakeApi(['A', 'B'])
	rotator = _rotator(api, {'A': '1.1.1.1', 'B': '2.2.2.2'})

	rotator.rotate('Account 1')
	rotator.rotate('Account 2')
	reused = rotator.rotate('Account 3')

	assert reused in {'1.1.1.1', '2.2.2.2'}
	output = capsys.readouterr().out
	assert 'Account 3' in output
	assert '复用' in output


def test_rotate_returns_none_when_no_node_is_usable(capsys):
	api = FakeApi(['A', 'B'])
	rotator = _rotator(api, {'A': None, 'B': None})

	assert rotator.rotate('Account 1') is None
	assert 'Account 1' in capsys.readouterr().out


def test_rotate_returns_none_when_the_node_list_cannot_be_loaded(capsys):
	rotator = _rotator(BrokenApi([]), {})

	assert rotator.rotate('Account 1') is None
	# 一次性告警，说明为什么本轮没有轮转；此时还不知道是哪个账号
	assert '无法读取节点列表' in capsys.readouterr().out


def test_rotate_bounds_how_many_nodes_it_probes(capsys):
	# 每个候选都要真实出网探测（最坏 5s 超时），不设上界会在节点大面积失联时拖垮整轮预算
	api = FakeApi([f'N{index}' for index in range(20)])
	rotator = _rotator(api, {})

	assert rotator.rotate('Account 1') is None
	assert len(api.select_calls) <= 8
	assert 'Account 1' in capsys.readouterr().out


def test_rotate_skips_nodes_that_exit_via_the_runner_ip(capsys):
	# 机场会在订阅里塞「剩余流量」这类伪节点，它们实际是直连 —— 出口就是 runner 自己的 IP
	api = FakeApi(['剩余流量：189.84 GB', 'B'])
	rotator = _rotator(api, {'剩余流量：189.84 GB': '9.9.9.9', 'B': '2.2.2.2'}, direct_ip='9.9.9.9')

	assert rotator.rotate('Account 1') == '2.2.2.2'
	assert '直连' in capsys.readouterr().out


def test_rotate_reuses_without_reprobing_nodes_it_already_knows(capsys):
	# 两个节点都已探明且出口都被占用 —— 复用不该再花任何一次探测
	api = FakeApi(['A', 'B'])
	rotator = _rotator(api, {'A': '1.1.1.1', 'B': '2.2.2.2'})

	rotator.rotate('Account 1')
	rotator.rotate('Account 2')
	reused = rotator.rotate('Account 3')

	assert reused in {'1.1.1.1', '2.2.2.2'}
	assert api.select_calls == ['A', 'B']
	assert '复用' in capsys.readouterr().out


def test_shuffling_stops_one_region_from_exhausting_the_probe_budget():
	# 订阅按地区排列，同地区节点常共用出口 IP。线性扫描会把探测预算烧在同一个 IP 簇里，
	# 扫不到后面的新鲜 IP —— 这正是实测中「只用掉 5 个 IP 就提示绕回一圈」的原因。
	nodes = [f'东京{index}号' for index in range(12)] + ['凤凰城1号']
	exit_ips = {node: '1.1.1.1' for node in nodes[:12]}
	exit_ips['凤凰城1号'] = '2.2.2.2'

	linear = _rotator(FakeApi(nodes), exit_ips)
	assert linear.rotate('Account 1') == '1.1.1.1'
	assert linear.rotate('Account 2') == '1.1.1.1'  # 预算烧光，只能复用同一个 IP

	def fresh_first(items: list[str]) -> None:
		items.sort(key=lambda name: name != '凤凰城1号')

	shuffled = _rotator(FakeApi(nodes), exit_ips, shuffle=fresh_first)
	assert shuffled.rotate('Account 1') == '2.2.2.2'


def test_rotator_shuffles_the_candidate_order_exactly_once():
	calls = []

	def spy(items: list[str]) -> None:
		calls.append(list(items))

	api = FakeApi(['A', 'B'])
	rotator = _rotator(api, {'A': '1.1.1.1', 'B': '2.2.2.2'}, shuffle=spy)

	rotator.rotate('Account 1')
	rotator.rotate('Account 2')

	assert calls == [['A', 'B']]


def test_rotate_reports_unusable_nodes_only_once(capsys):
	api = FakeApi(['A', 'B'])
	rotator = _rotator(api, {'A': None, 'B': '2.2.2.2'})

	rotator.rotate('Account 1')
	first = capsys.readouterr().out
	rotator.rotate('Account 2')
	second = capsys.readouterr().out

	assert '节点 A 不可用' in first
	assert '节点 A 不可用' not in second


def test_node_rotator_from_env_returns_none_without_configuration(monkeypatch):
	monkeypatch.delenv('CHECKIN_MIHOMO_API', raising=False)
	monkeypatch.setenv('CHECKIN_PROXY_URL', PROXY_URL)

	assert NodeRotator.from_env() is None


def test_node_rotator_from_env_returns_none_without_a_proxy_url(monkeypatch):
	monkeypatch.setenv('CHECKIN_MIHOMO_API', 'http://127.0.0.1:9090')
	monkeypatch.delenv('CHECKIN_PROXY_URL', raising=False)

	assert NodeRotator.from_env() is None


def test_node_rotator_from_env_builds_a_rotator_when_configured(monkeypatch):
	monkeypatch.setenv('CHECKIN_MIHOMO_API', 'http://127.0.0.1:9090')
	monkeypatch.setenv('CHECKIN_PROXY_URL', PROXY_URL)

	assert isinstance(NodeRotator.from_env(), NodeRotator)
