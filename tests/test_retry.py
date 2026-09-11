import httpx
import pytest

from utils.retry import (
	PermanentError,
	RetryableError,
	parse_json,
	raise_for_status,
	retry_call,
)


def test_returns_result_without_retry_when_first_attempt_succeeds():
	attempts = []
	delays = []

	def operation():
		attempts.append(1)
		return 'ok'

	result = retry_call(
		operation,
		max_attempts=3,
		base_delay=1.0,
		max_delay=8.0,
		sleep=delays.append,
	)

	assert result == 'ok'
	assert len(attempts) == 1
	assert delays == []


def test_retries_transport_error_until_success():
	attempts = []
	delays = []

	def operation():
		attempts.append(1)
		if len(attempts) < 3:
			raise httpx.ConnectError('connection reset by peer')
		return 'ok'

	result = retry_call(
		operation,
		max_attempts=3,
		base_delay=1.0,
		max_delay=8.0,
		sleep=delays.append,
	)

	assert result == 'ok'
	assert len(attempts) == 3
	assert len(delays) == 2


def test_reraises_original_error_after_max_attempts():
	attempts = []
	error = httpx.ReadTimeout('read timed out')

	def operation():
		attempts.append(1)
		raise error

	with pytest.raises(httpx.ReadTimeout) as excinfo:
		retry_call(
			operation,
			max_attempts=3,
			base_delay=1.0,
			max_delay=8.0,
			sleep=lambda _: None,
		)

	assert excinfo.value is error
	assert len(attempts) == 3


def test_backoff_grows_exponentially_and_is_capped():
	delays = []

	def operation():
		raise httpx.ConnectError('nope')

	with pytest.raises(httpx.ConnectError):
		retry_call(
			operation,
			max_attempts=5,
			base_delay=1.0,
			max_delay=4.0,
			sleep=delays.append,
		)

	# 1s, 2s, 4s, 4s（封顶）外加 0-500ms 抖动
	assert len(delays) == 4
	assert all(expected <= delay <= expected + 0.5 for delay, expected in zip(delays, [1.0, 2.0, 4.0, 4.0]))


@pytest.mark.parametrize('status_code', [408, 429, 500, 502, 503, 504])
def test_retryable_status_codes_raise_retryable_error(status_code):
	with pytest.raises(RetryableError):
		raise_for_status(httpx.Response(status_code))


@pytest.mark.parametrize('status_code', [401, 403])
def test_auth_status_codes_raise_permanent_error(status_code):
	with pytest.raises(PermanentError):
		raise_for_status(httpx.Response(status_code))


@pytest.mark.parametrize('status_code', [200, 204, 400, 404, 422])
def test_other_status_codes_are_left_to_the_caller(status_code):
	raise_for_status(httpx.Response(status_code))


def test_permanent_error_is_not_retried():
	attempts = []

	def operation():
		attempts.append(1)
		raise PermanentError('invalid credentials')

	with pytest.raises(PermanentError):
		retry_call(
			operation,
			max_attempts=3,
			base_delay=1.0,
			max_delay=8.0,
			sleep=lambda _: None,
		)

	assert len(attempts) == 1


def test_retryable_status_error_is_retried():
	attempts = []

	def operation():
		attempts.append(1)
		if len(attempts) < 3:
			raise_for_status(httpx.Response(503))
		return 'ok'

	result = retry_call(
		operation,
		max_attempts=3,
		base_delay=1.0,
		max_delay=8.0,
		sleep=lambda _: None,
	)

	assert result == 'ok'
	assert len(attempts) == 3


def test_retry_after_header_overrides_backoff():
	delays = []

	def operation():
		raise_for_status(httpx.Response(429, headers={'Retry-After': '7'}))

	with pytest.raises(RetryableError):
		retry_call(
			operation,
			max_attempts=2,
			base_delay=1.0,
			max_delay=4.0,
			sleep=delays.append,
		)

	assert delays == [7.0]


def test_invalid_json_body_is_retried():
	attempts = []

	def operation():
		attempts.append(1)
		if len(attempts) < 2:
			parse_json(httpx.Response(200, text='<html>proxy error</html>'))
		return 'ok'

	result = retry_call(
		operation,
		max_attempts=3,
		base_delay=1.0,
		max_delay=8.0,
		sleep=lambda _: None,
	)

	assert result == 'ok'
	assert len(attempts) == 2


def test_parse_json_returns_decoded_payload():
	assert parse_json(httpx.Response(200, json={'success': True})) == {'success': True}


def test_parse_json_reports_status_and_body_when_body_is_html():
	response = httpx.Response(200, text='<html><body>Access denied by WAF</body></html>')

	with pytest.raises(RetryableError) as excinfo:
		parse_json(response)

	message = str(excinfo.value)
	assert 'HTTP 200' in message
	assert 'Access denied by WAF' in message
	assert excinfo.value.response is response


def test_parse_json_marks_an_empty_body():
	with pytest.raises(RetryableError) as excinfo:
		parse_json(httpx.Response(200, text=''))

	assert '<empty>' in str(excinfo.value)


def test_parse_json_truncates_a_long_body():
	with pytest.raises(RetryableError) as excinfo:
		parse_json(httpx.Response(200, text='x' * 5000))

	message = str(excinfo.value)
	assert 'x' * 120 in message
	assert 'x' * 121 not in message


def test_invalid_retry_after_header_falls_back_to_backoff():
	delays = []

	def operation():
		raise_for_status(httpx.Response(429, headers={'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'}))

	with pytest.raises(RetryableError):
		retry_call(
			operation,
			max_attempts=2,
			base_delay=1.0,
			max_delay=8.0,
			sleep=delays.append,
		)

	# HTTP-date 形式不支持，退回指数退避（1s + 0-500ms 抖动）
	assert len(delays) == 1
	assert 1.0 <= delays[0] <= 1.5


def test_stops_before_a_sleep_that_would_exceed_the_budget():
	attempts = []
	clock = [0.0]

	def operation():
		attempts.append(1)
		raise httpx.ConnectError('flaky link')

	def fake_sleep(seconds):
		clock[0] += seconds

	with pytest.raises(httpx.ConnectError):
		retry_call(
			operation,
			max_attempts=5,
			base_delay=1.0,
			max_delay=8.0,
			budget=3.0,
			sleep=fake_sleep,
			clock=lambda: clock[0],
		)

	# 预算 3s：退避 1s 后仍在预算内；下一次退避 2s 会越过预算，直接放弃
	assert len(attempts) == 2
