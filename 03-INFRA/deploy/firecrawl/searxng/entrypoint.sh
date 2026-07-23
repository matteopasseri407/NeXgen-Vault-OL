#!/bin/sh
set -eu

python3 /opt/nexgen-searxng/render-settings.py /etc/searxng/settings.yml
exec /usr/local/searxng/entrypoint.sh "$@"
