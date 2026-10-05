#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/doc2md.py" "$@"
