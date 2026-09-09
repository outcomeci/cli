#!/usr/bin/env bash
set -euo pipefail

current_tag=${1:-}
output_file=${2:-}
if [[ ! $current_tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ -z $output_file ]]; then
  echo "usage: release-range.sh vMAJOR.MINOR.PATCH OUTPUT" >&2
  exit 1
fi
git rev-parse --verify "${current_tag}^{commit}" >/dev/null
previous_tag=$(git tag --merged "$current_tag" --list 'v*' | awk '$0 ~ /^v[0-9]+\.[0-9]+\.[0-9]+$/' | awk -v current="$current_tag" '$0 != current' | sort -V -r | head -n 1)
if [[ -n $previous_tag ]]; then
  from_revision=$previous_tag
else
  from_revision=$(git rev-list --max-parents=0 "$current_tag" | tail -n 1)
fi
if [[ -z $from_revision ]] || [[ $(git rev-parse "${from_revision}^{commit}") == $(git rev-parse "${current_tag}^{commit}") ]]; then
  echo "release range is empty" >&2
  exit 1
fi
printf 'from=%s\nto=%s\n' "$from_revision" "$current_tag" >> "$output_file"
