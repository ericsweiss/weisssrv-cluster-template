#!/usr/bin/env bash
# Tenant admission policies refuse only later writes: run at rollout and before
# each onboarding to list tenant objects that predate them. Exit 0 clean,
# 1 findings, 2 kubectl or jq failed.

# Labels are tenant-writable, so they are never the proof: every EndpointSlice
# and Endpoints address must be a live Pod in the same namespace that the
# named Service's selector picks.
set -euo pipefail

TENANT_LABEL=fluxcd.io/tenant
CONTROLLER=endpointslice-controller.k8s.io
# owned(ip; selector): a live Pod carries ip and every selector label.
# shellcheck disable=SC2016 # a jq program, expanded by jq
OWNED='def owned($ip; $s): any($live[];
    (.ips | index($ip)) != null and (.labels as $l | $s | to_entries | all($l[.key] == .value)));'

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
    pods=$(kget -n "$ns" get pods)

    # Services the endpoint controller fills, name to selector.
    selected=$(jq -c '[.items[]
        | select((.spec.type // "") != "ExternalName" and ((.spec.selector // {}) | length) > 0)
        | {key: .metadata.name, value: .spec.selector}] | from_entries' <<<"$services") ||
        die "unreadable Services in $ns"
    # Non-terminal Pods with their IPs and labels.
    live=$(jq -c '[.items[]
        | select((.status.phase // "") as $p | $p != "Succeeded" and $p != "Failed")
        | {labels: (.metadata.labels // {}),
           ips: ([.status.podIP, (.status.podIPs // [])[].ip] | map(select(. != null and . != "")))}]' \
        <<<"$pods") || die "unreadable Pods in $ns"

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
        jq -r --arg ns "$ns" --argjson sel "$selected" --argjson live "$live" "$OWNED"'
            .items[] | .metadata.name as $n
            | ((.metadata.labels // {})["kubernetes.io/service-name"] // "") as $svc
            | if $svc == "" then "\($ns) EndpointSlice/\($n): no kubernetes.io/service-name label"
              elif ($sel | has($svc) | not) then "\($ns) EndpointSlice/\($n): no Service \($svc) with a selector"
              else (.endpoints // [])[] | (.addresses // [])[] as $a
                | select(owned($a; $sel[$svc]) | not)
                | "\($ns) EndpointSlice/\($n): address \($a) is not a Pod selected by Service/\($svc)"
              end' <<<"$slices" || echo "!endpointslices"
        jq -r --arg ns "$ns" --argjson sel "$selected" --argjson live "$live" "$OWNED"'
            .items[] | .metadata.name as $n
            | if ($sel | has($n) | not) then "\($ns) Endpoints/\($n): no Service with a selector"
              else (.subsets // [])[] | ((.addresses // []) + (.notReadyAddresses // []))[] | .ip as $a
                | select(owned($a; $sel[$n]) | not)
                | "\($ns) Endpoints/\($n): address \($a) is not a Pod selected by Service/\($n)"
              end' <<<"$endpoints" || echo "!endpoints"
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
