"""网络重试工具：对暂时性失败做有界退避重试。"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

T = TypeVar('T')

JITTER_SECONDS = 0.5

# 网关 / 限流类状态码：抖动或过载导致，重试有意义
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
# 认证类状态码：凭据或会话失效，重试只会浪费时间
PERMANENT_STATUS_CODES = frozenset({401, 403})

DEFAULT_HTTP_MAX_ATTEMPTS = 3
DEFAULT_HTTP_BASE_DELAY_MS = 1000.0
DEFAULT_HTTP_MAX_DELAY_MS = 8000.0
DEFAULT_ACCOUNT_MAX_ATTEMPTS = 3
DEFAULT_ACCOUNT_RETRY_DELAY_MS = 5000.0
DEFAULT_TOTAL_TIMEOUT_SEC = 900.0


class RetryableError(Exception):
	"""暂时性失败，值得重试。"""

	def __init__(
		self,
		message: str,
		*,
		retry_after: float | None = None,
		response: httpx.Response | None = None,
	) -> None:
		super().__init__(message)
		self.retry_after = retry_after
		self.response = response


class PermanentError(Exception):
	"""永久性失败（认证失效、凭据错误等），重试无意义。"""


@dataclass(frozen=True)
class RetrySettings:
	"""重试策略配置，集中承载各层级的上限。"""

	http_max_attempts: int
	http_base_delay: float
	http_max_delay: float
	account_max_attempts: int
	account_retry_delay: float
	total_budget: float | None


def _env_int(name: str, default: int) -> int:
	raw = os.getenv(name, '').strip()
	if not raw:
		return default
	try:
		return int(raw)
	except ValueError:
		return default


def _env_float(name: str, default: float) -> float:
	raw = os.getenv(name, '').strip()
	if not raw:
		return default
	try:
		return float(raw)
	except ValueError:
		return default


def load_retry_settings() -> RetrySettings:
	"""从环境变量加载重试配置，非法值一律回落到默认值。"""
	total_budget = _env_float('CHECKIN_TOTAL_TIMEOUT_SEC', DEFAULT_TOTAL_TIMEOUT_SEC)
	return RetrySettings(
		http_max_attempts=max(1, _env_int('CHECKIN_HTTP_MAX_ATTEMPTS', DEFAULT_HTTP_MAX_ATTEMPTS)),
		http_base_delay=_env_float('CHECKIN_HTTP_RETRY_BASE_DELAY_MS', DEFAULT_HTTP_BASE_DELAY_MS) / 1000,
		http_max_delay=_env_float('CHECKIN_HTTP_RETRY_MAX_DELAY_MS', DEFAULT_HTTP_MAX_DELAY_MS) / 1000,
		account_max_attempts=max(1, _env_int('CHECKIN_ACCOUNT_MAX_ATTEMPTS', DEFAULT_ACCOUNT_MAX_ATTEMPTS)),
		account_retry_delay=_env_float('CHECKIN_ACCOUNT_RETRY_DELAY_MS', DEFAULT_ACCOUNT_RETRY_DELAY_MS) / 1000,
		total_budget=total_budget if total_budget > 0 else None,
	)


def _retry_after_seconds(response: httpx.Response) -> float | None:
	raw = response.headers.get('Retry-After')
	if not raw:
		return None
	try:
		return max(0.0, float(raw.strip()))
	except ValueError:
		# HTTP-date 形式暂不支持，退回指数退避
		return None


def raise_for_status(response: httpx.Response) -> None:
	"""把可重试 / 认证失败的状态码转成异常，其余状态码留给调用方处理。"""
	if response.status_code in PERMANENT_STATUS_CODES:
		raise PermanentError(f'HTTP {response.status_code}')
	if response.status_code in RETRYABLE_STATUS_CODES:
		raise RetryableError(
			f'HTTP {response.status_code}',
			retry_after=_retry_after_seconds(response),
		)


def _body_snippet(response: httpx.Response, limit: int = 120) -> str:
	"""取响应体开头用于诊断；空 body 与无法解码分别显式标注。"""
	try:
		text = ' '.join(response.text.split())
	except Exception:  # nosec B110 - 诊断信息，取不到就算了
		return '<undecodable>'
	if not text:
		return '<empty>'
	return text[:limit]


def parse_json(response: httpx.Response) -> Any:
	"""解析 JSON 响应体；body 不是合法 JSON（常见于代理返回错误页）时按可重试处理。"""
	try:
		return response.json()
	except ValueError as exc:
		raise RetryableError(
			f'non-JSON body (HTTP {response.status_code}, {len(response.content)} bytes): {_body_snippet(response)}',
			response=response,
		) from exc


def is_retryable(exc: BaseException) -> bool:
	"""判断异常是否属于网络抖动类失败，重试有意义。"""
	return isinstance(exc, (httpx.TransportError, RetryableError))


def _jitter() -> float:
	"""退避抖动，避免多个账号在同一时刻齐步重试。"""
	return random.uniform(0, JITTER_SECONDS)  # nosec B311 - 仅用于退避抖动，非安全用途


def _delay_for(exc: BaseException, attempt: int, base_delay: float, max_delay: float) -> float:
	if isinstance(exc, RetryableError) and exc.retry_after is not None:
		return exc.retry_after
	# 2.0 而非 2：int.__pow__ 在 typeshed 里返回 Any
	return min(base_delay * (2.0**attempt), max_delay) + _jitter()


def retry_call(
	fn: Callable[[], T],
	*,
	max_attempts: int,
	base_delay: float,
	max_delay: float,
	budget: float | None = None,
	sleep: Callable[[float], None] = time.sleep,
	clock: Callable[[], float] = time.monotonic,
) -> T:
	"""执行 fn，遇到暂时性失败时按指数退避重试，返回 fn 的结果。

	非暂时性失败（认证错误、配置错误等）立即抛出，不做重试。
	budget 为可选的秒数上限；若下一次退避会越过预算，则立即放弃而不是白等。
	"""
	started = clock()
	for attempt in range(max_attempts):
		try:
			return fn()
		except Exception as exc:
			if attempt == max_attempts - 1 or not is_retryable(exc):
				raise
			delay = _delay_for(exc, attempt, base_delay, max_delay)
			if budget is not None and clock() - started + delay > budget:
				raise
			sleep(delay)
	raise AssertionError('unreachable')
