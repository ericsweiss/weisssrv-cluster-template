#!/usr/bin/env bash
# Pure-logic helpers a consumer's collect-state.sh sources: secret redaction,
# the tri-state health classifiers, the section emitters. Functions and patterns
# only. Contract: weisssrv-lib docs/SCRIPTS.md - collect-state-lib.sh.

# secret redaction

# POSIX classes ([[:space:]], not \s) keep these portable across BSD and GNU
# sed. The (^|[^[:alnum:]]) prefix matches at line start OR after a separator.
# shellcheck disable=SC2016 # All patterns are sed-quoted regexes; $-escapes are intentional literals
REDACT_PATTERNS=(
    's/password[[:space:]]*[:=][[:space:]]*[^[:space:]]+/password: <REDACTED>/gi'
    's/(^|[^[:alnum:]])token[[:space:]]*[:=][[:space:]]*[^[:space:]]+/\1token: <REDACTED>/gi'
    's/access_token[[:space:]]*[:=][[:space:]]*[^[:space:]]+/access_token: <REDACTED>/gi'
    's/refresh_token[[:space:]]*[:=][[:space:]]*[^[:space:]]+/refresh_token: <REDACTED>/gi'
    's/id_token[[:space:]]*[:=][[:space:]]*[^[:space:]]+/id_token: <REDACTED>/gi'
    's/bearer[[:space:]]+[A-Za-z0-9._~+\/=-]+/Bearer <REDACTED>/gi'
    's/authorization:[[:space:]]*basic[[:space:]]+[A-Za-z0-9+\/=]+/authorization: Basic <REDACTED>/gi'
    's/(^|[^[:alnum:]])secret[[:space:]]*[:=][[:space:]]*[^[:space:]]+/\1secret: <REDACTED>/gi'
    's/(^|[^[:alnum:]])api_?key[[:space:]]*[:=][[:space:]]*[^[:space:]]+/\1api_key: <REDACTED>/gi'
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
    's/openvpn-user:[[:space:]]*[^[:space:]]+/openvpn-user: <REDACTED>/g'
    's/openvpn-password:[[:space:]]*[^[:space:]]+/openvpn-password: <REDACTED>/g'
    's/api-token:[[:space:]]*[^[:space:]]+/api-token: <REDACTED>/g'
    's/oidc_client_id:[[:space:]]*[^[:space:]]+/oidc_client_id: <REDACTED>/g'
    's/oidc_client_secret:[[:space:]]*[^[:space:]]+/oidc_client_secret: <REDACTED>/g'
    's/client_id:[[:space:]]*[^[:space:]]+/client_id: <REDACTED>/g'
    's/client_secret:[[:space:]]*[^[:space:]]+/client_secret: <REDACTED>/g'
    's/glrt-[A-Za-z0-9_-]+/<GITLAB_RUNNER_TOKEN>/g'
    # The rest of the GitLab token family (PAT, deploy, CI build) — shaped like
    # glrt- but distinct prefixes, so the pattern above never matched them.
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
    's/[A-Za-z_]*application_key[[:space:]]*[:=][[:space:]]*[^[:space:]]+/application_key: <REDACTED>/gi'
    's/[A-Za-z_]*key_id[[:space:]]*[:=][[:space:]]*[^[:space:]]+/key_id: <REDACTED>/gi'
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

# warning-event policy

# The jq program behind probe_warning_events, kept here so the counting rule is
# readable and unit-tested rather than a single line inside a remote body.
# shellcheck disable=SC2016 # $cutoff is a jq --arg, not a shell variable
WARNING_EVENTS_JQ='
[ .items[]
  # Events-API events often carry only eventTime (lastTimestamp null), and jq
  # compares null >= $cutoff as false, so the timestamp is coalesced.
  | select(((.lastTimestamp // .eventTime // .metadata.creationTimestamp) // "") >= $cutoff)
] | length'

# warning_events_filter <cutoff> — read `kubectl get events -A
# --field-selector type=Warning -o json` on stdin and echo the number of Warning
# events at or after <cutoff> (RFC3339), or "unknown" when jq could not parse it.
warning_events_filter() {
    local cutoff="$1" out
    out=$(jq --arg cutoff "$cutoff" "$WARNING_EVENTS_JQ" 2>/dev/null) || out="unknown"
    echo "${out:-unknown}"
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
#   <zfs_degraded> <sections_ok> <sections_total> <alerts_firing>

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

# regular_failing_predicates — same 13 args as classify_regular. Prints the
# space-separated names (with values) of the OK predicates that do NOT hold, so
# the console summary names its cause. Empty output == all-OK.
regular_failing_predicates() {
    local pve_reachable="$1" k3s_api_ok="$2" k3s_ready="$3" k3s_total="$4"
    local hosts_ok="$5" hosts_total="$6" coverage_pct="$7" coverage_floor="$8"
    local flux_not_ready="$9" zfs_degraded="${10}"
    local sections_ok="${11}" sections_total="${12}" alerts_firing="${13}"
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
    [ "$alerts_firing" -eq 0 ] || _rfp_add "alerts_firing=${alerts_firing}"
    unset -f _rfp_add
    echo "$out"
}

# classify_json <pve_up> <pve_total> <k3s_api_ok> <k3s_ready> <k3s_total>
#   <flux_not_ready> <zfs_degraded> <alerts_firing>

# The --json verdict: healthy means full Proxmox coverage, no Flux or ZFS
# imperfection and nothing firing; degraded means any imperfection with core
# infra up; catastrophic is neither. Unknown arrives as 1 and counts as firing.
classify_json() {
    local pve_up="$1" pve_total="$2" k3s_api_ok="$3" k3s_ready="$4" k3s_total="$5"
    local flux_not_ready="$6" zfs_degraded="$7" alerts_firing="${8:-0}"
    if [ "$pve_up" -gt 0 ] && [ "$pve_up" -eq "$pve_total" ] \
       && [ "$k3s_total" -gt 0 ] && [ "$k3s_ready" -eq "$k3s_total" ] \
       && [ "$flux_not_ready" -eq 0 ] && [ "$zfs_degraded" -eq 0 ] \
       && [ "$alerts_firing" -eq 0 ]; then
        echo "healthy"
    elif { [ "$pve_up" -lt "$pve_total" ] || [ "$k3s_ready" -lt "$k3s_total" ] \
           || [ "$k3s_api_ok" != true ] || [ "$flux_not_ready" -gt 0 ] \
           || [ "$zfs_degraded" -gt 0 ] || [ "$alerts_firing" -gt 0 ]; } \
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
