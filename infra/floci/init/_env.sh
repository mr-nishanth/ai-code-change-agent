# shellcheck shell=sh
# Shared environment for the Floci bootstrap scripts. Source it; do not run it.
#
# Safety: every AWS call made by these scripts goes to the local Floci emulator
# on the loopback interface with the dummy credentials test/test. Any other
# endpoint aborts before a single AWS call is made. The developer's AWS profile,
# config and credentials files are never read.
#
# Input: ACCA_FLOCI_ENDPOINT_URL (default http://127.0.0.1:4566). Only plain http
# on 127.0.0.1, localhost or [::1] with a numeric port is accepted, so a second
# Floci on another loopback port (for example 4577) can be targeted. Any
# AWS_ENDPOINT_URL from the caller is ignored and replaced; it is exported only
# for the AWS CLI inside these scripts.

_acca_endpoint="${ACCA_FLOCI_ENDPOINT_URL:-http://127.0.0.1:4566}"
_acca_endpoint="${_acca_endpoint%/}"
case "$_acca_endpoint" in
  http://127.0.0.1:*) _acca_port="${_acca_endpoint#http://127.0.0.1:}" ;;
  http://localhost:*) _acca_port="${_acca_endpoint#http://localhost:}" ;;
  "http://[::1]:"*) _acca_port="${_acca_endpoint#http://\[::1\]:}" ;;
  *) _acca_port= ;;
esac
case "$_acca_port" in
  "" | *[!0-9]* | ??????*)
    echo "floci-init: refusing endpoint '$_acca_endpoint' (only http://127.0.0.1:<port>, http://localhost:<port> or http://[::1]:<port>)" >&2
    exit 2
    ;;
esac
AWS_ENDPOINT_URL="$_acca_endpoint"
export AWS_ENDPOINT_URL
unset _acca_endpoint _acca_port

# Dummy credentials only; never a real profile, session or role.
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_SESSION_TOKEN AWS_SECURITY_TOKEN \
  AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_SESSION_NAME
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
export AWS_REGION=us-east-1
export AWS_CONFIG_FILE=/dev/null
export AWS_SHARED_CREDENTIALS_FILE=/dev/null
export AWS_PAGER=

awsl() {
  aws --endpoint-url "$AWS_ENDPOINT_URL" "$@"
}

log() {
  echo "floci-init: $*"
}

# Wait up to 60 s for Floci to report healthy.
if [ -z "${ACCA_FLOCI_HEALTHY:-}" ]; then
  _floci_waited=0
  until curl -fsS -m 2 -o /dev/null "$AWS_ENDPOINT_URL/_floci/health"; do
    if [ "$_floci_waited" -ge 60 ]; then
      echo "floci-init: Floci is not healthy at $AWS_ENDPOINT_URL after 60 s" >&2
      exit 3
    fi
    sleep 2
    _floci_waited=$((_floci_waited + 2))
  done
  unset _floci_waited
  ACCA_FLOCI_HEALTHY=1
  export ACCA_FLOCI_HEALTHY
fi
