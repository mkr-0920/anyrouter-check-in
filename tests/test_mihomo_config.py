"""校验 setup_mihomo_proxy.sh 生成的 mihomo 配置。

一个 YAML typo 会让 mihomo 启动失败，而脚本在 PROXY_REQUIRED=false 时会静默退出 0
（表现为「整轮无代理」），所以这里把生成的配置结构固化下来。
"""

import re
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).parent.parent / 'scripts' / 'setup_mihomo_proxy.sh'

SUBSTITUTIONS = {
	'${PROXY_PORT}': '7890',
	'${PROXY_SUBSCRIPTION_URL}': 'https://example.com/sub',
	'${PROXY_TEST_URL}': 'https://www.google.com/generate_204',
	'${CONTROLLER_PORT}': '9090',
	'${CONTROLLER_SECRET}': 'deadbeef',
}


def generate_config() -> dict:
	text = SCRIPT.read_text(encoding='utf-8')
	match = re.search(r'cat > config\.yaml <<EOF\n(.*?)\nEOF\n', text, re.DOTALL)
	assert match, 'config.yaml heredoc not found in setup_mihomo_proxy.sh'
	body = match.group(1)
	for placeholder, value in SUBSTITUTIONS.items():
		body = body.replace(placeholder, value)
	return yaml.safe_load(body)


def test_generated_config_is_valid_yaml_with_expected_endpoints():
	config = generate_config()

	assert config['mixed-port'] == 7890
	assert config['external-controller'] == '127.0.0.1:9090'
	assert config['secret'] == 'deadbeef'


def test_checkin_group_is_manually_selectable():
	config = generate_config()
	groups = {group['name']: group for group in config['proxy-groups']}

	checkin = groups['CHECKIN']
	assert checkin['type'] == 'select'
	# AUTO 必须排在首位：不额外切换时，行为回落到原来的自动选路
	assert checkin['proxies'][0] == 'AUTO'
	assert checkin['use'] == ['subscription']


def test_auto_group_keeps_the_original_failover_behaviour():
	config = generate_config()
	groups = {group['name']: group for group in config['proxy-groups']}

	auto = groups['AUTO']
	assert auto['type'] == 'url-test'
	assert auto['use'] == ['subscription']
	assert auto['url'] == 'https://www.google.com/generate_204'


def test_all_traffic_still_routes_through_the_checkin_group():
	config = generate_config()

	assert config['rules'] == ['MATCH,CHECKIN']
	assert 'subscription' in config['proxy-providers']


def test_subscription_is_loaded_as_a_provider_not_merged_wholesale():
	config = generate_config()
	provider = config['proxy-providers']['subscription']

	assert provider['type'] == 'http'
	assert provider['url'] == 'https://example.com/sub'
	assert 'health-check' in provider


@pytest.mark.parametrize('placeholder', sorted(SUBSTITUTIONS))
def test_every_placeholder_is_substituted(placeholder):
	body = SCRIPT.read_text(encoding='utf-8')
	assert placeholder in body
