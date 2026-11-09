#!/usr/bin/env bash
# Pure-logic helpers a consumer's collect-state.sh sources: secret redaction, the
# alert and event policy, the tri-state classifiers and the section helpers.
# Functions and patterns only. Contract: weisssrv-lib docs/SCRIPTS.md - collect-state-lib.sh.

# secret redaction

# A keyed secret's value: a double-quoted string with backslash escapes, a
# single-quoted string, or a run of non-space. Leftmost-longest takes a quoted one whole.
REDACT_VALUE='("([^"\\]|\\.)*"|'\''[^'\'']*'\''|[^[:space:]]+)'

# POSIX classes keep these portable across BSD and GNU sed. (^|[^[:alnum:]]) is
# line start or a separator, and `"?` admits a JSON-quoted key.
# shellcheck disable=SC2016 # All patterns are sed-quoted regexes; $-escapes are intentional literals
REDACT_PATTERNS=(
    's/password"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/password: <REDACTED>/gi'
    's/(^|[^[:alnum:]])token"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/\1token: <REDACTED>/gi'
    's/access_token"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/access_token: <REDACTED>/gi'
    's/refresh_token"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/refresh_token: <REDACTED>/gi'
    's/id_token"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/id_token: <REDACTED>/gi'
    's/bearer[[:space:]]+[A-Za-z0-9._~+\/=-]+/Bearer <REDACTED>/gi'
    's/authorization:[[:space:]]*basic[[:space:]]+[A-Za-z0-9+\/=]+/authorization: Basic <REDACTED>/gi'
    's/(^|[^[:alnum:]])secret"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/\1secret: <REDACTED>/gi'
    's/(^|[^[:alnum:]])api_?key"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/\1api_key: <REDACTED>/gi'
    's/CF_Token=[^[:space:]]+/CF_Token=<REDACTED>/g'
    's/CF_Account_ID=[^[:space:]]+/CF_Account_ID=<REDACTED>/g'
    's/SAVED_CF_Token=[^[:space:]]+/SAVED_CF_Token=<REDACTED>/g'
    's/SAVED_CF_Account_ID=[^[:space:]]+/SAVED_CF_Account_ID=<REDACTED>/g'
    's/\$2[aby]\$[0-9]+\$[A-Za-z0-9.\/]+/<BCRYPT_HASH>/g'
    's/client-certificate-data:[[:space:]]*[^[:space:]]+/client-certificate-data: <REDACTED>/g'
    's/client-key-data:[[:space:]]*[^[:space:]]+/client-key-data: <REDACTED>/g'
    's/certificate-authority-data:[[:space:]]*[^[:space:]]+/certificate-authority-data: <REDACTED>/g'
    's/OPENVPN_USER=[^[:space:]]+/OPENVPN_USER=<REDACTED>/g'
    's/OPENVPN_PASSWORD=[^[:space:]]+/OPENVPN_PASSWORD=<REDACTED>/g'
    's/WIREGUARD_(PRIVATE_KEY|PRESHARED_KEY)=[^[:space:]]+/WIREGUARD_\1=<REDACTED>/g'
    's/openvpn-user"?:[[:space:]]*'"$REDACT_VALUE"'/openvpn-user: <REDACTED>/g'
    's/openvpn-password"?:[[:space:]]*'"$REDACT_VALUE"'/openvpn-password: <REDACTED>/g'
    's/api-token"?:[[:space:]]*'"$REDACT_VALUE"'/api-token: <REDACTED>/g'
    's/oidc_client_id"?:[[:space:]]*'"$REDACT_VALUE"'/oidc_client_id: <REDACTED>/g'
    's/oidc_client_secret"?:[[:space:]]*'"$REDACT_VALUE"'/oidc_client_secret: <REDACTED>/g'
    's/client_id"?:[[:space:]]*'"$REDACT_VALUE"'/client_id: <REDACTED>/g'
    's/client_secret"?:[[:space:]]*'"$REDACT_VALUE"'/client_secret: <REDACTED>/g'
    's/glrt-[A-Za-z0-9_-]+/<GITLAB_RUNNER_TOKEN>/g'
    # The rest of the GitLab token family (glpat, gldt, glcbt, glsoat, glft,
    # glimt): at least 16 token characters after the prefix.
    's/gl(pat|dt|cbt|soat|ft|imt)-[A-Za-z0-9_-]{16,}/<GITLAB_TOKEN>/g'
    's/gh[oprsu]_[A-Za-z0-9]{30,}/<GITHUB_TOKEN>/g'
    # Model-provider API keys. sk-ant- and sk-proj- are longer forms of sk-, so
    # the alternation is ordered longest-first.
    's/sk-(ant|proj)-[A-Za-z0-9_-]{20,}/<MODEL_API_KEY>/g'
    's/sk-[A-Za-z0-9]{20,}/<MODEL_API_KEY>/g'
    # AWS-shaped access key ids (Backblaze B2 S3-compatible keys included).
    's/(AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}/<AWS_ACCESS_KEY_ID>/g'
    # B2 native credentials, named by key rather than by shape — the master
    # application key is high-entropy but has no recognisable prefix.
    's/[A-Za-z_]*application_key"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/application_key: <REDACTED>/gi'
    's/[A-Za-z_]*key_id"?[[:space:]]*[:=][[:space:]]*'"$REDACT_VALUE"'/key_id: <REDACTED>/gi'
    's/ops_[A-Za-z0-9_.-]{40,}/<OP_SA_TOKEN>/g'
    's/xox[abprs]-[A-Za-z0-9-]{10,}/<SLACK_TOKEN>/g'
    's/eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/<JWT>/g'
    's|https://discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+|https://discord.com/api/webhooks/<REDACTED>|g'
)

redact_file() {
    local infile="$1"
    local outfile="$2"
    local sed_args=()
    for pattern in "${REDACT_PATTERNS[@]}"; do
        sed_args+=(-e "$pattern")
    done
    # Second pass collapses whole PEM private-key blocks, which the single-line
    # s/// patterns cannot catch. awk, not sed: range-change syntax differs
    # between BSD and GNU sed, awk's does not.
    sed -E "${sed_args[@]}" "$infile" \
        | awk '
            /-----BEGIN [A-Z ]*PRIVATE KEY-----/ { print "<PRIVATE_KEY_REDACTED>"; inkey=1; next }
            /-----END [A-Z ]*PRIVATE KEY-----/   { inkey=0; next }
            inkey { next }
            { print }
        ' > "$outfile"
}

# remote section emitters, injected into every remote body via `declare -f`.
# They cap with an explicit truncation marker and emit <fallback> for empty
# input only; producer failure is detected by the caller's exit status.

# cs_capped <cap> <fallback>: print at most <cap> stdin lines (0 = uncapped),
# <fallback> when stdin was empty, a truncation marker when the cap clipped.
# The whole stream is consumed so the producer never takes SIGPIPE.
cs_capped() {
    local cap=$1 fallback=$2 n=0 line
    # `|| [ -n "$line" ]` keeps a final unterminated line (kubectl -o jsonpath
    # emits one) from being dropped by read's non-zero exit.
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        if [ "$cap" -eq 0 ] || [ "$n" -le "$cap" ]; then
            printf '%s\n' "$line"
        fi
    done
    if [ "$n" -eq 0 ]; then
        printf '%s\n' "$fallback"
    elif [ "$cap" -ne 0 ] && [ "$n" -gt "$cap" ]; then
        printf '  ... (truncated: showing %s of %s lines)\n' "$cap" "$n"
    fi
}

# cs_emit <fallback> — uncapped cs_capped, for sections that must never be
# clipped (ZFS datasets, df: the DR-critical inventories) or are short anyway.
cs_emit() {
    cs_capped 0 "$1"
}

# cs_journal_error_sources: `journalctl -o json` on stdin -> the 15 commonest
# sources as "<count> <name>" (a container by name), then "<count> (unparseable)".
# Non-zero when every non-blank line is unparseable.
cs_journal_error_sources() {
    python3 -c '
import collections, json, sys
counts = collections.Counter()
bad = 0
for line in sys.stdin:
    if not line.strip():
        continue
    try:
        entry = json.loads(line)
        names = [entry.get(k) for k in ("CONTAINER_NAME", "SYSLOG_IDENTIFIER", "_COMM")]
    except (ValueError, AttributeError):
        bad += 1
        continue
    counts[next((n for n in names if isinstance(n, str) and n), "?")] += 1
for name, count in counts.most_common(15):
    print(f"{count:7d} {name}")
if bad:
    print(f"{bad:7d} (unparseable)")
sys.exit(1 if bad and not counts else 0)
'
}

# cs_tailscale_status: the first five lines of `tailscale status`, the local
# node first, or why there are none.
# A failed sudo or tailscale call is reported with its first line.
cs_tailscale_status() {
    local out
    if ! command -v tailscale >/dev/null 2>&1; then
        echo "Tailscale not installed"
        return 0
    fi
    if ! out=$(sudo -n tailscale status 2>&1); then
        echo "Tailscale status FAILED: $(printf '%s\n' "$out" | head -1)"
    elif [ -z "$out" ]; then
        echo "Tailscale connected, no peers"
    else
        printf '%s\n' "$out" | head -5
    fi
}

# cs_head_tail <head> <tail> <fallback>: the first <head> and last <tail> (>= 1)
# stdin lines with an omission marker between, so a time-sorted listing shows
# both ends. <fallback> for empty input; the whole stream is consumed.
cs_head_tail() {
    awk -v h="$1" -v t="$2" -v fb="$3" '
        { n++; if (n <= h) print; else buf[n % t] = $0 }
        END {
            if (n == 0) { print fb; exit }
            start = (n - t > h) ? n - t + 1 : h + 1
            if (start > h + 1) printf "  ... (%d lines omitted)\n", start - h - 1
            for (i = start; i <= n; i++) print buf[i % t]
        }'
}

# cs_smart_ata_attrs: read `smartctl -A` on stdin and print the raw value of
# each attribute that predicts a failing disk, as "Name=raw" pairs on one line.
cs_smart_ata_attrs() {
    awk '
        $2 ~ /^(Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable|Reported_Uncorrect|Command_Timeout|Temperature_Celsius)$/ {
            printf "%s=%s ", $2, $10
        }'
}

# cs_zpool_leaf_paths: read `zpool status -P` on stdin and print
# "<pool>\t<device path>" for every leaf device of every pool.
cs_zpool_leaf_paths() {
    awk '
        $1 == "pool:" { pool = $2; next }
        pool != "" && $1 ~ /^\/dev\// { printf "%s\t%s\n", pool, $1 }'
}

# cs_pool_map: read "<pool>\t<leaf path>" lines on stdin and print
# "<whole-disk device>\t<pool>", walking a stacked leaf (dm-crypt, LVM) up to its
# disk. Returns 1, naming the path on stderr, when a leaf does not resolve.
cs_pool_map() {
    local pool path dev parent resolved rc=0
    while IFS=$'\t' read -r pool path; do
        resolved=false
        if dev=$(readlink -f "$path"); then
            for _ in 1 2 3 4 5 6 7 8; do
                parent=$(lsblk -no PKNAME "$dev") || break
                parent=${parent%%$'\n'*}
                if [ -z "$parent" ]; then
                    resolved=true
                    break
                fi
                dev="/dev/$parent"
            done
        fi
        if [ "$resolved" != true ]; then
            echo "  cannot resolve zpool leaf $path" >&2
            rc=1
            continue
        fi
        printf '%s\t%s\n' "$dev" "$pool"
    done
    return "$rc"
}

# cs_disk_pool <device> <map-rc>: read "<device>\t<pool>" lines on stdin and
# print the device's pool, "-" when it is in no pool, or "unknown" when the map
# is incomplete (map-rc non-zero).
cs_disk_pool() {
    awk -F'\t' -v d="$1" -v rc="$2" '
        p == "" && $1 == d { p = $2 }
        END { print (rc != 0 ? "unknown" : (p == "" ? "-" : p)) }'
}

# cs_snapshot_report: stdin is "D<TAB>dataset<TAB>usedbysnapshots<TAB>auto-snapshot"
# and "S<TAB>dataset@snap<TAB>creation" lines, oldest first. Prints each dataset's newest
# snapshot, count and space, flagging zfs-auto-snap ones auto-snapshot=false never prunes.
cs_snapshot_report() {
    awk -F'\t' '
        $1 == "D" { used[$2] = $3; auto[$2] = $4; next }
        $1 == "S" {
            split($2, a, "@")
            if (!(a[1] in count)) order[++n] = a[1]
            count[a[1]]++
            newest[a[1]] = $3
            if (a[2] ~ /^zfs-auto-snap/) autosnaps[a[1]]++
        }
        END {
            for (i = 1; i <= n; i++) {
                d = order[i]
                line = sprintf("  %s: newest %s, %d snapshots, usedbysnapshots %s", d, newest[d], count[d], (d in used) ? used[d] : "?")
                if (auto[d] == "false" && autosnaps[d] > 0)
                    line = line sprintf(" [auto-snapshot=false yet %d zfs-auto-snap kept: never pruned]", autosnaps[d])
                print line
            }
        }' | sort
}

# cs_retry_ssh <command...>: run an ssh command line once more when it exits
# 255, the code ssh reserves for its own failure. A one-off agent or network
# hiccup then does not read as an unreachable host. Never for a piped body.
cs_retry_ssh() {
    local rc=0
    "$@" || rc=$?
    if [ "$rc" -eq 255 ]; then
        rc=0
        sleep 1
        "$@" || rc=$?
    fi
    return "$rc"
}

# warning-event policy

# The jq program behind probe_warning_events. `excluded` is the consumer's jq
# filter over one event, false by default; an event it maps to true never counts.
# shellcheck disable=SC2016 # $cutoff is a jq --arg, not a shell variable
WARNING_EVENTS_JQ='
[ .items[]
  # Events-API events often carry only eventTime (lastTimestamp null), and jq
  # compares null >= $cutoff as false, so the timestamp is coalesced.
  | select(((.lastTimestamp // .eventTime // .metadata.creationTimestamp) // "") >= $cutoff)
  | select(excluded | not)
] | length'

# warning_events_filter <cutoff> [exclude] — count the stdin Warning events at or
# after <cutoff> (RFC3339) that [exclude] does not match, or echo "unknown" with
# jq's first error line on stderr when jq rejects the input or the filter.
warning_events_filter() {
    local cutoff="$1" exclude="${2:-false}" out err
    err=$(mktemp) || { echo "unknown"; return 0; }
    if ! out=$(jq --arg cutoff "$cutoff" "def excluded: ${exclude}; ${WARNING_EVENTS_JQ}" 2>"$err"); then
        echo "warning_events_filter: jq failed: $(head -1 "$err")" >&2
        out="unknown"
    fi
    rm -f "$err"
    echo "${out:-unknown}"
}

# alert policy

# firing_alerts_counts' jq: Watchdog, InfoInhibitor and severity=none never count,
# tenant-namespace alerts count apart, and a non-list input errors rather than reading 0.
# shellcheck disable=SC2016 # $tenants is a jq --arg, not a shell variable
FIRING_ALERTS_JQ='
(if type == "array" then . elif . == null then [] else error("not an alert list") end) as $alerts
| ($tenants | split(" ") | map(select(length > 0))) as $t
| [ $alerts[]
    | select(.labels.alertname != "Watchdog" and .labels.alertname != "InfoInhibitor")
    | select((.labels.severity // "") != "none")
    | (.labels.namespace // "") as $ns
    | ($t | index([$ns])) != null ]
| "\(map(select(. | not)) | length) \(map(select(.)) | length)"'

# firing_alerts_counts <tenant namespaces>: read `amtool alert query -o json` on
# stdin and echo "<platform> <tenant>" firing counts, or "unknown" when jq could
# not parse it. The namespaces are space-joined.
firing_alerts_counts() {
    local out
    out=$(jq -r --arg tenants "$1" "$FIRING_ALERTS_JQ" 2>/dev/null) || out="unknown"
    echo "${out:-unknown}"
}

# The jq program behind alerts_render: one line per alert, firing first.
# Input other than an array or null is an error.
# shellcheck disable=SC2016 # jq string interpolation, not shell
ALERTS_RENDER_JQ='
(if type == "array" then . elif . == null then [] else error("not an alert list") end)
| sort_by(((.status.state // "active") != "active"), (.labels.severity // ""), .labels.alertname)
| .[]
| "  \(.labels.alertname) [\(.labels.severity // "-")] \(.labels.namespace // .labels.instance // "-") since \(.startsAt // "?")"
  + (if (.status.state // "active") == "active" then ""
     else " (\(.status.state): \(((.status.silencedBy // []) + (.status.inhibitedBy // [])) | join(",")))" end)'

# alerts_render: read `amtool alert query -o json` on stdin and print one line
# per alert with name, severity, namespace or instance, start and silence or
# inhibit state. Non-zero when jq could not parse it or it is not an alert list.
alerts_render() {
    jq -r "$ALERTS_RENDER_JQ"
}

# coerce_int <value> <fallback> — echo <value> when it is all digits, else the
# fallback: 1 for a verdict input so an unaskable query never reads as healthy,
# null for the JSON so a consumer can tell "none" from "could not ask".
coerce_int() {
    case "$1" in
        ''|*[!0-9]*) printf '%s' "$2" ;;
        *) printf '%s' "$1" ;;
    esac
}

# tri-state health classification
# classify_regular derives its OK arm from regular_failing_predicates; a signal
# added to either classifier must be mirrored in the other.

# classify_regular <pve_reachable> <k3s_api_ok> <k3s_ready> <k3s_total>
#   <hosts_ok> <hosts_total> <coverage_pct> <coverage_floor> <flux_not_ready>
#   <zfs_degraded> <gitlab_ok> <sections_ok> <sections_total> <alerts_firing>

# FAILED: no Proxmox host reachable, K3s API up with zero nodes Ready, or host
# coverage below the floor. OK: every regular_failing_predicates entry holds.
# PARTIAL: anything else with core infra up, a bad kubeconfig included.
classify_regular() {
    local pve_reachable="$1" k3s_api_ok="$2" k3s_ready="$3"
    local coverage_pct="$7" coverage_floor="$8"
    if [ "$pve_reachable" -eq 0 ] \
       || { [ "$k3s_api_ok" = true ] && [ "$k3s_ready" -eq 0 ]; } \
       || [ "$coverage_pct" -lt "$coverage_floor" ]; then
        echo "FAILED"
    elif [ -z "$(regular_failing_predicates "$@")" ]; then
        echo "OK"
    else
        echo "PARTIAL"
    fi
}

# regular_failing_predicates — same 14 args as classify_regular. Prints the OK
# predicates that do NOT hold, with values; empty output == all-OK. gitlab_ok is
# 1 healthy and 0 unhealthy; a cluster without GitLab passes 1.
regular_failing_predicates() {
    local pve_reachable="$1" k3s_api_ok="$2" k3s_ready="$3" k3s_total="$4"
    local hosts_ok="$5" hosts_total="$6" coverage_pct="$7" coverage_floor="$8"
    local flux_not_ready="$9" zfs_degraded="${10}" gitlab_ok="${11}"
    local sections_ok="${12}" sections_total="${13}" alerts_firing="${14}"
    local out=""
    _rfp_add() { out="${out:+$out }$1"; }
    [ "$pve_reachable" -gt 0 ] || _rfp_add "pve_reachable=0"
    [ "$coverage_pct" -ge "$coverage_floor" ] || _rfp_add "coverage=${coverage_pct}%<${coverage_floor}%"
    [ "$hosts_ok" -eq "$hosts_total" ] || _rfp_add "hosts=${hosts_ok}/${hosts_total}"
    [ "$sections_ok" -eq "$sections_total" ] || _rfp_add "sections=${sections_ok}/${sections_total}"
    [ "$k3s_api_ok" = true ] || _rfp_add "k3s_api=unreachable"
    { [ "$k3s_total" -gt 0 ] && [ "$k3s_ready" -eq "$k3s_total" ]; } \
        || _rfp_add "k3s_nodes=${k3s_ready}/${k3s_total}"
    [ "$flux_not_ready" -eq 0 ] || _rfp_add "flux_not_ready=${flux_not_ready}"
    [ "$zfs_degraded" -eq 0 ] || _rfp_add "zfs_degraded=${zfs_degraded}"
    [ "$gitlab_ok" -eq 1 ] || _rfp_add "gitlab=unhealthy"
    [ "$alerts_firing" -eq 0 ] || _rfp_add "alerts_firing=${alerts_firing}"
    unset -f _rfp_add
    echo "$out"
}

# classify_json <pve_up> <pve_total> <k3s_api_ok> <k3s_ready> <k3s_total>
#   <flux_not_ready> <zfs_degraded> <gitlab_ok> <alerts_firing>
#   gitlab_ok is 1 healthy and 0 unhealthy; a cluster without GitLab passes 1.

# The --json verdict: healthy means full Proxmox coverage, no Flux or ZFS
# imperfection, GitLab healthy and nothing firing; degraded means any imperfection
# with core infra up; catastrophic is neither. Unknown arrives as 1 and so fires.
classify_json() {
    local pve_up="$1" pve_total="$2" k3s_api_ok="$3" k3s_ready="$4" k3s_total="$5"
    local flux_not_ready="$6" zfs_degraded="$7" gitlab_ok="$8" alerts_firing="${9:-0}"
    if [ "$pve_up" -gt 0 ] && [ "$pve_up" -eq "$pve_total" ] \
       && [ "$k3s_total" -gt 0 ] && [ "$k3s_ready" -eq "$k3s_total" ] \
       && [ "$flux_not_ready" -eq 0 ] && [ "$zfs_degraded" -eq 0 ] \
       && [ "$gitlab_ok" -eq 1 ] && [ "$alerts_firing" -eq 0 ]; then
        echo "healthy"
    elif { [ "$pve_up" -lt "$pve_total" ] || [ "$k3s_ready" -lt "$k3s_total" ] \
           || [ "$k3s_api_ok" != true ] || [ "$flux_not_ready" -gt 0 ] \
           || [ "$zfs_degraded" -gt 0 ] || [ "$gitlab_ok" -eq 0 ] \
           || [ "$alerts_firing" -gt 0 ]; } \
         && [ "$pve_up" -gt 0 ] \
         && { [ "$k3s_api_ok" != true ] || [ "$k3s_ready" -gt 0 ]; }; then
        echo "degraded"
    else
        echo "catastrophic"
    fi
}

# compose_active_sections <health_url> <nginx_cert> <backup_timer> <backup_prom>
# A "-" argument drops that section. Echoes the comma-joined active sections in
# output order (health,nginx,backup,metrics); `metrics` needs `backup`.
compose_active_sections() {
    local health_url=$1 nginx_cert=$2 backup_timer=$3 backup_prom=$4
    local out=""
    [ "$health_url" != "-" ] && out="${out:+$out,}health"
    [ "$nginx_cert" != "-" ] && out="${out:+$out,}nginx"
    if [ "$backup_timer" != "-" ]; then
        out="${out:+$out,}backup"
        [ "$backup_prom" != "-" ] && out="${out:+$out,}metrics"
    fi
    echo "$out"
}

# firewall_guest_fw_list: read candidate /etc/pve/firewall/*.fw paths on stdin,
# drop the cluster-wide cluster.fw and emit the per-guest paths sorted.
# Injected into the remote Proxmox body via `declare -f`.
firewall_guest_fw_list() {
    grep -v -E '(^|/)cluster\.fw$' | sort
}
