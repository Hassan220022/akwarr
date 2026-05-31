#!/usr/bin/env bash

aria2_limit_from_mbit() {
  local total_mbit="$1"
  local percent="$2"

  if ! [[ "${total_mbit}" =~ ^[0-9]+$ ]] || [[ "${total_mbit}" -le 0 ]]; then
    echo "TOTAL_BANDWIDTH_MBIT must be a positive integer" >&2
    return 2
  fi
  if ! [[ "${percent}" =~ ^[0-9]+$ ]] || [[ "${percent}" -le 0 ]] || [[ "${percent}" -gt 100 ]]; then
    echo "ARIA2_BANDWIDTH_LIMIT_PERCENT must be an integer from 1 to 100" >&2
    return 2
  fi

  local kib_per_second=$((total_mbit * 1000 * 1000 * percent / 100 / 8 / 1024))
  if [[ "${kib_per_second}" -lt 1 ]]; then
    kib_per_second=1
  fi
  printf '%sK\n' "${kib_per_second}"
}

aria2_limit_from_bytes_per_second() {
  local bytes_per_second="$1"
  local percent="$2"

  if ! awk -v value="${bytes_per_second}" 'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value > 0) }'; then
    echo "measured download speed must be a positive number of bytes per second" >&2
    return 2
  fi
  if ! [[ "${percent}" =~ ^[0-9]+$ ]] || [[ "${percent}" -le 0 ]] || [[ "${percent}" -gt 100 ]]; then
    echo "ARIA2_BANDWIDTH_LIMIT_PERCENT must be an integer from 1 to 100" >&2
    return 2
  fi

  awk -v bytes="${bytes_per_second}" -v percent="${percent}" 'BEGIN {
    kib_per_second = int(bytes * percent / 100 / 1024)
    if (kib_per_second < 1) {
      kib_per_second = 1
    }
    printf "%dK\n", kib_per_second
  }'
}

measure_download_bytes_per_second() {
  local url="$1"
  local max_time="$2"
  local curl_status=0
  local speed

  speed="$(
    curl --fail --location --silent --show-error \
      --user-agent 'Mozilla/5.0' \
      --connect-timeout 10 \
      --max-time "${max_time}" \
      --output /dev/null \
      --write-out '%{speed_download}' \
      "${url}"
  )" || curl_status=$?
  if ! awk -v value="${speed}" 'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value > 0) }'; then
    echo "failed to measure download speed from ${url} (curl exit ${curl_status})" >&2
    return 2
  fi
  printf '%s\n' "${speed}"
}

mbit_from_bytes_per_second() {
  local bytes_per_second="$1"

  if ! awk -v value="${bytes_per_second}" 'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value > 0) }'; then
    echo "measured download speed must be a positive number of bytes per second" >&2
    return 2
  fi
  awk -v bytes="${bytes_per_second}" 'BEGIN { printf "%.2f\n", bytes * 8 / 1000000 }'
}
