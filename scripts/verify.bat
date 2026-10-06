@echo off
REM ===========================================================================
REM  Sluice -- end-to-end verification (Windows host, Redis in WSL2 Docker)
REM
REM    scripts\verify.bat            full run: clean, start topology, verify, tear down
REM    scripts\verify.bat --keep     leave the effect services running when it finishes
REM
REM  Running everything inside WSL instead? Use scripts/verify.sh.
REM
REM  Owns the whole lifecycle deliberately.  It kills anything already on ports
REM  8100-8103 before starting its own services, because a stale process running
REM  pre-edit code is the single most misleading failure mode here -- you fix a
REM  bug, rerun, and watch the old process fail in exactly the old way.
REM
REM  Expected result: ALL CHECKS PASSED, 0 failures.
REM  See docs\verification.md for what each step proves and what its output should be.
REM ===========================================================================

setlocal enabledelayedexpansion
REM This script lives in scripts\; everything below runs from the repo root. pushd, not
REM cd /d: it also works when the repo is on the WSL filesystem (\\wsl.localhost\...),
REM which cmd.exe cannot cd into, by mapping a temporary drive letter.
pushd "%~dp0.."
REM Works with or without `pip install -e .`: the package is imported from src\.
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

set "PY=.venv\Scripts\python.exe"
set "OUT=_verify_out"
set "WSL_DISTRO=Ubuntu-24.04"
set /a FAILURES=0
set /a STEP=0
set "STARTED_SERVICES=0"
set "KEEP=0"
if /i "%~1"=="--keep" set "KEEP=1"

echo.
echo ===========================================================================
echo   Sluice end-to-end verification
echo ===========================================================================

REM ------------------------------------------------------------------ preflight
if not exist "%PY%" (
    echo   FATAL: %PY% not found.
    echo   Create the venv first:
    echo       python -m venv .venv
    echo       .venv\Scripts\activate
    echo       pip install -e ".[dev]"
    popd
    exit /b 1
)

REM ---------------------------------------------------------- 1. clean remnants
set /a STEP+=1
echo.
echo [%STEP%] Removing remnants
REM Journals, caches and compiled bytecode. A stale .sluice/shared.db is what
REM makes a fresh run replay instead of execute, so this is not mere tidiness.
if exist ".sluice"   rd /s /q ".sluice"
if exist ".pytest_cache" rd /s /q ".pytest_cache"
if exist "%OUT%"         rd /s /q "%OUT%"
for /d /r src %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"
for /d /r tests %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"
mkdir "%OUT%" 2>nul
echo     removed .sluice, .pytest_cache, __pycache__, %OUT%

REM Anything already holding the service ports is stale by definition.
for %%p in (8100 8101 8102 8103) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:"LISTENING" ^| findstr /C:":%%p "') do (
        echo     killing stale listener on %%p ^(pid %%a^)
        taskkill /PID %%a /T /F >nul 2>&1
    )
)

REM ---------------------------------------------------------------- 2. redis up
set /a STEP+=1
echo.
echo [%STEP%] Redis
%PY% -c "import redis,sys;sys.exit(0 if redis.Redis(port=6379,socket_connect_timeout=3).ping() else 1)" 2>nul
if not errorlevel 1 (
    echo     already up on localhost:6379
) else (
    echo     not reachable; starting the container
    REM Ask WSL to translate this repo's path rather than hardcoding one -- the repo
    REM has already moved once, and a baked-in /mnt/c/... would silently rot.
    set "WSLCWD="
    for /f "delims=" %%w in ('wsl -d %WSL_DISTRO% -- wslpath -a "%CD%" 2^>nul') do set "WSLCWD=%%w"
    if not defined WSLCWD (
        echo     FAILED: could not reach WSL distro %WSL_DISTRO%
        set /a FAILURES+=1
        goto redisdone
    )
    wsl -d %WSL_DISTRO% -u root -- bash -lc "systemctl is-active docker >/dev/null 2>&1 || systemctl start docker; cd '!WSLCWD!' && docker compose up -d" >"%OUT%\redis_up.txt" 2>&1
    set /a TRIES=0
    :waitredis
    set /a TRIES+=1
    %PY% -c "import redis,sys;sys.exit(0 if redis.Redis(port=6379,socket_connect_timeout=3).ping() else 1)" 2>nul
    if not errorlevel 1 goto redisok
    if !TRIES! GEQ 30 (
        echo     FAILED: redis never came up. See %OUT%\redis_up.txt
        set /a FAILURES+=1
        goto redisdone
    )
    ping -n 2 127.0.0.1 >nul
    goto waitredis
    :redisok
    echo     up on localhost:6379
)
:redisdone

REM WSL terminates the whole distro once the last wsl.exe client disconnects, and
REM that SIGTERMs the Redis container with it -- observed dying 16s after start.
REM Hold one client open for the duration of the run and drop it at teardown.
%PY% -c "import redis,sys;sys.exit(0 if redis.Redis(port=6379,socket_connect_timeout=3).ping() else 1)" 2>nul
if not errorlevel 1 (
    powershell -NoProfile -Command "$p = Start-Process -FilePath 'wsl.exe' -ArgumentList '-d','%WSL_DISTRO%','-u','root','--','sleep','3600' -PassThru -WindowStyle Hidden; $p.Id | Out-File -Encoding ascii '%OUT%\wsl.pid'" >nul 2>&1
    echo     holding the WSL distro open so the container survives the run
)

REM --------------------------------------------------------------- 3. importable
set /a STEP+=1
echo.
echo [%STEP%] Package imports
%PY% -c "import sluice;print('    sluice',sluice.__version__)"
if errorlevel 1 (
    echo     FAILED: package does not import
    set /a FAILURES+=1
)

REM -------------------------------------------------------------------- 4. tests
set /a STEP+=1
echo.
echo [%STEP%] Test suite: unit + integration ^(own topology on free ports, Redis DB 15^)
REM One POSIX-only test (SIGTERM to the services supervisor) is skipped on Windows.
%PY% -m pytest -q -rs >"%OUT%\pytest.txt" 2>&1
if errorlevel 1 (
    echo     FAILED -- see %OUT%\pytest.txt
    set /a FAILURES+=1
) else (
    for /f "delims=" %%l in ('findstr /R /C:"passed" "%OUT%\pytest.txt"') do echo     %%l
)

REM ------------------------------------------------------------- 5. services up
set /a STEP+=1
echo.
echo [%STEP%] Effect services ^(ticket, channel, pager, ledger^)
powershell -NoProfile -Command "$p = Start-Process -FilePath '%PY%' -ArgumentList '-m','sluice','services' -PassThru -WindowStyle Minimized -RedirectStandardOutput '%OUT%\services.log' -RedirectStandardError '%OUT%\services.err'; $p.Id | Out-File -Encoding ascii '%OUT%\services.pid'"
set "STARTED_SERVICES=1"

set /a TRIES=0
:waitsvc
set /a TRIES+=1
%PY% -c "import httpx,sys;sys.exit(0 if all(httpx.get(f'http://127.0.0.1:{p}/health',timeout=2).status_code==200 for p in (8100,8101,8102,8103)) else 1)" 2>nul
if not errorlevel 1 goto svcok
if !TRIES! GEQ 40 (
    echo     FAILED: services never became healthy. See %OUT%\services.err
    set /a FAILURES+=1
    goto teardown
)
ping -n 2 127.0.0.1 >nul
goto waitsvc
:svcok
echo     all four healthy on 8100-8103

REM --------------------------------------------------------------------- 6. demo
set /a STEP+=1
echo.
echo [%STEP%] Three-pane demo ^(expect 1/1/0, 2/2/1, 1/1/1 and EEO PASS^)
%PY% -m sluice demo --db-dir .sluice >"%OUT%\demo.txt" 2>&1
if errorlevel 1 (
    echo     FAILED: the demo exited nonzero -- see %OUT%\demo.txt
    set /a FAILURES+=1
) else (
    %PY% -c "import io,sys;t=io.open(r'%OUT%\demo.txt',encoding='utf-8',errors='replace').read();need=['tickets 1   posts 1   pages 0','tickets 2   posts 2   pages 1','tickets 1   posts 1   pages 1','unexplained violations:    0'];miss=[n for n in need if n not in t];sys.exit(0) if not miss else (print('     missing:',miss),sys.exit(1))"
    if errorlevel 1 (
        echo     FAILED: scoreboard did not match -- see %OUT%\demo.txt
        set /a FAILURES+=1
    ) else (
        echo     pinned 1/1/0, naive 2/2/1, sluice 1/1/1, 0 unexplained violations
    )
)

REM -------------------------------------------------------------------- 7. smoke
set /a STEP+=1
echo.
echo [%STEP%] Topology smoke ^(expect 20 passed, 0 failed, 0 skipped^)
%PY% -c "import redis,sys;sys.exit(0 if redis.Redis(port=6379,socket_connect_timeout=3).ping() else 1)" 2>nul
if errorlevel 1 (
    echo     WARNING: Redis went away since step 2. The stream checks below will
    echo              fail for that reason and not for anything in the code.
)
%PY% -m sluice smoke >"%OUT%\smoke.txt" 2>&1
if errorlevel 1 (
    echo     FAILED -- see %OUT%\smoke.txt
    set /a FAILURES+=1
    findstr /C:"FAIL " "%OUT%\smoke.txt"
) else (
    for /f "delims=" %%l in ('findstr /C:"passed," "%OUT%\smoke.txt"') do echo    %%l
)
findstr /C:"skip " "%OUT%\smoke.txt" >nul
if not errorlevel 1 (
    echo     WARNING: something was SKIPPED -- redis is probably down, so the
    echo              stream was never actually exercised. See %OUT%\smoke.txt
    set /a FAILURES+=1
)

REM ------------------------------------------- 8. redis pipeline, fresh execution
set /a STEP+=1
echo.
echo [%STEP%] Redis -^> orchestrator -^> HTTP, fresh execution
call :resetall
REM Configure the fault this step needs rather than inherit whatever ran last: the
REM services keep their faults across /admin/reset.
%PY% -m sluice faults poison >"%OUT%\faults.txt" 2>&1
if errorlevel 1 (
    echo     FAILED: could not set the poison fault -- see %OUT%\faults.txt
    set /a FAILURES+=1
)
%PY% -m sluice producer --demo --count 1 >"%OUT%\producer1.txt" 2>&1
%PY% -m sluice orchestrator --owner orch-a --source redis --world http --once --narrate >"%OUT%\orch_fresh.txt" 2>&1
findstr /C:"[ingest] redis stream" "%OUT%\orch_fresh.txt" >nul
if errorlevel 1 (
    echo     FAILED: fell back to the in-process queue; redis was NOT exercised
    set /a FAILURES+=1
)
findstr /C:"barrier_released" "%OUT%\orch_fresh.txt" >nul
if errorlevel 1 (
    echo     FAILED: no barrier_released -- compensation never ran
    set /a FAILURES+=1
) else (
    echo     barrier blocked, both effects compensated, barrier released
)
findstr /C:"EEO PASS" "%OUT%\orch_fresh.txt" >nul
if errorlevel 1 (
    echo     FAILED: EEO did not pass -- see %OUT%\orch_fresh.txt
    set /a FAILURES+=1
) else (
    for /f "delims=" %%l in ('findstr /C:"a-1001:" "%OUT%\orch_fresh.txt"') do echo    %%l
)

REM ----------------------------------------------------------- 9. replay is a no-op
set /a STEP+=1
echo.
echo [%STEP%] Same alert again, journal intact ^(expect replay, not re-execution^)
%PY% -m sluice producer --demo --count 1 >"%OUT%\producer2.txt" 2>&1
%PY% -m sluice orchestrator --owner orch-a --source redis --world http --once --narrate >"%OUT%\orch_replay.txt" 2>&1
findstr /C:"step_replayed" "%OUT%\orch_replay.txt" >nul
if errorlevel 1 (
    echo     FAILED: expected step_replayed; the workflow re-executed instead
    set /a FAILURES+=1
) else (
    echo     all steps replayed from the journal, nothing re-executed
)

REM ------------------------------------------- 10. exactly-once under redelivery
set /a STEP+=1
echo.
echo [%STEP%] At-least-once redelivery must not duplicate effects
call :counts "%OUT%\counts_before.txt"
%PY% -m sluice producer --demo --count 1 --redeliver >"%OUT%\producer3.txt" 2>&1
%PY% -m sluice orchestrator --owner orch-a --source redis --world http --once >"%OUT%\orch_dup.txt" 2>&1
call :counts "%OUT%\counts_after.txt"
fc "%OUT%\counts_before.txt" "%OUT%\counts_after.txt" >nul
if errorlevel 1 (
    echo     FAILED: ledger counts moved after a duplicate delivery
    echo     before: & type "%OUT%\counts_before.txt"
    echo     after:  & type "%OUT%\counts_after.txt"
    set /a FAILURES+=1
) else (
    echo     ledger counts unchanged across the duplicate:
    for /f "delims=" %%l in ('type "%OUT%\counts_after.txt"') do echo        %%l
)

REM ------------------------------------------------------------------- teardown
:teardown
REM --keep means "leave it usable". Releasing the WSL client would let the distro go
REM down and take Redis with it, so hold it for as long as the services live.
if not "%KEEP%"=="1" (
    for /f "delims=" %%p in ('type "%OUT%\wsl.pid" 2^>nul') do taskkill /PID %%p /T /F >nul 2>&1
)
echo.
if "%STARTED_SERVICES%"=="1" (
    if "%KEEP%"=="1" (
        echo   Leaving the effect services and Redis running ^(--keep^).
        echo   Stop them with:  taskkill /PID ^<pid in %OUT%\services.pid^> /T /F
        echo   and             taskkill /PID ^<pid in %OUT%\wsl.pid^> /T /F
    ) else (
        for /f "delims=" %%p in ('type "%OUT%\services.pid" 2^>nul') do taskkill /PID %%p /T /F >nul 2>&1
        echo   Effect services stopped.
    )
)

echo.
echo ===========================================================================
if %FAILURES%==0 (
    echo   ALL CHECKS PASSED
    echo   Full output kept in %OUT%\ if you want to read it.
    echo ===========================================================================
    popd
    exit /b 0
) else (
    echo   %FAILURES% CHECK^(S^) FAILED -- see %OUT%\ for the full output
    echo ===========================================================================
    popd
    exit /b 1
)

REM ------------------------------------------------------------------ subroutines
:resetall
REM Clear the journal, the services' idempotency keys, the ledger and the stream.
REM All four, or the next run replays instead of executing and proves nothing.
if exist ".sluice" rd /s /q ".sluice"
%PY% -c "import httpx;from sluice.ingest import DEFAULT_STREAM;import redis;[httpx.post(u,timeout=5) for u in ['http://127.0.0.1:8100/reset','http://127.0.0.1:8101/admin/reset','http://127.0.0.1:8102/admin/reset','http://127.0.0.1:8103/admin/reset']];redis.Redis(port=6379).delete(DEFAULT_STREAM)" >nul 2>&1
exit /b 0

:counts
REM Net ledger counts for the demo workflow from the out-of-process oracle, sorted so fc
REM can compare them. Per workflow, not global: a late effect from an earlier check
REM must not be able to move these numbers.
%PY% -c "import httpx,json;from sluice.core.tools import DEMO_ALERT;from sluice.core.types import workflow_id_for as w;print(json.dumps(httpx.get('http://127.0.0.1:8100/counts',params={'net':'true','workflow_id':w(DEMO_ALERT.alert_id)},timeout=5).json(),sort_keys=True))" >%1 2>&1
exit /b 0
