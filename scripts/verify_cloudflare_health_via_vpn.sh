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
vpn_started=0
tun_owned=0
launcher_pid=''
config_file=''

is_openvpn_pid() {
  candidate_pid=$1
  case "$candidate_pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  sudo -n kill -0 "$candidate_pid" >/dev/null 2>&1 || return 1
  process_arguments=$(sudo -n cat "/proc/$candidate_pid/cmdline" 2>/dev/null | tr '\000' ' ' || true)
  case "$process_arguments" in
    *openvpn*"$pid_file"*) return 0 ;;
    *) return 1 ;;
  esac
}

is_launcher_pid() {
  candidate_pid=$1
  case "$candidate_pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  sudo -n kill -0 "$candidate_pid" >/dev/null 2>&1 || return 1
  process_arguments=$(sudo -n cat "/proc/$candidate_pid/cmdline" 2>/dev/null | tr '\000' ' ' || true)
  case "$process_arguments" in
    *timeout*"$config_file"*|*openvpn*"$pid_file"*) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for_process_exit() {
  candidate_pid=$1
  attempts=0
  while [ "$attempts" -lt 5 ]; do
    if ! sudo -n kill -0 "$candidate_pid" >/dev/null 2>&1; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  return 1
}

reap_launcher() {
  case "$launcher_pid" in
    ''|*[!0-9]*) return 0 ;;
  esac
  if ! sudo -n kill -0 "$launcher_pid" >/dev/null 2>&1; then
    wait "$launcher_pid" >/dev/null 2>&1 || true
    launcher_pid=''
    return 0
  fi
  if ! is_launcher_pid "$launcher_pid"; then
    return 1
  fi
  sudo -n kill -TERM "$launcher_pid" >/dev/null 2>&1 || return 1
  if ! wait_for_process_exit "$launcher_pid"; then
    if ! is_launcher_pid "$launcher_pid"; then
      return 1
    fi
    sudo -n kill -KILL "$launcher_pid" >/dev/null 2>&1 || return 1
    wait_for_process_exit "$launcher_pid" || return 1
  fi
  wait "$launcher_pid" >/dev/null 2>&1 || true
  launcher_pid=''
}

cleanup_vpn() {
  cleanup_failed=0
  if [ "$vpn_started" -eq 1 ] && [ -s "$pid_file" ]; then
    vpn_pid=$(cat "$pid_file" 2>/dev/null || true)
    if is_openvpn_pid "$vpn_pid"; then
      sudo -n kill -TERM "$vpn_pid" >/dev/null 2>&1 || cleanup_failed=1
      if ! wait_for_process_exit "$vpn_pid"; then
        if is_openvpn_pid "$vpn_pid"; then
          sudo -n kill -KILL "$vpn_pid" >/dev/null 2>&1 || cleanup_failed=1
          wait_for_process_exit "$vpn_pid" || cleanup_failed=1
        else
          cleanup_failed=1
        fi
      fi
    else
      cleanup_failed=1
    fi
  fi
  reap_launcher || cleanup_failed=1
  if [ "$vpn_started" -eq 1 ] || [ "$tun_owned" -eq 1 ]; then
    sudo -n ip link delete "$tun_device" >/dev/null 2>&1 || true
  fi
  if sudo -n ip link show dev "$tun_device" >/dev/null 2>&1; then
    cleanup_failed=1
  fi
  rm -f "$pid_file"
  vpn_started=0
  tun_owned=0
  return "$cleanup_failed"
}

# shellcheck disable=SC2329 # Invoked through the EXIT trap below.
cleanup() {
  status=$?
  set +e
  cleanup_vpn
  cleanup_status=$?
  case "$runtime_dir" in
    "$runtime_root"/cloudflare-health-vpn.*) rm -rf "$runtime_dir" ;;
  esac
  trap - EXIT HUP INT TERM
  if [ "$status" -eq 0 ] && [ "$cleanup_status" -ne 0 ]; then
    exit 1
  fi
  exit "$status"
}

trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
trap cleanup EXIT
umask 077
printf '%s' "$VPN_AUTH" > "$auth_file"

sanitize_openvpn_config() {
  awk '
    function allowed(directive) {
      return directive == "client" || directive == "remote" || directive == "proto" ||
        directive == "resolv-retry" || directive == "nobind" || directive == "persist-key" ||
        directive == "persist-tun" || directive == "cipher" || directive == "data-ciphers" ||
        directive == "data-ciphers-fallback" || directive == "auth" || directive == "verb" ||
        directive == "reneg-sec" || directive == "verify-x509-name" ||
        directive == "remote-cert-tls" || directive == "mute-replay-warnings" ||
        directive == "tls-version-min" || directive == "tls-cipher" || directive == "key-direction"
    }
    {
      sub(/\r$/, "")
      if (block != "") {
        print
        if ($0 == "</" block ">") block = ""
        next
      }
      if ($0 ~ /^[[:space:]]*$/ || $0 ~ /^[[:space:]]*[#;]/) next
      if ($0 == "<ca>") {
        block = $0
        sub(/^</, "", block)
        sub(/>$/, "", block)
        print
        next
      }
      if ($0 ~ /^</) exit 1
      directive = $1
      if (directive == "auth-user-pass" || directive == "dev" || directive == "setenv" || directive == "ignore-unknown-option") next
      if (!allowed(directive)) exit 1
      print
    }
    END { if (block != "") exit 1 }
  '
}

ensure_tun_absent() {
  ! sudo -n ip link show dev "$tun_device" >/dev/null 2>&1
}

wait_for_vpn() {
  attempts=0
  while [ "$attempts" -lt 30 ]; do
    attempts=$((attempts + 1))
    if [ -s "$pid_file" ]; then
      vpn_pid=$(cat "$pid_file" 2>/dev/null || true)
      if is_openvpn_pid "$vpn_pid" && sudo -n ip link show dev "$tun_device" >/dev/null 2>&1; then
        tun_owned=1
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

resolve_target_routes() {
  resolved_address=''
  resolved_addresses=$("$python_bin" scripts/verify_cloudflare_health.py --url "$health_url" --resolve-addresses) || return 1
  test -n "$resolved_addresses" || return 1
  while IFS=' ' read -r address_family address; do
    case "$address_family" in
      4) route_output=$(sudo -n ip -4 route get "$address" 2>/dev/null) || continue ;;
      6) route_output=$(sudo -n ip -6 route get "$address" 2>/dev/null) || continue ;;
      *) return 1 ;;
    esac
    case "$route_output" in
      *"dev $tun_device"*) ;;
      *) continue ;;
    esac
    if [ -z "$resolved_address" ]; then
      resolved_address=$address
    fi
  done <<EOF
$resolved_addresses
EOF
  test -n "$resolved_address"
}

verify_country() {
  country=$1
  config_name=$2
  config_value=$3
  config_file="$runtime_dir/$config_name"
  if ! printf '%s' "$config_value" | sanitize_openvpn_config > "$config_file"; then
    printf 'OpenVPN configuration rejected via %s\n' "$country" >&2
    return 1
  fi
  if ! ensure_tun_absent; then
    printf '%s\n' 'VPN tunnel device is already present' >&2
    return 1
  fi

  sudo -n timeout --foreground --signal=TERM --kill-after=5s 45s openvpn \
    --config "$config_file" \
    --auth-user-pass "$auth_file" \
    --auth-nocache \
    --auth-retry nointeract \
    --dev "$tun_device" \
    --writepid "$pid_file" \
    --log "$vpn_log" > /dev/null 2>&1 &
  launcher_pid=$!
  vpn_started=1
  if ! wait_for_vpn; then
    printf 'VPN connection timed out via %s\n' "$country" >&2
    return 1
  fi
  if ! resolve_target_routes; then
    printf 'VPN target route unavailable via %s\n' "$country" >&2
    return 1
  fi
  sudo -n "$python_bin" scripts/verify_cloudflare_health.py \
    --url "$health_url" \
    --expected-version "$expected_version" \
    --resolved-address "$resolved_address" \
    --bind-device "$tun_device"
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
  if ! cleanup_vpn; then
    printf '%s\n' 'VPN cleanup failed; refusing next country' >&2
    exit 1
  fi
done

printf '%s\n' 'VPN health verification failed for all configured countries' >&2
exit 1
