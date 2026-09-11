#!/usr/bin/env python3
"""一次性探测：遍历 mihomo 分组里的节点，量出真实不同的出口 IP 个数。

用法（需要先启动 mihomo 并配置 CHECKIN_MIHOMO_API / CHECKIN_PROXY_URL）:
    uv run python scripts/probe_nodes.py
    uv run python scripts/probe_nodes.py --group CHECKIN
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

from utils.mihomo import MihomoApi, MihomoError, load_mihomo_api  # noqa: E402
from utils.proxy import fetch_exit_ip  # noqa: E402

DEFAULT_GROUP = 'CHECKIN'
EXIT_IP_TIMEOUT = 10.0


@dataclass(frozen=True)
class NodeProbe:
	"""单个节点的探测结果。"""

	node: str
	exit_ip: str | None = None
	error: str | None = None


@dataclass(frozen=True)
class ProbeReport:
	probes: list[NodeProbe] = field(default_factory=list)

	@property
	def reachable(self) -> list[NodeProbe]:
		return [probe for probe in self.probes if probe.exit_ip]

	@property
	def distinct_ips(self) -> list[str]:
		"""去重后的出口 IP，保持首次出现的顺序。"""
		seen: dict[str, None] = {}
		for probe in self.reachable:
			assert probe.exit_ip is not None
			seen.setdefault(probe.exit_ip, None)
		return list(seen)


def _default_client_factory(proxy_url: str) -> httpx.Client:
	return httpx.Client(proxy=proxy_url, timeout=EXIT_IP_TIMEOUT)


def probe_nodes(
	api: MihomoApi,
	group: str,
	proxy_url: str,
	*,
	client_factory=_default_client_factory,
) -> ProbeReport:
	"""逐个切换节点并记录出口 IP。

	每个节点都用全新的 client：连接池会复用隧道，而复用的隧道钉在建立时的节点上，
	不换 client 就会把每个节点都测成同一个出口 IP。
	"""
	probes: list[NodeProbe] = []
	for node in api.group_members(group):
		try:
			api.select_node(group, node)
		except MihomoError as exc:
			probes.append(NodeProbe(node=node, error=f'切换失败: {exc}'))
			continue

		with client_factory(proxy_url) as client:
			exit_ip = fetch_exit_ip(client)
		if exit_ip:
			probes.append(NodeProbe(node=node, exit_ip=exit_ip))
		else:
			probes.append(NodeProbe(node=node, error='无法取得出口 IP'))

	return ProbeReport(probes=probes)


def format_report(report: ProbeReport) -> str:
	lines = [
		f'节点总数: {len(report.probes)}',
		f'可用节点: {len(report.reachable)}',
		f'不同出口 IP: {len(report.distinct_ips)}',
	]
	if report.distinct_ips:
		lines.append('')
		lines.append('出口 IP 分布:')
		for exit_ip in report.distinct_ips:
			nodes = [probe.node for probe in report.reachable if probe.exit_ip == exit_ip]
			lines.append(f'  {exit_ip}  ← {len(nodes)} 个节点')
			for node in nodes:
				lines.append(f'      {node}')
	failed = [probe for probe in report.probes if probe.error]
	if failed:
		lines.append('')
		lines.append('不可用节点:')
		for probe in failed:
			lines.append(f'  {probe.node}: {probe.error}')
	return '\n'.join(lines)


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--group', default=DEFAULT_GROUP, help=f'要探测的分组名（默认 {DEFAULT_GROUP}）')
	args = parser.parse_args()

	api = load_mihomo_api()
	if api is None:
		print('[FAILED] CHECKIN_MIHOMO_API 未设置，无法访问 mihomo 控制接口')
		return 1

	proxy_url = os.getenv('CHECKIN_PROXY_URL', '').strip()
	if not proxy_url:
		print('[FAILED] CHECKIN_PROXY_URL 未设置，无法通过代理探测出口 IP')
		return 1

	try:
		report = probe_nodes(api, args.group, proxy_url)
	except MihomoError as exc:
		print(f'[FAILED] {exc}')
		return 1

	print(format_report(report))
	return 0


if __name__ == '__main__':
	sys.exit(main())
