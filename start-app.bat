@echo off
setlocal enabledelayedexpansion

REM LlamaNet Windows Startup Script
REM Handles deployment on MACHAAO platform and local development

REM ── MACHAAO Cloud Detection ──
if defined MACHAAO_APP_ID (
    set LLAMANET_MODE=landing
    echo MACHAAO cloud detected - starting gateway mode
)

REM Detect Python interpreter
set PYTHON_CMD=
where python >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python
    goto :python_found
)
where python3 >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python3
    goto :python_found
)
where py >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=py
    goto :python_found
)
echo ERROR: Python not found. Please install Python 3.8+ and add it to PATH.
echo Download from: https://www.python.org/downloads/
exit /b 1

:python_found
echo Using Python: %PYTHON_CMD%

REM ── Handle --help ──
if "%~1"=="--help" goto :show_help
if "%~1"=="-h" goto :show_help
if "%~1"=="help" goto :show_help
goto :skip_help

:show_help
echo.
echo   LlamaNet — Distributed AI Inference Network
echo   ────────────────────────────────────────────
echo.
echo   Usage:
echo     llamanet                                  Start (no-model mode)
echo     llamanet run ^<hf-url^> [OPTIONS]           Download and run a model
echo.
echo   Options:
echo     --bootstrap-peers URL Gateway URL (default: https://llamanet.app)
echo     --port PORT           HTTP API port (default: 8000)
echo     --host HOST           Bind address (default: 0.0.0.0)
echo     --ctx-size N          Context window in tokens (0 = auto-detect)
echo     --batch-size N        Batch size in tokens (default: 4096)
echo     --ubatch-size N       Physical micro-batch size in tokens (default: 512)
echo     --n-parallel N        Number of parallel slots (default: 1)
echo     --threads N           CPU threads for generation (0 = auto)
echo     --threads-batch N     CPU threads for prefill processing (0 = auto)
echo     --flash-attn          Enable FlashAttention
echo     --cache-type-k TYPE   KV cache key type: f16, q8_0, q4_0 (default: f16)
echo     --cache-type-v TYPE   KV cache value type: f16, q8_0, q4_0 (default: f16)
echo     --gpu-layers N        GPU layers (-1 = all)
echo     --no-gpu              Disable GPU acceleration
echo     --node-id ID          Custom node identifier
echo     --public-ip IP        Override public IP detection
echo     --verbose             Enable verbose logging
echo     --help                Show this help
echo.
echo   Examples:
echo     llamanet
echo     start-app.bat run hf.co/user/Model:Q4_K_M
echo     start-app.bat run hf.co/user/Model:Q4_K_M --ctx-size 16384
echo     start-app.bat run hf.co/user/Model:Q4_K_M --no-gpu --cache-type-k q8_0
echo     start-app.bat --no-gpu --port 8080
echo.
echo   Web UI opens automatically at http://localhost:8000
echo.
exit /b 0

:skip_help

REM ── Landing/Gateway Mode ──
if "%LLAMANET_MODE%"=="landing" (
    echo Starting llamanet.app gateway...

    %PYTHON_CMD% -c "import supabase" >nul 2>&1
    if %errorlevel% neq 0 (
        echo Installing Supabase client...
        %PYTHON_CMD% -m pip install supabase python-jose[cryptography]
    )

    %PYTHON_CMD% -c "import landing" >nul 2>&1
    if %errorlevel% neq 0 (
        %PYTHON_CMD% -m pip install -e .
    )

    %PYTHON_CMD% -m landing.server
    goto :eof
)

echo Starting LlamaNet OpenAI-Compatible Inference Node...

REM ── Parse Arguments ──
set ENABLE_TUNNEL=false
set REMAINING_ARGS=
set BOOTSTRAP_PEERS_VALUE=
set RUN_MODE=false
set HF_URL=

if "%~1"=="run" (
    set RUN_MODE=true
    set HF_URL=%~2
    shift
    shift
)

:parse_args
if "%~1"=="" goto :done_args
if "%~1"=="--tunnel" (
    set ENABLE_TUNNEL=true
    shift
    goto :parse_args
)
if "%~1"=="--bootstrap-peers" (
    set BOOTSTRAP_PEERS_VALUE=%~2
    shift
    shift
    goto :parse_args
)
set REMAINING_ARGS=%REMAINING_ARGS% %~1
shift
goto :parse_args

:done_args

if defined BOOTSTRAP_PEERS_VALUE (
    set BOOTSTRAP_PEERS=%BOOTSTRAP_PEERS_VALUE%
)

REM ── Default Configuration ──
if not defined MODEL_PATH set MODEL_PATH=
if not defined HOST set HOST=0.0.0.0
if not defined PORT set PORT=8000

REM ── Install Dependencies ──
%PYTHON_CMD% -c "import fastapi, uvicorn" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing dependencies...
    if exist requirements.txt (
        %PYTHON_CMD% -m pip install -r requirements.txt
    ) else (
        echo ERROR: requirements.txt not found
        exit /b 1
    )
)

if "%RUN_MODE%"=="true" (
    %PYTHON_CMD% -c "import llama_cpp" >nul 2>&1
    if %errorlevel% neq 0 (
        echo Installing inference dependencies...
        if exist requirements-inference.txt (
            %PYTHON_CMD% -m pip install -r requirements-inference.txt
        ) else (
            %PYTHON_CMD% -m pip install -r requirements.txt
        )
    )
)

REM Install package in development mode
%PYTHON_CMD% -c "import inference_node" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing LlamaNet package...
    %PYTHON_CMD% -m pip install -e .
)

REM ── Build Command Line ──
set ARGS=

if "%RUN_MODE%"=="true" (
    set ARGS=%HF_URL%
    if defined REMAINING_ARGS set ARGS=!ARGS! %REMAINING_ARGS%
    echo.
    echo Running model from Hugging Face: %HF_URL%
    echo.
    %PYTHON_CMD% -m inference_node.server run !ARGS!
) else (
    if defined MODEL_PATH (
        set ARGS=--model-path %MODEL_PATH%
    )
    set ARGS=!ARGS! --host %HOST% --port %PORT%
    if defined NODE_ID set ARGS=!ARGS! --node-id %NODE_ID%
    if defined PUBLIC_IP set ARGS=!ARGS! --public-ip %PUBLIC_IP%
    if defined BOOTSTRAP_PEERS set ARGS=!ARGS! --bootstrap-peers %BOOTSTRAP_PEERS%
    if defined REMAINING_ARGS set ARGS=!ARGS! %REMAINING_ARGS%

    echo.
    echo Configuration:
    if defined MODEL_PATH (
        echo   Model: %MODEL_PATH%
    ) else (
        echo   Model: none - download via Web UI
    )
    echo   Host: %HOST%
    echo   Port: %PORT%
    echo   Bootstrap Peers: %BOOTSTRAP_PEERS%
    echo.

    echo Starting inference node...
    echo API will be available at: http://%HOST%:%PORT%
    echo Web UI will be available at: http://%HOST%:%PORT%
    echo.
    echo OpenAI-compatible endpoints:
    echo   GET  /v1/models
    echo   POST /v1/completions
    echo   POST /v1/chat/completions
    echo.

    %PYTHON_CMD% -m inference_node.server !ARGS!
)

:end
endlocal
