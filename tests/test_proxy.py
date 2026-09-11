import httpx
import pytest

from utils.proxy import fetch_exit_ip


def _client(handler) -> httpx.Client:
	return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_exit_ip_returns_address_from_plain_text_body():
	def handler(request):
		assert request.url.host == 'api.ipify.org'
		return httpx.Response(200, text='203.0.113.7\n')

	assert fetch_exit_ip(_client(handler)) == '203.0.113.7'


def test_fetch_exit_ip_returns_none_on_transport_error():
	def handler(request):
		raise httpx.ConnectError('proxy refused')

	assert fetch_exit_ip(_client(handler)) is None


def test_fetch_exit_ip_returns_none_on_error_status():
	def handler(request):
		return httpx.Response(502)

	assert fetch_exit_ip(_client(handler)) is None


def test_fetch_exit_ip_returns_none_when_body_is_not_an_address():
	def handler(request):
		return httpx.Response(200, text='<html>proxy login required</html>')

	assert fetch_exit_ip(_client(handler)) is None


def test_fetch_exit_ip_returns_none_on_empty_body():
	def handler(request):
		return httpx.Response(200, text='')

	assert fetch_exit_ip(_client(handler)) is None


@pytest.mark.parametrize('address', ['2001:0db8:85a3:0000:0000:8a2e:0370:7334', '10.0.0.1'])
def test_fetch_exit_ip_accepts_ipv6_and_private_addresses(address):
	def handler(request):
		return httpx.Response(200, text=address)

	assert fetch_exit_ip(_client(handler)) == address
