#!/bin/zsh
set -e
cd "${0:A:h}"
exec /usr/bin/python3 app.py "$@"
