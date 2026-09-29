#!/usr/bin/env bash
# =============================================================================
# healthcheck.sh - verify every cross-container dependency BEFORE debugging code.
#
# WHY: In a distributed setup most failures are "the network or a peer is down",
#      not bugs. This gives a one-command answer to "which piece is broken?".
#      Exit code is non-zero if any check fails, so it is CI/cron friendly.
# Run from the repo root:  ./scripts/healthcheck.sh
# =============================================================================
set -uo pipefail   # no -e on purpose: we want to run ALL checks and report each one.

ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE (copy .env.example first)"; exit 2
fi
# `set -a` auto-exports every variable assigned while sourcing the file.
set -a; source "$ENV_FILE"; set +a

FAILS=0
pass() { printf '  [ OK ] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAILS=$((FAILS+1)); }

# curl flags: -s silent, -S show errors, -f fail on HTTP >= 400, -m timeout (seconds).
# A timeout matters: without it a dead peer hangs the check forever.
CURL_TLS=()
[[ "${NEXTCLOUD_VERIFY_TLS:-1}" == "0" ]] && CURL_TLS=(-k)

echo "Ollama  ($OLLAMA_BASE_URL)"
# /api/tags lists installed models, so it doubles as a liveness probe.
if TAGS=$(curl -sSf -m 10 "$OLLAMA_BASE_URL/api/tags"); then
  pass "API reachable"
  # The configured model must already be pulled on the Ollama host.
  if grep -q "\"name\":\"${MODEL_NAME}\"" <<<"$TAGS"; then
    pass "model '$MODEL_NAME' is installed"
  else
    fail "model '$MODEL_NAME' not found; run 'ollama pull $MODEL_NAME' on the Ollama host"
  fi
else
  fail "cannot reach Ollama (check OLLAMA_HOST=0.0.0.0 and the firewall rule)"
fi

echo "Nextcloud ($NEXTCLOUD_URL)"
# /status.php is Nextcloud's unauthenticated health endpoint.
if curl -sSf -m 10 "${CURL_TLS[@]}" "$NEXTCLOUD_URL/status.php" | grep -q '"installed":true'; then
  pass "server up and installed"
else
  fail "status.php failed (URL wrong, TLS problem, or container down)"
fi
# PROPFIND on the user's WebDAV root proves the app password actually works.
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${CURL_TLS[@]}" \
  -u "$NEXTCLOUD_USER:$NEXTCLOUD_APP_PASSWORD" -X PROPFIND -H 'Depth: 0' \
  "$NEXTCLOUD_URL/remote.php/dav/files/$NEXTCLOUD_USER/")
# 207 = WebDAV "Multi-Status" success.
[[ "$CODE" == "207" ]] && pass "WebDAV auth OK" || fail "WebDAV returned HTTP $CODE (expected 207)"

# Open Notebook (RAG source) is OPTIONAL: it is often stopped to save RAM, so an
# outage is a warning, not a failure. Skipped entirely if the URL is unset.
if [[ -n "${OPEN_NOTEBOOK_URL:-}" ]]; then
  echo "Open Notebook (${OPEN_NOTEBOOK_URL})"
  if curl -sSf -m 10 "$OPEN_NOTEBOOK_URL/health" | grep -q healthy; then
    pass "API healthy"
  else
    printf '  [WARN] API not reachable (start container 103 when you need RAG)\n'
  fi
fi

echo
if [[ $FAILS -eq 0 ]]; then echo "All checks passed."; else echo "$FAILS check(s) failed."; fi
exit $((FAILS > 0))
