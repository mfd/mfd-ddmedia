#!/usr/bin/env bash
set -euo pipefail

# Run on your Mac from any directory: bash /path/to/bot/publish.sh
project_dir="$(cd "$(dirname "$0")" && pwd)"
python_bin="${PUBLISH_PYTHON:-python3}"
version="$(sed -n 's/^SCRIPT_VERSION = "\([^"]*\)"/\1/p' "$project_dir/media_bot/app.py")"
if [[ ! "$version" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]{2}\.[0-9]{2}$ ]]; then
  echo "Cannot read a valid SCRIPT_VERSION" >&2
  exit 1
fi

remote="aiagent@88.218.93.204"
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT
archive="$temporary_dir/media-bot-release-$version.tar.gz"

PYTHONPYCACHEPREFIX="$temporary_dir/pycache" "$python_bin" -m compileall -q "$project_dir"
(cd "$project_dir" && BOT_TOKEN='123:TEST' OWNER_ID=1 DATA_DIR="$temporary_dir/test-data" \
  PYTHONPYCACHEPREFIX="$temporary_dir/pycache" "$python_bin" -m unittest discover -s tests -q)
tar -czf "$archive" -C "$project_dir" \
  main.py notify.py yandex_auth.py deploy.sh deploy_remote.sh publish.sh CHANGELOG.txt \
  README.md INSTALLATION.md DEPLOYMENT.md TRANSCRIPTION_PRD.md \
  config.example.env requirements.txt media_bot tests

scp -O "$archive" "$project_dir/deploy_remote.sh" "$remote:/home/aiagent/"
ssh -tt "$remote" "bash /home/aiagent/deploy_remote.sh '$version'"
