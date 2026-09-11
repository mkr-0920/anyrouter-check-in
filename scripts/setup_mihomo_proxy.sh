#!/usr/bin/env bash
# 通过 mihomo 拉取订阅、启动本地代理并探测可用节点。
# 环境变量:
#   PROXY_SUBSCRIPTION_URL  订阅链接（必填才启用）
#   PROXY_TEST_URL          探测目标，默认 https://www.google.com/generate_204
#   PROXY_REQUIRED          true 时探测失败则退出 1
#   PROXY_PORT              本地 mixed-port，默认 7890
#   CONTROLLER_PORT         mihomo 控制接口端口，默认 9090（只监听本机）
#   CONTROLLER_SECRET       控制接口密钥，未设置时随机生成
#
# 成功后写入 GITHUB_ENV：CHECKIN_PROXY_URL、CHECKIN_MIHOMO_API、CHECKIN_MIHOMO_SECRET

set -euo pipefail

if [[ -z "${PROXY_SUBSCRIPTION_URL:-}" ]]; then
	echo "[INFO] PROXY_SUBSCRIPTION_URL not set, skip proxy setup"
	exit 0
fi

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_TEST_URL="${PROXY_TEST_URL:-https://www.google.com/generate_204}"
MIHOMO_VERSION="${MIHOMO_VERSION:-v1.19.0}"
PROXY_REQUIRED="${PROXY_REQUIRED:-false}"
CONTROLLER_PORT="${CONTROLLER_PORT:-9090}"
CONTROLLER_SECRET="${CONTROLLER_SECRET:-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')}"

mkdir -p "${PROXY_DIR}"
cd "${PROXY_DIR}"

echo "[INFO] Downloading mihomo ${MIHOMO_VERSION}..."
ARCHIVE="mihomo-linux-amd64-${MIHOMO_VERSION}.gz"
if ! curl --retry 3 --retry-delay 5 --retry-all-errors -fsSL -o "${ARCHIVE}" \
	"https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/${ARCHIVE}"; then
	echo "[WARN] Failed to download mihomo ${MIHOMO_VERSION}, skip proxy setup"
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi
gunzip -f "${ARCHIVE}"
chmod +x "mihomo-linux-amd64-${MIHOMO_VERSION}"
MIHOMO_BIN="${PROXY_DIR}/mihomo-linux-amd64-${MIHOMO_VERSION}"

cat > config.yaml <<EOF
mixed-port: ${PROXY_PORT}
allow-lan: false
ipv6: false
mode: rule
log-level: warning
unified-delay: true
external-controller: 127.0.0.1:${CONTROLLER_PORT}
secret: "${CONTROLLER_SECRET}"

proxy-providers:
  subscription:
    type: http
    url: "${PROXY_SUBSCRIPTION_URL}"
    interval: 3600
    path: ./subscription.yaml
    health-check:
      enable: true
      interval: 300
      url: https://www.gstatic.com/generate_204

proxy-groups:
  # AUTO 保留原来的自动选路 + 故障转移，作为 CHECKIN 的默认选项
  - name: AUTO
    type: url-test
    url: "${PROXY_TEST_URL}"
    interval: 300
    tolerance: 150
    lazy: false
    use:
      - subscription

  # CHECKIN 由 checkin 按账号手动切换；默认走 AUTO，所以不切换时行为与以前一致
  - name: CHECKIN
    type: select
    proxies:
      - AUTO
    use:
      - subscription

rules:
  - MATCH,CHECKIN
EOF

echo "[INFO] Starting mihomo on 127.0.0.1:${PROXY_PORT}..."
nohup "${MIHOMO_BIN}" -d "${PROXY_DIR}" -f config.yaml > mihomo.log 2>&1 &
echo $! > mihomo.pid

CONTROLLER_API="http://127.0.0.1:${CONTROLLER_PORT}"

# 显式选回 AUTO：不依赖分组成员的默认排序，失败也不阻断（后面的健康检查会暴露问题）
for _ in $(seq 1 10); do
	if curl -fsS -X PUT -H "Authorization: Bearer ${CONTROLLER_SECRET}" \
		-d '{"name":"AUTO"}' "${CONTROLLER_API}/proxies/CHECKIN" > /dev/null 2>&1; then
		echo "[INFO] CHECKIN group defaulted to AUTO"
		break
	fi
	sleep 1
done

PROXY_URL="http://127.0.0.1:${PROXY_PORT}"
READY=false
for attempt in $(seq 1 45); do
	if curl -fsS -x "${PROXY_URL}" --max-time 20 "${PROXY_TEST_URL}" -o /dev/null 2>/dev/null; then
		READY=true
		break
	fi
	echo "[INFO] Waiting for proxy health check (${attempt}/45)..."
	sleep 2
done

if [[ "${READY}" != "true" ]]; then
	echo "[FAILED] Proxy health check failed for ${PROXY_TEST_URL}"
	tail -n 30 mihomo.log || true
	if [[ -f mihomo.pid ]]; then
		kill "$(cat mihomo.pid)" 2>/dev/null || true
	fi
	if [[ "${PROXY_REQUIRED}" == "true" ]]; then
		exit 1
	fi
	exit 0
fi

echo "[SUCCESS] Proxy is ready: ${PROXY_URL}"
echo "[INFO] Proxy is scoped to CHECKIN_PROXY_URL (browser/python only, not global HTTP_PROXY)"
if [[ -n "${GITHUB_ENV:-}" ]]; then
	echo "CHECKIN_PROXY_URL=${PROXY_URL}" >> "${GITHUB_ENV}"
	echo "CHECKIN_MIHOMO_API=${CONTROLLER_API}" >> "${GITHUB_ENV}"
	echo "CHECKIN_MIHOMO_SECRET=${CONTROLLER_SECRET}" >> "${GITHUB_ENV}"
fi
