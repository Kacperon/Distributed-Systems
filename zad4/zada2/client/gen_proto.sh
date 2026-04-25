#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p generated
PY="${PYTHON:-python3}"
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
fi
"$PY" -m grpc_tools.protoc \
    -I proto \
    --python_out=generated \
    --grpc_python_out=generated \
    proto/can.proto
echo "generated/ ready (using $PY)"
