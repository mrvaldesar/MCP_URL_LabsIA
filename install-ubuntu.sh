#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if ! python3 -c 'import sys; assert sys.version_info >= (3, 10); import ensurepip' 2>/dev/null; then
    if ! command -v sudo >/dev/null; then
        echo 'Instala python3 y python3-venv con apt y vuelve a ejecutar el instalador.' >&2
        exit 1
    fi
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv
fi

if ! python3 -c 'import sys; assert sys.version_info >= (3, 10); import ensurepip'; then
    echo 'Se requiere Python 3.10 o superior con soporte para venv.' >&2
    exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -c requirements.txt mcp pydantic starlette uvicorn httpx
.venv/bin/python -m tools.local_server configure
echo 'Instalacion lista. Para iniciar el servidor: .venv/bin/python -m tools.local_server start'
