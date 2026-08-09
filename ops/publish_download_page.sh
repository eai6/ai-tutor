#!/usr/bin/env bash
#
# Publish the desktop download page to the public downloads bucket.
#
#   ./ops/publish_download_page.sh [version] [stack]
#
# The installers themselves are uploaded by .github/workflows/desktop-build.yml
# on a `desktop-v*` tag. This script only refreshes the page around them, so it
# can be re-run any time the copy changes without rebuilding anything.
#
# Why a separate script rather than a workflow step: the page is the same for
# both platforms, and the build runs once PER platform in a matrix. Generating
# it in the matrix would have the two jobs race to overwrite each other.
#
# Plan: memory/desktop_sync_and_distribution_plan.md
set -euo pipefail

VERSION="${1:-latest}"
STACK="${2:-dev}"
PREFIX="aitutor-${STACK}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${PREFIX}-downloads-${ACCOUNT_ID}"
SRC="$(dirname "$0")/download_page.html"

[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

TMP=$(mktemp -t aitutor-download-page)
trap 'rm -f "$TMP"' EXIT

# Stamp the version/date. sed rather than a template engine: two substitutions
# do not justify a dependency in an ops script.
sed -e "s/__VERSION__/${VERSION}/g" \
    -e "s/__DATE__/$(date -u +%Y-%m-%d)/g" \
    "$SRC" > "$TMP"

echo "==> publishing download page (version ${VERSION}) to s3://${BUCKET}"
aws s3 cp "$TMP" "s3://${BUCKET}/public/desktop/latest/index.html" \
  --content-type 'text/html; charset=utf-8' \
  --cache-control 'no-cache, max-age=60'

REGION="${AWS_REGION:-us-east-1}"
echo
echo "Download page:"
echo "  https://${BUCKET}.s3.${REGION}.amazonaws.com/public/desktop/latest/index.html"
echo
echo "Verify it is ANONYMOUSLY readable (aws s3 ls proves nothing — it uses your"
echo "credentials). This must return 200 with no auth:"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' \\"
echo "    https://${BUCKET}.s3.${REGION}.amazonaws.com/public/desktop/latest/index.html"
