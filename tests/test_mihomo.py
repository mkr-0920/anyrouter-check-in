import json

import httpx
import pytest

from utils.mihomo import MihomoApi, MihomoError, load_mihomo_api

MEMBERS = {
	'CHECKIN': {'type': 'Selector', 'now': 'AUTO', 'all': ['AUTO', '香港 01', '美国 02']},
	'AUTO': {'type': 'URLTest', 'now': '香港 01', 'all': ['香港 01', '美国 02']},
	'香港 01': {'type': 'Shadowsocks', 'name': '香港 01'},
	'美国 02': {'type': 'Vmess', 'name': '美国 02'},
}

# GET /proxies 返回的是包了一层的信封，条目本身在 proxies 键下面
PROXIES_RESPONSE = {'proxies': MEMBERS}


def _api(handler, *, base_url: str = 'http://127.0.0.1:9090', secret: str = 's3cret') -> MihomoApi:
	return MihomoApi(base_url, secret, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _ok_handler(request: httpx.Request) -> httpx.Response:
	return httpx.Response(200, json=PROXIES_RESPONSE)


def test_proxies_unwraps_the_response_envelope():
	assert _api(_ok_handler).proxies() == MEMBERS


def test_proxies_sends_bearer_token():
	seen = {}

	def handler(request: httpx.Request) -> httpx.Response:
		seen['url'] = str(request.url)
		seen['auth'] = request.headers.get('Authorization')
		return httpx.Response(200, json=PROXIES_RESPONSE)

	payload = _api(handler).proxies()

	assert payload['CHECKIN']['now'] == 'AUTO'
	assert seen['url'] == 'http://127.0.0.1:9090/proxies'
	assert seen['auth'] == 'Bearer s3cret'


def test_proxies_omits_authorization_when_secret_is_empty():
	seen = {}

	def handler(request: httpx.Request) -> httpx.Response:
		seen['auth'] = request.headers.get('Authorization')
		return httpx.Response(200, json=PROXIES_RESPONSE)

	_api(handler, secret='').proxies()

	assert seen['auth'] is None


def test_group_members_excludes_nested_groups():
	assert _api(_ok_handler).group_members('CHECKIN') == ['香港 01', '美国 02']


def test_group_members_raises_for_unknown_group():
	with pytest.raises(MihomoError):
		_api(_ok_handler).group_members('NOPE')


def test_unknown_group_error_lists_what_is_actually_available():
	# 失败时必须带上现场证据，否则分不清「分组名写错」和「响应结构变了」
	with pytest.raises(MihomoError) as excinfo:
		_api(_ok_handler).group_members('NOPE')

	assert 'CHECKIN' in str(excinfo.value)


def test_current_node_returns_the_selected_member():
	assert _api(_ok_handler).current_node('CHECKIN') == 'AUTO'


def test_select_node_puts_the_node_name():
	seen = {}

	def handler(request: httpx.Request) -> httpx.Response:
		seen['method'] = request.method
		seen['url'] = str(request.url)
		seen['body'] = request.content.decode()
		return httpx.Response(204)

	_api(handler).select_node('CHECKIN', '美国 02')

	assert seen['method'] == 'PUT'
	assert seen['url'] == 'http://127.0.0.1:9090/proxies/CHECKIN'
	assert json.loads(seen['body']) == {'name': '美国 02'}


def test_select_node_raises_on_api_error():
	def handler(request: httpx.Request) -> httpx.Response:
		return httpx.Response(404, json={'message': 'Group not found'})

	with pytest.raises(MihomoError):
		_api(handler).select_node('CHECKIN', '美国 02')


def test_transport_error_becomes_mihomo_error():
	def handler(request: httpx.Request) -> httpx.Response:
		raise httpx.ConnectError('connection refused')

	with pytest.raises(MihomoError):
		_api(handler).proxies()


def test_load_mihomo_api_returns_none_when_not_configured(monkeypatch):
	monkeypatch.delenv('CHECKIN_MIHOMO_API', raising=False)

	assert load_mihomo_api() is None


def test_load_mihomo_api_reads_environment(monkeypatch):
	monkeypatch.setenv('CHECKIN_MIHOMO_API', 'http://127.0.0.1:9090')
	monkeypatch.setenv('CHECKIN_MIHOMO_SECRET', 'abc123')

	api = load_mihomo_api()

	assert api is not None
	assert api.base_url == 'http://127.0.0.1:9090'
