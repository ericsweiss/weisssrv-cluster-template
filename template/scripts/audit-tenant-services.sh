#!/usr/bin/env bash
# Tenant admission policies refuse only later writes: run at rollout and before
# each onboarding to list tenant objects that predate them. Exit 0 clean,
# 1 findings, 2 kubectl or jq failed.
set -euo pipefail

TENANT_LABEL=fluxcd.io/tenant
CONTROLLER=endpointslice-controller.k8s.io

die() {
    echo "audit-tenant-services: $*" >&2
    exit 2
}

command -v jq >/dev/null || die "jq is required"

kget() {
    kubectl "$@" -o json || die "kubectl $* failed"
}

namespaces_json=$(kget get namespaces -l "$TENANT_LABEL")
namespaces=$(jq -r '.items[].metadata.name' <<<"$namespaces_json") || die "unreadable namespace list"
if [ -z "$namespaces" ]; then
    echo "audit-tenant-services: OK (no $TENANT_LABEL namespaces)"
    exit 0
fi

findings=()
count=0
for ns in $namespaces; do
    count=$((count + 1))
    services=$(kget -n "$ns" get services)
    slices=$(kget -n "$ns" get endpointslices.discovery.k8s.io)
    endpoints=$(kget -n "$ns" get endpoints)

    # Services the endpoint controller fills: a selector and not ExternalName.
    selected=$(jq -c '[.items[]
        | select((.spec.type // "") != "ExternalName" and ((.spec.selector // {}) | length) > 0)
        | .metadata.name]' <<<"$services") || die "unreadable Services in $ns"

    while IFS= read -r line; do
        [ -n "$line" ] && findings+=("$line")
    done < <(
        jq -r --arg ns "$ns" '.items[] | .metadata.name as $n
            | if (.spec.type // "") == "ExternalName" then "\($ns) Service/\($n): type ExternalName"
              elif ((.spec.selector // {}) | length) == 0 then "\($ns) Service/\($n): no selector"
              else empty end' <<<"$services" || echo "!services"
        jq -r --arg ns "$ns" --arg c "$CONTROLLER" '.items[]
            | (.metadata.labels // {})["endpointslice.kubernetes.io/managed-by"] as $m
            | select($m != $c)
            | "\($ns) EndpointSlice/\(.metadata.name): managed-by \($m // "unset")"' <<<"$slices" ||
            echo "!endpointslices"
        jq -r --arg ns "$ns" --argjson selected "$selected" '.items[]
            | .metadata.name as $n
            | select($selected | index([$n]) | not)
            | "\($ns) Endpoints/\($n): no Service with a selector"' <<<"$endpoints" ||
            echo "!endpoints"
    )
done

for line in "${findings[@]+"${findings[@]}"}"; do
    case $line in
    "!"*) die "unreadable ${line#!} JSON" ;;
    esac
done

if [ "${#findings[@]}" -gt 0 ]; then
    printf '%s\n' "${findings[@]}"
    echo "audit-tenant-services: ${#findings[@]} object(s) the tenant policies would refuse." >&2
    echo "    Fix: give each Service a selector, drop ExternalName, delete hand-written endpoints." >&2
    exit 1
fi
echo "audit-tenant-services: OK ($count tenant namespace(s))"
