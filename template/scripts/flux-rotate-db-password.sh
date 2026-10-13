#!/usr/bin/env bash
# Rotate an in-cluster Postgres role password: 1Password first, then the role,
# then the ExternalSecret refresh and pod restarts. Usage: <authentik>.
set -euo pipefail

app=${1:-}
case "$app" in
    authentik)
        pod="-n authentik authentik-postgresql-0 -c postgresql"
        item="Authentik Secrets"
        # role:1Password-field pairs; the admin role is the pod's own superuser.
        roles="authentik:postgresql-password postgres:postgresql-admin-password"
        psql_auth='PGPASSWORD="$(cat "$POSTGRES_POSTGRES_PASSWORD_FILE")" psql -U postgres -d postgres'
        rotate_secret="authentik/authentik-secrets"
        ;;
    *)
        echo "usage: $0 <authentik>" >&2
        exit 2
        ;;
esac

declare -a edits=()
declare -a alters=()
for pair in $roles; do
    role=${pair%%:*}
    field=${pair#*:}
    new=$(openssl rand -base64 48 | tr -d '/+=' | cut -c1-40)
    edits+=("$field=$new")
    alters+=("ALTER USER $role PASSWORD '$new'")
done
# The primary field mirrors the first role's password: a Password item with an
# empty primary is refused by the op item validator.
first=${edits[0]#*=}
edits+=("password=$first")

# The item is edited through its JSON template on a pipe: an assignment on the
# command line would show the new passwords in the process list.
echo "rotate-db-password: $item -> 1Password"
{
    op item get "$item" --vault Homelab --format json
    printf '%s\n' "${edits[@]}" |
        jq -R -s 'split("\n") | map(select(length > 0) | capture("^(?<k>[^=]+)=(?<v>.*)$") | {(.k): .v}) | add'
} | jq -s '.[1] as $e | .[0] | .fields |= map(($e[.id] // $e[.label]) as $v | if $v == null then . else .value = $v end)' |
    op item edit "$item" --vault Homelab >/dev/null

sql=""
for a in "${alters[@]}"; do sql+="$a;"; done
echo "rotate-db-password: ALTER USER in the database"
# The SQL travels on stdin, as one transaction: an exec argument lands in the
# API audit log, and a role left half-rotated has no item history to restore.
# shellcheck disable=SC2086  # $pod is a deliberate word list of kubectl args
if ! kubectl exec -i $pod -- sh -c "$psql_auth -v ON_ERROR_STOP=1 --single-transaction" <<<"$sql" >/dev/null; then
    echo "ERROR: the database refused the new password; restore the previous" \
        "value of '$item' from its 1Password item history before retrying." >&2
    exit 1
fi

echo "rotate-db-password: refresh + restart ($rotate_secret)"
task flux:rotate-secret -- "$rotate_secret"
