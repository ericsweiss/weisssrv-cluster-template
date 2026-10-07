#!/usr/bin/env bash
# Assert each pin written twice holds one value: an `include:` input against its
# `variables:` counterpart, and a pin copied into several script: blocks.
# Usage: scripts/check-ci-pin-parity.sh [CI_FILE]   (repo root; 1 on drift).
set -uo pipefail

CI_FILE="${1:-.gitlab-ci.yml}"
rc=0

# Floor assertion on the derivation itself: a parser regression that derived
# nothing would otherwise pass by inspecting zero pins.
FLOOR_PINS="ansible_version"
# The same floor for the script:-block arm, whose pins no `variables:` key backs.
SHELL_FLOOR_PINS="FLUX_VERSION"

var() { sed -n "s/^  $1: \"\(.*\)\"\$/\1/p" "$CI_FILE" | head -1; }
inp() { sed -n "s/^      $1: \"\(.*\)\"\$/\1/p" "$CI_FILE" | sort -u; }

# Every input key that also names a `variables:` key. `tr` handles the
# lower_snake -> UPPER_SNAKE convention the include inputs follow.
derive_pins() {
    sed -n 's/^      \([a-z0-9_]*\): ".*"$/\1/p' "$CI_FILE" | sort -u | while read -r key; do
        [ -n "$key" ] || continue
        upper=$(echo "$key" | tr '[:lower:]' '[:upper:]')
        [ -n "$(var "$upper")" ] && echo "$key"
    done
}

# Shell pins: `NAME="value"` inside a script: block. Names, then the distinct
# values one name carries across the file.
shell_pin_names() { sed -n 's/^[[:space:]]*\([A-Z][A-Z0-9_]*\)="[^"]*"$/\1/p' "$CI_FILE" | sort -u; }
shell_pin_values() { sed -n "s/^[[:space:]]*$1=\"\([^\"]*\)\"\$/\1/p" "$CI_FILE" | sort -u; }

cmp_pin() {
    echo "$1: variables=${2:-<none>} include-input(s)=$(echo "$3" | tr '\n' ' ')"
    if [ -z "$2" ] || [ -z "$3" ]; then
        echo "  could not extract both sides"
        rc=1
        return
    fi
    if [ "$2" != "$3" ]; then
        echo "  DRIFT — bump both, they are one pin"
        rc=1
    fi
}

pins=$(derive_pins)

# The floor: a derivation that stopped seeing a known pin is a broken gate, not
# a clean pipeline.
for floor in $FLOOR_PINS; do
    if ! echo "$pins" | grep -qx "$floor"; then
        echo "$floor: derived from neither side — the include-input parser no longer sees it"
        rc=1
    fi
done

for key in $pins; do
    upper=$(echo "$key" | tr '[:lower:]' '[:upper:]')
    cmp_pin "$key" "$(var "$upper")" "$(inp "$key")"
done

shell_pins=$(shell_pin_names)
for floor in $SHELL_FLOOR_PINS; do
    if ! echo "$shell_pins" | grep -qx "$floor"; then
        echo "$floor: no shell assignment found — the script:-block parser no longer sees it"
        rc=1
    fi
done

for name in $shell_pins; do
    values=$(shell_pin_values "$name")
    if [ "$(echo "$values" | wc -l | tr -d ' ')" -ne 1 ]; then
        echo "$name: script-block copies=$(echo "$values" | tr '\n' ' ')"
        echo "  DRIFT — bump every copy, they are one pin"
        rc=1
    fi
done

exit "$rc"
