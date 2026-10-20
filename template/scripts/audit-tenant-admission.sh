#!/usr/bin/env bash
# Tenant admission policies refuse only later writes: run at rollout and before
# each onboarding to list the tenant objects they would refuse that predate them.
# Exit 0 clean, 1 findings, 2 kubectl or jq failed. Was audit-tenant-services.sh.

# Labels are tenant-writable, so they are never the proof: every EndpointSlice
# and Endpoints address must be a live Pod in the same namespace that the
# named Service's selector picks.
set -euo pipefail

TENANT_LABEL=fluxcd.io/tenant
CONTROLLER=endpointslice-controller.k8s.io
FLUX_NAMESPACE=flux-system
GRANT_PREFIX=tenant-routing-
# The allowlists in tenant-traefik-policy.yaml, verbatim; a test compares them.
PLATFORM_MIDDLEWARES='["traefik/lan-tailscale-only", "traefik/lan-tailscale-strict", "traefik/hsts-header", "traefik/security-headers", "authentik/authentik-auth"]'
PLATFORM_REFS='["traefik-lan-tailscale-only@kubernetescrd", "traefik-lan-tailscale-strict@kubernetescrd", "traefik-hsts-header@kubernetescrd", "traefik-security-headers@kubernetescrd", "authentik-authentik-auth@kubernetescrd"]'
PLATFORM_FORWARD_AUTH='["http://authentik-server.authentik.svc.cluster.local/outpost.goauthentik.io/auth/traefik"]'
PLATFORM_ISSUERS='["letsencrypt-prod", "letsencrypt-staging"]'
OWN_SERVICE_HEAD='^https?://[a-z]([-a-z0-9]*[a-z0-9])?[.]'
OWN_SERVICE_TAIL='[.]svc([.]cluster[.]local)?(:[0-9]{1,5})?(/.*)?$'
CERT_MANAGER_SA=system:serviceaccount:cert-manager:cert-manager
# tenant-pod-volumes' allowlist: the restricted volume set and each volume's name.
RESTRICTED_VOLUME_KEYS='["name", "configMap", "csi", "downwardAPI", "emptyDir", "ephemeral", "persistentVolumeClaim", "projected", "secret"]'

# owned(ip; selector): a live Pod carries ip and every selector label.
# shellcheck disable=SC2016 # a jq program, expanded by jq
OWNED='def owned($ip; $s): any($live[];
    (.ips | index($ip)) != null and (.labels as $l | $s | to_entries | all($l[.key] == .value)));'

# Shared by the policy-mirroring programs; $g is the namespace's grant.
# shellcheck disable=SC2016 # a jq program, expanded by jq
DEFS='
def own: (has("namespace") | not) or .namespace == $ns;
def at: (. // "") | tostring | contains("@");
def trimmed: gsub("^\\s+|\\s+$"; "");
def granted: . as $h | any($g.hosts[]; . == $h);
def mwref($o; $what): "\(.namespace)/\(.name)" as $k
    | (if own or any($c.middlewares[]; . == $k) then empty
       else "\($o): \($what) \(.namespace)/\(.name) outside its namespace and the platform allowlist" end),
      (if .name | at then "\($o): \($what) \(.name) uses a name@provider reference" else empty end);
def svcref($o; $what):
    (if own then empty else "\($o): \($what) \(.namespace)/\(.name) in another namespace" end),
    (if (.name | at) or (.serversTransport | at)
     then "\($o): \($what) \(.name) uses a name@provider reference" else empty end),
    ((.middlewares // [])[] | mwref($o; "\($what) middleware"));
def refs($o): (.metadata.annotations // {}) | to_entries[]
    | select(.key | startswith("traefik.ingress.kubernetes.io/")) | .key as $k
    | .value | split(",")[] | trimmed as $v
    | select(($v | contains("@")) and (any($c.refs[]; . == $v) | not))
    | "\($o): annotation \($k) names \($v), outside the platform allowlist";
def tailscale($o): (.metadata.annotations // {}) | keys[] | select(startswith("tailscale.com/"))
    | "\($o): annotation \(.) publishes through the Tailscale operator";
'

# One program per policy-covered kind, each evaluated per object with $o its
# label; host checks report the unusable grant once instead.
# shellcheck disable=SC2016 # jq programs, expanded by jq
{
    UNGRANTED='if $g.problem != null then "\($o): \($g.problem)" else'
    ROUTE_CHECKS=$UNGRANTED'
        ((.spec.routes // [])[] | (.match // "") as $m
            | (if ($m | test("^Host[(]`[^`]+`[)]( && .*)?$")) and ($m | contains("||") | not)
                  and ($m | contains("HostRegexp(") | not)
                  and all($m | scan("Host[(]`[^`]*`[)]"); .[6:-2] | granted)
               then empty else "\($o): match \($m) is not Host() on granted hostnames only" end),
              (if has("priority") and ($g.max == null or .priority > $g.max)
               then "\($o): priority \(.priority) above the grant'"'"'s maxPriority \($g.max // "(none)")"
               else empty end)),
        ((.spec.tls.domains // [])[] | (.main // empty), (.sans // [])[] | select(granted | not)
            | "\($o): TLS domain \(.) is not granted")
        end,
        ((.spec.routes // [])[]
            | ((.services // [])[] | svcref($o; "service")),
              ((.middlewares // [])[] | mwref($o; "middleware"))),
        ((.spec.tls // {}) | (.options // empty | ["options", .]), (.store // empty | ["store", .])
            | .[0] as $w | .[1] | select(own and (.name | at | not) | not)
            | "\($o): TLS \($w) \(.namespace // $ns)/\(.name) outside its namespace"),
        (if (.spec.parentRefs // []) | length > 0
         then "\($o): parentRefs attach to parent routes, which is platform-owned" else empty end)'
    INGRESS_CHECKS=$UNGRANTED'
        (if .spec.defaultBackend != null then "\($o): default backend answers every hostname" else empty end),
        ((.spec.rules // [])[] | if (.host // "") == "" then "\($o): rule without a host"
            elif (.host | granted | not) then "\($o): host \(.host) is not granted" else empty end),
        ((.spec.tls // [])[] | (.hosts // [])[] | select(granted | not) | "\($o): TLS host \(.) is not granted"),
        ((.metadata.annotations // {}) | to_entries[]
            | select(.key | ascii_downcase | startswith("traefik.ingress.kubernetes.io/router.tls.domains."))
            | .value | split(",")[] | trimmed | select(granted | not)
            | "\($o): router.tls.domains name \(.) is not granted")
        end,
        ((.metadata.annotations // {}) | keys[] | select(ascii_downcase | IN(
            "traefik.ingress.kubernetes.io/router.rule", "traefik.ingress.kubernetes.io/router.priority",
            "traefik.ingress.kubernetes.io/router.rulesyntax")) | "\($o): annotation \(.) is refused"),
        (.spec.ingressClassName // "traefik" | select(. != "traefik")
            | "\($o): ingressClassName \(.) is not traefik"),
        ((.metadata.annotations // {})["kubernetes.io/ingress.class"] // "traefik" | select(. != "traefik")
            | "\($o): annotation kubernetes.io/ingress.class \(.) is not traefik"),
        tailscale($o), refs($o)'
    CERTIFICATE_CHECKS='.spec as $s
        | (if $g.problem != null then "\($o): \($g.problem)" else
            (($s.dnsNames // [])[], ($s.commonName // empty)
            | select(contains("*") or (granted | not)) | "\($o): name \(.) is not granted") end),
          ("literalSubject", "ipAddresses", "uris", "emailAddresses", "otherNames"
            | select(. as $f | $s | has($f)) | "\($o): sets \(.)"),
          ($s.issuerRef // {} | select(((.group // "") | IN("", "cert-manager.io"))
                and (if .kind == "ClusterIssuer" then (.name as $i | any($c.issuers[]; . == $i))
                     else (.kind // "") | IN("", "Issuer") end) | not)
            | "\($o): issuer \(.kind // "Issuer")/\(.name) is not a platform ClusterIssuer or an Issuer in its namespace")'
    MIDDLEWARE_CHECKS='.spec as $s
        | ($s.forwardAuth // empty | (.address // "") as $a
            | if any($c.forwardAuth[]; . == $a) or ($a | test($c.head + $ns + $c.tail)) then empty
              else "\($o): forwardAuth address \($a) is neither a Service in \($ns) nor the platform outpost" end),
          (($s.chain.middlewares // [])[] | svcref($o; "chain middleware")),
          ($s.errors.service // empty | svcref($o; "errors service")),
          ($s.rateLimit.redis // empty | "\($o): rateLimit names a Redis backend, which Traefik would connect to with its own egress")'
    TRAEFIKSERVICE_CHECKS='.spec as $s
        | (($s.weighted.services // [])[] | svcref($o; "weighted service")),
          (($s.highestRandomWeight.services // [])[] | svcref($o; "highestRandomWeight service")),
          ($s.mirroring // empty | svcref($o; "mirroring service"),
            ((.mirrors // [])[] | svcref($o; "mirror service"))),
          ($s.failover // empty | "\($o): failover services are not supported")'
    REQUEST_CHECKS='select((.spec.username // "") != $c.sa) | (.spec.username // "(no username)") as $u
        | .spec.issuerRef // {} | select(((.group // "") | IN("", "cert-manager.io"))
            and ((.kind // "") | IN("", "Issuer")) | not)
        | "\($o): \($u) asked \(.kind)/\(.name), not a core Issuer in its namespace"'
    TCP_UDP_CHECKS='"\($o): tenants may not route TCP or UDP"'
    DEFAULT_TLS_CHECKS='select($n == "default") | "\($o): the default \($kind) is platform-owned"'
    SERVICE_CHECKS='tailscale($o), refs($o)'
    VOLUME_CHECKS='(if $kind == "Pod" then .spec.volumes
            elif $kind == "CronJob" then .spec.jobTemplate.spec.template.spec.volumes
            else .spec.template.spec.volumes end) // []
        | .[] | .name as $v | keys[] | select(. as $k | any($c.volumes[]; . == $k) | not)
        | "\($o): volume \($v) uses \(.), outside the restricted volume set"'
}

die() {
    echo "audit-tenant-admission: $*" >&2
    exit 2
}

command -v jq >/dev/null || die "jq is required"

kget() {
    kubectl "$@" -o json || die "kubectl $* failed"
}

# audit KIND JSON PROGRAM: one policy-mirroring program over a kind's list,
# or a "!KIND" marker the caller turns into exit 2.
audit() {
    jq -r --arg ns "$ns" --arg kind "$1" --argjson g "$grant" --argjson c "$constants" \
        "$DEFS"'.items[] | .metadata.name as $n | "\($ns) \($kind)/\($n)" as $o | '"$3" \
        <<<"$2" || echo "!$1"
}

namespaces_json=$(kget get namespaces -l "$TENANT_LABEL")
namespaces=$(jq -r '.items[].metadata.name' <<<"$namespaces_json") || die "unreadable namespace list"
if [ -z "$namespaces" ]; then
    echo "audit-tenant-admission: OK (no $TENANT_LABEL namespaces)"
    exit 0
fi

# shellcheck disable=SC2016 # a jq program, expanded by jq
constants=$(jq -nc --argjson m "$PLATFORM_MIDDLEWARES" --argjson r "$PLATFORM_REFS" \
    --argjson f "$PLATFORM_FORWARD_AUTH" --argjson i "$PLATFORM_ISSUERS" \
    --arg h "$OWN_SERVICE_HEAD" --arg t "$OWN_SERVICE_TAIL" --arg sa "$CERT_MANAGER_SA" \
    --argjson v "$RESTRICTED_VOLUME_KEYS" \
    '{middlewares: $m, refs: $r, forwardAuth: $f, issuers: $i, head: $h, tail: $t, sa: $sa,
      volumes: $v}') ||
    die "unreadable allowlists"
grants=$(kget -n "$FLUX_NAMESPACE" get configmaps)

findings=()
count=0
for ns in $namespaces; do
    count=$((count + 1))
    services=$(kget -n "$ns" get services)
    slices=$(kget -n "$ns" get endpointslices.discovery.k8s.io)
    endpoints=$(kget -n "$ns" get endpoints)
    claims=$(kget -n "$ns" get persistentvolumeclaims)
    pods=$(kget -n "$ns" get pods)
    routes=$(kget -n "$ns" get ingressroutes.traefik.io)
    tcp_routes=$(kget -n "$ns" get ingressroutetcps.traefik.io)
    udp_routes=$(kget -n "$ns" get ingressrouteudps.traefik.io)
    middlewares=$(kget -n "$ns" get middlewares.traefik.io)
    traefik_services=$(kget -n "$ns" get traefikservices.traefik.io)
    tls_options=$(kget -n "$ns" get tlsoptions.traefik.io)
    tls_stores=$(kget -n "$ns" get tlsstores.traefik.io)
    ingresses=$(kget -n "$ns" get ingresses.networking.k8s.io)
    certificates=$(kget -n "$ns" get certificates.cert-manager.io)
    requests=$(kget -n "$ns" get certificaterequests.cert-manager.io)
    deployments=$(kget -n "$ns" get deployments.apps)
    statefulsets=$(kget -n "$ns" get statefulsets.apps)
    daemonsets=$(kget -n "$ns" get daemonsets.apps)
    replicasets=$(kget -n "$ns" get replicasets.apps)
    jobs=$(kget -n "$ns" get jobs.batch)
    cronjobs=$(kget -n "$ns" get cronjobs.batch)

    # The grant: hosts, maxPriority as the isolation gate takes it (ASCII digits
    # to 2^31-1; \A..\z, as $ matches before a final newline), and any problem.
    # shellcheck disable=SC2016 # a jq program, expanded by jq
    grant=$(jq -c --arg name "$GRANT_PREFIX$ns" --arg where "$FLUX_NAMESPACE" '
        (first(.items[] | select(.metadata.name == $name)) // null) as $cm
        | (($cm.data.hosts // "") | split("\n") | map(gsub("^\\s+|\\s+$"; "")) | map(select(. != ""))) as $h
        | ($cm.data.maxPriority // null) as $m
        | (($m | type) == "string" and ($m | test("\\A[0-9]+\\z"))
            and ($m | tonumber) <= 2147483647) as $ok
        | {hosts: $h,
           max: (if $ok then ($m | tonumber) else null end),
           problem: (if $cm == null then "no hostname grant \($where)/\($name)"
                     elif any($h[]; contains("*")) then "grant \($where)/\($name) lists a wildcard host"
                     elif $m != null and ($ok | not)
                     then "grant \($where)/\($name) has an unparseable maxPriority \($m | tojson)"
                     else null end)}' <<<"$grants") || die "unreadable ConfigMaps in $FLUX_NAMESPACE"

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
        jq -r --arg ns "$ns" '.items[] | .metadata.name as $n | (.spec.type // "ClusterIP") as $t
            | (if $t != "ClusterIP" then "\($ns) Service/\($n): type \($t)" else empty end),
              (if $t != "ExternalName" and ((.spec.selector // {}) | length) == 0
               then "\($ns) Service/\($n): no selector" else empty end),
              (if ((.spec.externalIPs // []) | length) > 0
               then "\($ns) Service/\($n): externalIPs \(.spec.externalIPs | join(","))"
               else empty end)' <<<"$services" || echo "!services"
        # Only the Service-driven controller is accepted: a slice the MIRRORING
        # controller manages mirrors a hand-written Endpoints object, which the
        # policies refuse to recreate, so it is a finding rather than noise.
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
        audit Service "$services" "$SERVICE_CHECKS"
        audit IngressRoute "$routes" "$ROUTE_CHECKS"
        audit IngressRouteTCP "$tcp_routes" "$TCP_UDP_CHECKS"
        audit IngressRouteUDP "$udp_routes" "$TCP_UDP_CHECKS"
        audit Middleware "$middlewares" "$MIDDLEWARE_CHECKS"
        audit TraefikService "$traefik_services" "$TRAEFIKSERVICE_CHECKS"
        audit TLSOption "$tls_options" "$DEFAULT_TLS_CHECKS"
        audit TLSStore "$tls_stores" "$DEFAULT_TLS_CHECKS"
        audit Ingress "$ingresses" "$INGRESS_CHECKS"
        audit Certificate "$certificates" "$CERTIFICATE_CHECKS"
        audit CertificateRequest "$requests" "$REQUEST_CHECKS"
        audit Pod "$pods" "$VOLUME_CHECKS"
        audit Deployment "$deployments" "$VOLUME_CHECKS"
        audit StatefulSet "$statefulsets" "$VOLUME_CHECKS"
        audit DaemonSet "$daemonsets" "$VOLUME_CHECKS"
        audit ReplicaSet "$replicasets" "$VOLUME_CHECKS"
        audit Job "$jobs" "$VOLUME_CHECKS"
        audit CronJob "$cronjobs" "$VOLUME_CHECKS"
        # tenant-pvc-storageclasses: tenant-local, or "" for a platform-written static volume.
        jq -r --arg ns "$ns" '.items[] | (if (.spec | has("storageClassName")) then .spec.storageClassName else null end) as $s
            | if $s == "" and ((.spec.volumeName // "") == "")
              then "\($ns) PersistentVolumeClaim/\(.metadata.name): storageClassName \"\" without volumeName"
              elif $s != "" and $s != "tenant-local"
              then "\($ns) PersistentVolumeClaim/\(.metadata.name): storageClassName \($s // "unset")"
              else empty end' <<<"$claims" ||
            echo "!persistentvolumeclaims"
    )
done

for line in "${findings[@]+"${findings[@]}"}"; do
    case $line in
    "!"*) die "unreadable ${line#!} JSON" ;;
    esac
done

if [ "${#findings[@]}" -gt 0 ]; then
    printf '%s\n' "${findings[@]}"
    echo "audit-tenant-admission: ${#findings[@]} finding(s) the tenant policies refuse on the next write." >&2
    echo "    Fix each as the tenants README describes: grant, routes, Services, endpoints, certificates, volumes, claims." >&2
    exit 1
fi
echo "audit-tenant-admission: OK ($count tenant namespace(s))"
