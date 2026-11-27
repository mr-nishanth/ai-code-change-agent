# shellcheck shell=sh
# Shared environment for the Floci bootstrap scripts. Source it; do not run it.
#
# Safety: every AWS call made by these scripts goes to the local Floci emulator
# on the loopback interface with the dummy credentials test/test. Any other
# endpoint aborts before a single AWS call is made. The developer's AWS profile,
# config and credentials files are never read.

AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://127.0.0.1:4566}"
case "$AWS_ENDPOINT_URL" in
  http://127.0.0.1:4566 | http://127.0.0.1:4566/ | http://localhost:4566 | http://localhost:4566/) ;;
  *)
    echo "floci-init: refusing non-loopback endpoint '$AWS_ENDPOINT_URL' (only http://127.0.0.1:4566 or http://localhost:4566)" >&2
    exit 2
    ;;
esac
AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL%/}"
export AWS_ENDPOINT_URL

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
if [ -z "${FLOCI_INIT_HEALTHY:-}" ]; then
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
  FLOCI_INIT_HEALTHY=1
  export FLOCI_INIT_HEALTHY
fi
