#!/usr/bin/env bash
# Adoption / disaster-recovery state bootstrap: `terraform import` for every
# address imports.tf declares, skipping what is already in state. Idempotent;
# run via `task terraform:authentik-import`. `--check` prints the table and exits.
set -euo pipefail
cd "$(dirname "$0")"

CHECK_ONLY=0
[ "${1:-}" != "--check" ] || CHECK_ONLY=1

# address|id pairs, derived from imports.tf so there is no second copy to drift.
# The extractor handles three block shapes: a plain `to`/`id` pair, a for_each
# over local.imported_application_slugs, an inline for_each map. A fourth aborts.
IMPORTS="$(awk '
function fail(msg) { print "extractor: " msg > "/dev/stderr"; aborted = 1; exit 2 }
function strip(s) { gsub(/^[ \t]*[a-z_]+[ \t]*=[ \t]*/, "", s); gsub(/[ \t]+$/, "", s); return s }
function dequote(s) { gsub(/^"|"$/, "", s); return s }
function process(   i, line, fe, to, id, addr, j, k, v) {
  fe = ""; to = ""; id = ""
  split("", pairk); split("", pairv); npairs = 0
  for (i = 1; i <= nlines; i++) {
    line = lines[i]
    if (line ~ /^[ \t]*(#|\/\/)/ || line ~ /^[ \t]*$/) continue
    if (line ~ /^[ \t]*for_each[ \t]*=/) { fe = strip(line); continue }
    if (line ~ /^[ \t]*to[ \t]*=/)       { to = strip(line); continue }
    if (line ~ /^[ \t]*id[ \t]*=/)       { id = dequote(strip(line)); continue }
    if (line ~ /^[ \t]*"[^"]+"[ \t]*=[ \t]*"[^"]+"[ \t]*$/) {
      j = index(line, "="); npairs++
      k = substr(line, 1, j - 1); v = substr(line, j + 1)
      gsub(/^[ \t]*"|"[ \t]*$/, "", k); gsub(/^[ \t]*"|"[ \t]*$/, "", v)
      pairk[npairs] = k; pairv[npairs] = v
      continue
    }
    if (line ~ /^[ \t]*\}[ \t]*$/) continue
    fail("unrecognised line in an import block: " line)
  }
  if (to == "") fail("import block with no `to`")
  if (fe == "") {
    if (id == "") fail("import block with no `id`: " to)
    print to "|" id
  } else if (fe == "local.imported_application_slugs") {
    if (to !~ /\[each\.value\]$/ || id != "each.value") fail("unexpected slug for_each: " to)
    for (j = 1; j <= nslugs; j++) {
      addr = to; sub(/\[each\.value\]$/, "[\"" slugs[j] "\"]", addr)
      print addr "|" slugs[j]
    }
  } else if (fe == "{") {
    if (to !~ /\[each\.key\]$/ || id != "each.value") fail("unexpected map for_each: " to)
    if (npairs == 0) fail("inline for_each map with no entries: " to)
    for (j = 1; j <= npairs; j++) {
      addr = to; sub(/\[each\.key\]$/, "[\"" pairk[j] "\"]", addr)
      print addr "|" pairv[j]
    }
  } else {
    fail("unrecognised for_each source: " fe)
  }
}
/imported_application_slugs = toset\(\[/ { in_slugs = 1; next }
in_slugs && /^[ \t]*\]\)/               { in_slugs = 0; next }
in_slugs {
  if (match($0, /"[^"]+"/)) slugs[++nslugs] = substr($0, RSTART + 1, RLENGTH - 2)
  next
}
/^import[ \t]*\{/ { inblock = 1; depth = 1; nlines = 0; next }
inblock {
  saved = $0
  depth += gsub(/\{/, "{")
  depth -= gsub(/\}/, "}")
  if (depth <= 0) { process(); inblock = 0; next }
  lines[++nlines] = saved
}
END { if (!aborted && inblock) fail("unterminated import block") }
' imports.tf | sort)"

# Under-reporting would produce a partial import and a DR plan full of creates
# against live objects. Every block yields at least one pair, so fewer pairs than
# blocks means a shape went unparsed; a fresh cluster holds the floor at zero.
BLOCKS="$(grep -c '^import[[:space:]]*{' imports.tf || true)"
PAIRS="$(printf '%s\n' "${IMPORTS}" | grep -c '^module\.sso\.' || true)"
if [ "${PAIRS}" -lt "${BLOCKS}" ]; then
  echo "ERROR: extractor produced ${PAIRS} imports for ${BLOCKS} import blocks in imports.tf — its block shape changed; fix the awk parser in this file." >&2
  exit 2
fi

if [ "${CHECK_ONLY}" = 1 ]; then
  [ -z "${IMPORTS}" ] || printf '%s\n' "${IMPORTS}"
  exit 0
fi

STATE="$(terraform state list 2>/dev/null || true)"
imported=0
skipped=0
while IFS='|' read -r addr id; do
  [ -n "${addr}" ] || continue
  if printf '%s\n' "${STATE}" | grep -Fxq "${addr}"; then
    skipped=$((skipped + 1))
    continue
  fi
  echo "==> terraform import '${addr}' '${id}'"
  terraform import -input=false "${addr}" "${id}" < /dev/null
  imported=$((imported + 1))
done <<EOF
${IMPORTS}
EOF

echo "import.sh done: ${imported} imported, ${skipped} already in state."
