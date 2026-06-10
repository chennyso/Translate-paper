$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (!(Test-Path ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
}

if (!(Test-Path ".\.venv\Scripts\uv.exe")) {
    .\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple uv
}

.\.venv\Scripts\uv.exe pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
.\.venv\Scripts\python.exe run.py
