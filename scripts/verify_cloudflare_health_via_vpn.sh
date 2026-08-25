#!/bin/sh
# Verify public Worker health through one temporary VPN egress path.
set -eu

: "${VPN_CONFIG_DE:?VPN_CONFIG_DE is required}"
: "${VPN_CONFIG_NL:?VPN_CONFIG_NL is required}"
: "${VPN_CONFIG_CH:?VPN_CONFIG_CH is required}"
: "${VPN_AUTH:?VPN_AUTH is required}"

if [ "$#" -ne 4 ] || [ "$1" != "--url" ] || [ "$3" != "--expected-version" ]; then
  printf '%s\n' 'usage: verify_cloudflare_health_via_vpn.sh --url URL --expected-version VERSION' >&2
  exit 2
fi

health_url=$2
expected_version=$4
runtime_root=${RUNNER_TEMP:-/tmp}
runtime_dir=$(mktemp -d "$runtime_root/cloudflare-health-vpn.XXXXXX")
tun_device=tun-health
pid_file="$runtime_dir/openvpn.pid"
auth_file="$runtime_dir/auth"
vpn_log="$runtime_dir/openvpn.log"
python_bin=${PYTHON_BIN:-python3}

cleanup_vpn() {
  if [ -s "$pid_file" ]; then
    vpn_pid=$(cat "$pid_file" 2>/dev/null || true)
    case "$vpn_pid" in
      ''|*[!0-9]*) ;;
      *) sudo kill "$vpn_pid" >/dev/null 2>&1 || true ;;
    esac
  fi
  sudo ip link delete "$tun_device" >/dev/null 2>&1 || true
  rm -f "$pid_file"
}

cleanup() {
  status=$?
  set +e
  cleanup_vpn
  case "$runtime_dir" in
    "$runtime_root"/cloudflare-health-vpn.*) rm -rf "$runtime_dir" ;;
  esac
  trap - EXIT HUP INT TERM
  exit "$status"
}

trap cleanup EXIT HUP INT TERM
umask 077
printf '%s' "$VPN_AUTH" > "$auth_file"

wait_for_vpn() {
  attempts=0
  while [ "$attempts" -lt 30 ]; do
    attempts=$((attempts + 1))
    if [ -s "$pid_file" ]; then
      vpn_pid=$(cat "$pid_file" 2>/dev/null || true)
      case "$vpn_pid" in
        ''|*[!0-9]*) ;;
        *)
          if sudo kill -0 "$vpn_pid" >/dev/null 2>&1 \
            && sudo ip link show dev "$tun_device" >/dev/null 2>&1 \
            && sudo ip route get 1.1.1.1 2>/dev/null | grep -F "dev $tun_device" >/dev/null; then
            return 0
          fi
          ;;
      esac
    fi
    sleep 1
  done
  return 1
}

verify_country() {
  country=$1
  config_name=$2
  config_value=$3
  config_file="$runtime_dir/$config_name"
  printf '%s' "$config_value" > "$config_file"

  if ! sudo openvpn \
    --config "$config_file" \
    --auth-user-pass "$auth_file" \
    --dev "$tun_device" \
    --writepid "$pid_file" \
    --daemon \
    --log "$vpn_log"; then
    printf 'VPN connection unavailable via %s\n' "$country" >&2
    return 1
  fi
  if ! wait_for_vpn; then
    printf 'VPN connection timed out via %s\n' "$country" >&2
    return 1
  fi
  "$python_bin" scripts/verify_cloudflare_health.py \
    --url "$health_url" \
    --expected-version "$expected_version"
}

for candidate in \
  "DE:vpn-de.premiumize.me.ovpn:$VPN_CONFIG_DE" \
  "NL:vpn-nl.premiumize.me.ovpn:$VPN_CONFIG_NL" \
  "CH:vpn-ch.premiumize.me.ovpn:$VPN_CONFIG_CH"; do
  country=${candidate%%:*}
  candidate=${candidate#*:}
  config_name=${candidate%%:*}
  config_value=${candidate#*:}
  if verify_country "$country" "$config_name" "$config_value"; then
    printf 'VPN health verification succeeded via %s\n' "$country"
    exit 0
  fi
  cleanup_vpn
done

printf '%s\n' 'VPN health verification failed for all configured countries' >&2
exit 1
