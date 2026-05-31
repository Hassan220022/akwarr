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
