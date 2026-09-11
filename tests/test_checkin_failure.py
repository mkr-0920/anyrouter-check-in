import time

import httpx
import pytest

import checkin
from checkin import AccountResult, FailureKind, check_in_account_with_retry, run_check_in_requests
from utils.config import AccountConfig, AppConfig, ProviderConfig
from utils.retry import RetrySettings, load_retry_settings


def test_retry_settings_defaults_match_the_documented_budget():
	settings = load_retry_settings()

	assert settings.http_max_attempts == 3
	assert settings.http_base_delay == 1.0
	assert settings.http_max_delay == 8.0
	assert settings.account_max_attempts == 3
	assert settings.account_retry_delay == 5.0
	assert settings.total_budget == 900.0


def test_retry_settings_read_environment_overrides(monkeypatch):
	monkeypatch.setenv('CHECKIN_HTTP_MAX_ATTEMPTS', '5')
	monkeypatch.setenv('CHECKIN_HTTP_RETRY_BASE_DELAY_MS', '250')
	monkeypatch.setenv('CHECKIN_ACCOUNT_MAX_ATTEMPTS', '4')
	monkeypatch.setenv('CHECKIN_TOTAL_TIMEOUT_SEC', '60')

	settings = load_retry_settings()

	assert settings.http_max_attempts == 5
	assert settings.http_base_delay == 0.25
	assert settings.account_max_attempts == 4
	assert settings.total_budget == 60.0


def test_retry_settings_ignore_invalid_values(monkeypatch):
	monkeypatch.setenv('CHECKIN_HTTP_MAX_ATTEMPTS', 'not-a-number')
	monkeypatch.setenv('CHECKIN_TOTAL_TIMEOUT_SEC', '')

	settings = load_retry_settings()

	assert settings.http_max_attempts == 3
	assert settings.total_budget == 900.0


def test_retry_settings_allow_disabling_the_total_budget(monkeypatch):
	monkeypatch.setenv('CHECKIN_TOTAL_TIMEOUT_SEC', '0')

	assert load_retry_settings().total_budget is None


def test_retry_settings_ignore_unparseable_delay(monkeypatch):
	monkeypatch.setenv('CHECKIN_HTTP_RETRY_BASE_DELAY_MS', 'not-a-number')

	assert load_retry_settings().http_base_delay == 1.0


class FakeClient:
	"""按顺序返回预置响应或抛出预置异常，并记录调用次数。"""

	def __init__(self, *responses):
		self._responses = list(responses)
		self.calls = 0

	def request(self, method, url, **kwargs):
		self.calls += 1
		item = self._responses.pop(0)
		if isinstance(item, Exception):
			raise item
		assert isinstance(item, httpx.Response)
		return item


def fast_settings(**overrides) -> RetrySettings:
	base = {
		'http_max_attempts': 3,
		'http_base_delay': 0.0,
		'http_max_delay': 0.0,
		'account_max_attempts': 2,
		'account_retry_delay': 0.0,
		'total_budget': 30.0,
	}
	base.update(overrides)
	return RetrySettings(**base)


def test_get_user_info_retries_transport_error_then_succeeds(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import get_user_info

	client = FakeClient(
		httpx.ConnectError('connection reset'),
		httpx.Response(200, json={'success': True, 'data': {'quota': 500000, 'used_quota': 0}}),
	)

	info = get_user_info(client, {}, 'https://example.com/api/user/self', fast_settings())

	assert client.calls == 2
	assert info['success'] is True
	assert info['quota'] == 1.0


def test_get_user_info_raises_permanent_error_on_auth_failure(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import get_user_info
	from utils.retry import PermanentError

	client = FakeClient(httpx.Response(401))

	with pytest.raises(PermanentError):
		get_user_info(client, {}, 'https://example.com/api/user/self', fast_settings())

	assert client.calls == 1


def test_get_user_info_gives_up_after_exhausting_retries(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import get_user_info

	client = FakeClient(
		httpx.ConnectError('down'),
		httpx.ConnectError('down'),
		httpx.ConnectError('down'),
	)

	with pytest.raises(httpx.ConnectError):
		get_user_info(client, {}, 'https://example.com/api/user/self', fast_settings())

	assert client.calls == 3


def test_get_user_info_retries_html_body_from_proxy(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import get_user_info

	client = FakeClient(
		httpx.Response(200, text='<html>proxy authentication required</html>'),
		httpx.Response(200, json={'success': True, 'data': {'quota': 0, 'used_quota': 0}}),
	)

	info = get_user_info(client, {}, 'https://example.com/api/user/self', fast_settings())

	assert client.calls == 2
	assert info['success'] is True


def test_get_user_info_reports_plain_client_error_without_retrying(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import get_user_info

	client = FakeClient(httpx.Response(404))

	info = get_user_info(client, {}, 'https://example.com/api/user/self', fast_settings())

	assert client.calls == 1
	assert info['success'] is False
	assert '404' in info['error']


def _provider() -> ProviderConfig:
	return ProviderConfig(
		name='anyrouter',
		domain='https://anyrouter.top',
		sign_in_path='/api/user/sign_in',
	)


def test_execute_check_in_retries_transport_error_then_succeeds(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import execute_check_in

	client = FakeClient(
		httpx.ReadTimeout('read timed out'),
		httpx.Response(200, json={'ret': 1}),
	)

	assert execute_check_in(client, 'Account 1', _provider(), {}, fast_settings()) is True
	assert client.calls == 2


def test_execute_check_in_treats_already_checked_in_as_success(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import execute_check_in

	client = FakeClient(httpx.Response(200, json={'ret': 0, 'msg': '今天已经签到'}))
	settings = fast_settings(http_max_attempts=1)

	assert execute_check_in(client, 'Account 1', _provider(), {}, settings) is True
	assert client.calls == 1


def test_execute_check_in_falls_back_to_body_text_when_json_is_broken(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import execute_check_in

	client = FakeClient(
		httpx.Response(200, text='{"msg": "success"} trailing garbage'),
		httpx.Response(200, text='{"msg": "success"} trailing garbage'),
		httpx.Response(200, text='{"msg": "success"} trailing garbage'),
	)

	assert execute_check_in(client, 'Account 1', _provider(), {}, fast_settings()) is True
	assert client.calls == 3


def test_execute_check_in_propagates_retryable_status_after_retries(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import execute_check_in
	from utils.retry import RetryableError

	client = FakeClient(
		httpx.Response(503),
		httpx.Response(503),
		httpx.Response(503),
	)

	with pytest.raises(RetryableError):
		execute_check_in(client, 'Account 1', _provider(), {}, fast_settings())

	assert client.calls == 3


def test_execute_check_in_reports_non_retryable_status(mocker):
	mocker.patch('utils.retry._jitter', return_value=0.0)
	from checkin import execute_check_in

	client = FakeClient(httpx.Response(404))
	settings = fast_settings(http_max_attempts=1)

	assert execute_check_in(client, 'Account 1', _provider(), {}, settings) is False
	assert client.calls == 1


def test_run_check_in_requests_logs_the_proxy_exit_ip(monkeypatch, capsys):
	monkeypatch.setenv('CHECKIN_PROXY_URL', 'http://127.0.0.1:7890')
	hosts = []

	def handler(request: httpx.Request) -> httpx.Response:
		hosts.append(request.url.host)
		if request.url.host == 'api.ipify.org':
			return httpx.Response(200, text='203.0.113.7\n')
		return httpx.Response(200, json={'success': True, 'data': {'quota': 0, 'used_quota': 0}})

	real_client = httpx.Client
	monkeypatch.setattr(
		checkin.httpx,
		'Client',
		lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
	)

	provider = ProviderConfig(
		name='agentrouter',
		domain='https://agentrouter.org',
		sign_in_path=None,
		use_proxy=True,
	)
	result = run_check_in_requests(
		{'session': 'x'},
		AccountConfig(cookies={'session': 'x'}, name='Account 1'),
		'Account 1',
		provider,
		use_proxy=True,
	)

	assert result.success is True
	assert 'api.ipify.org' in hosts
	assert 'Proxy exit IP: 203.0.113.7' in capsys.readouterr().out


def _transient(reason: str = 'connection reset') -> AccountResult:
	return AccountResult(False, failure_kind=FailureKind.TRANSIENT, reason=reason)


def _succeeded() -> AccountResult:
	return AccountResult(True, before={'success': True}, after={'success': True})


def _patch_account(monkeypatch, *results):
	"""按顺序返回预置 AccountResult，返回记录调用次数的列表。"""
	calls = []

	async def fake(account, index, app_config):
		calls.append(1)
		return results[min(len(calls) - 1, len(results) - 1)]

	monkeypatch.setattr(checkin, 'check_in_account', fake)
	return calls


@pytest.fixture
def account() -> AccountConfig:
	return AccountConfig(cookies={'session': 'x'}, name='Account 1')


@pytest.fixture
def retry_env(monkeypatch):
	monkeypatch.setenv('CHECKIN_ACCOUNT_MAX_ATTEMPTS', '2')
	monkeypatch.setenv('CHECKIN_ACCOUNT_RETRY_DELAY_MS', '0')


async def test_retries_a_transient_account_failure(monkeypatch, account, retry_env):
	calls = _patch_account(monkeypatch, _transient(), _succeeded())

	result = await check_in_account_with_retry(account, 0, AppConfig(providers={}), deadline=None)

	assert len(calls) == 2
	assert result.success is True


async def test_does_not_retry_an_auth_failure(monkeypatch, account, retry_env):
	calls = _patch_account(monkeypatch, AccountResult(False, failure_kind=FailureKind.AUTH, reason='HTTP 401'))

	result = await check_in_account_with_retry(account, 0, AppConfig(providers={}), deadline=None)

	assert len(calls) == 1
	assert result.failure_kind is FailureKind.AUTH


async def test_does_not_retry_a_config_failure(monkeypatch, account, retry_env):
	calls = _patch_account(
		monkeypatch, AccountResult(False, failure_kind=FailureKind.CONFIG, reason='provider not found')
	)

	result = await check_in_account_with_retry(account, 0, AppConfig(providers={}), deadline=None)

	assert len(calls) == 1
	assert result.failure_kind is FailureKind.CONFIG


async def test_gives_up_after_the_last_permitted_attempt(monkeypatch, account, retry_env):
	calls = _patch_account(monkeypatch, _transient())

	result = await check_in_account_with_retry(account, 0, AppConfig(providers={}), deadline=None)

	assert len(calls) == 2
	assert result.success is False


async def test_runs_first_attempt_but_skips_retry_when_budget_is_exhausted(monkeypatch, account, retry_env):
	calls = _patch_account(monkeypatch, _transient())
	deadline = time.monotonic() - 1

	result = await check_in_account_with_retry(account, 0, AppConfig(providers={}), deadline=deadline)

	assert len(calls) == 1
	assert result.success is False


async def test_waits_before_retrying(monkeypatch, account):
	monkeypatch.setenv('CHECKIN_ACCOUNT_MAX_ATTEMPTS', '2')
	monkeypatch.setenv('CHECKIN_ACCOUNT_RETRY_DELAY_MS', '200')
	_patch_account(monkeypatch, _transient(), _succeeded())

	started = time.monotonic()
	result = await check_in_account_with_retry(account, 0, AppConfig(providers={}), deadline=None)
	elapsed = time.monotonic() - started

	assert result.success is True
	# 阈值放宽到请求间隔以下，避免 Windows 计时器粒度导致误判
	assert elapsed >= 0.1


async def test_rotates_the_exit_node_before_every_attempt(monkeypatch, retry_env):
	_patch_account(monkeypatch, _transient(), _succeeded())
	rotations = []

	class FakeRotator:
		def rotate(self, label):
			rotations.append(label)
			return '1.1.1.1'

	provider = ProviderConfig(name='agentrouter', domain='https://agentrouter.org', use_proxy=True)
	account = AccountConfig(cookies={'session': 'x'}, name='Account 1', provider='agentrouter')

	result = await check_in_account_with_retry(
		account,
		0,
		AppConfig(providers={'agentrouter': provider}),
		deadline=None,
		rotator=FakeRotator(),
	)

	assert result.success is True
	assert rotations == ['Account 1', 'Account 1']


async def test_does_not_rotate_for_accounts_that_bypass_the_proxy(monkeypatch, retry_env):
	# 不走代理的 provider 轮转毫无意义，还会白占一个出口 IP 名额
	_patch_account(monkeypatch, _succeeded())
	rotations = []

	class FakeRotator:
		def rotate(self, label):
			rotations.append(label)

	provider = ProviderConfig(name='anyrouter', domain='https://anyrouter.top', use_proxy=False)
	account = AccountConfig(cookies={'session': 'x'}, name='Account 1', provider='anyrouter')

	result = await check_in_account_with_retry(
		account,
		0,
		AppConfig(providers={'anyrouter': provider}),
		deadline=None,
		rotator=FakeRotator(),
	)

	assert result.success is True
	assert rotations == []


async def test_works_without_a_rotator(monkeypatch, account, retry_env):
	_patch_account(monkeypatch, _succeeded())

	result = await check_in_account_with_retry(account, 0, AppConfig(providers={}), deadline=None)

	assert result.success is True
