#!/usr/bin/env bash
# Example deploy script for a target of TYPE=script.
#
#   TARGET_MYAPP_TYPE=script
#   TARGET_MYAPP_PATH=example-deploy.sh      (relative to the "scripts" folder)
#   TARGET_MYAPP_HOST=192.0.2.60             (available here as $OPT_HOST)
#
# Provided by certsync:
#   CERT_NAME  CERT_DOMAINS  CERT_NOT_AFTER  TARGET_NAME
#   CERT_FULLCHAIN CERT_PRIVKEY CERT_CERT CERT_CHAIN   (file paths)
#   OPT_<KEY>  every other TARGET_<NAME>_<KEY> of this target
#
# Exit 0 = success. Any other exit code marks the deploy as failed and it is
# retried in the next cycle. Tools available: bash, curl, ssh, scp, python3.
set -euo pipefail

echo "Deploying ${CERT_NAME} (${CERT_DOMAINS}), valid until ${CERT_NOT_AFTER}"
echo "fullchain: ${CERT_FULLCHAIN}"
echo "host option: ${OPT_HOST:-<not set>}"

# Example: push via curl to some API
# curl -fsS -X POST "https://${OPT_HOST}/api/cert" \
#   -F "cert=@${CERT_FULLCHAIN}" -F "key=@${CERT_PRIVKEY}"
