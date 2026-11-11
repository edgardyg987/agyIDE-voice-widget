@echo off
title Installer / Instalador - agyIDE-voice-widget
cd /d "%~dp0"

echo ====================================================================
echo      SETUP ASSISTANT / INSTALADOR ASISTIDO - agyIDE-voice-widget
echo ====================================================================
echo.

:: 1. Verify Python installation / Verificar instalacion de Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found / No se encontro Python en tu sistema.
    echo Please install Python 3.10+ and check 'Add python.exe to PATH'.
    echo Por favor instala Python 3.10 o superior y marca 'Add python.exe to PATH'.
    echo.
    pause
    exit /b 1
)

:: 2. Install dependencies / Instalar dependencias
echo [*] Installing dependencies / Instalando dependencias (sounddevice, pywin32, keyboard)...
python -m pip install -r requirements.txt --quiet
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [NOTICE] Retrying verbose installation / Reintentando instalacion detallada...
    python -m pip install -r requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo [ERROR] Failed to install dependencies / No se pudieron instalar las dependencias.
        pause
        exit /b 1
    )
)
echo [OK] Dependencies installed successfully / Dependencias verificadas correctamente.
echo.

:: 3. Generate silent runner dynamically inside src/
set "SRC_DIR=%~dp0src"
set "RUNNER_VBS=%SRC_DIR%\runner.vbs"
set "ICON_FILE=%SRC_DIR%\app_icon.ico"

(
    echo Set WshShell = CreateObject("WScript.Shell"^)
    echo srcDir = CreateObject("Scripting.FileSystemObject"^).GetParentFolderName(WScript.ScriptFullName^)
    echo WshShell.CurrentDirectory = srcDir
    echo WshShell.Run "pythonw """ ^& srcDir ^& "\agyIDE_voice_widget.py""", 0, False
) > "%RUNNER_VBS%"

:: 4. Daily startup configuration menu / Menu de configuracion de inicio
echo ====================================================================
echo  LAUNCH CONFIGURATION / CONFIGURACION DE USO DIARIO
echo ====================================================================
echo  Choose how to launch the widget / Elige como prefieres iniciarlo:
echo.
echo  [1] Project folder / Acceso directo en esta carpeta
echo      Creates a shortcut in this project folder for manual launch.
echo      Crea un acceso con icono en esta misma carpeta para iniciarlo a mano.
echo.
echo  [2] Desktop shortcut / Acceso directo en el Escritorio
echo      Creates an official Aurora shortcut on your Desktop for 1-click launch.
echo      Crea un acceso directo en tu Escritorio con su icono para iniciarlo con 1 clic.
echo.
echo  [3] (Recommended) Automatic / Iniciar con Windows (Startup)
echo      Starts automatically with Windows. Hides when Antigravity is inactive.
echo      Se inicia solo con Windows. Se oculta si Antigravity no esta activo.
echo      (Toggleable anytime via right-click / Conmutable con clic derecho).
echo ====================================================================
set "OPCION=3"
set /p "OPCION=Select an option / Selecciona opcion (1, 2, 3) [Default: 3]: "

if "%OPCION%"=="1" (
    echo.
    echo [*] Creating project folder shortcut / Creando acceso en carpeta...
    python -c "import os, win32com.client; ws = win32com.client.Dispatch('WScript.Shell'); s = ws.CreateShortcut(os.path.join(r'%~dp0', 'Start agyIDE Voice.lnk')); s.TargetPath = r'%RUNNER_VBS%'; s.WorkingDirectory = r'%SRC_DIR%'; s.IconLocation = r'%ICON_FILE%,0'; s.Save()"
    echo [OK] Shortcut created in project folder / Acceso creado en esta carpeta.
)

if "%OPCION%"=="2" (
    echo.
    echo [*] Creating Desktop shortcut / Creando acceso directo en el Escritorio...
    python -c "import os, win32com.client; ws = win32com.client.Dispatch('WScript.Shell'); s = ws.CreateShortcut(os.path.join(ws.SpecialFolders('Desktop'), 'agyIDE Voice Widget.lnk')); s.TargetPath = r'%RUNNER_VBS%'; s.WorkingDirectory = r'%SRC_DIR%'; s.IconLocation = r'%ICON_FILE%,0'; s.Save()"
    echo [OK] 'agyIDE Voice Widget' Desktop shortcut created successfully.
)

if "%OPCION%"=="3" (
    echo.
    echo [*] Setting up automatic startup / Configurando inicio con Windows...
    python -c "import os, win32com.client; ws = win32com.client.Dispatch('WScript.Shell'); s = ws.CreateShortcut(os.path.join(ws.SpecialFolders('Startup'), 'agyIDE Voice Widget.lnk')); s.TargetPath = r'%RUNNER_VBS%'; s.WorkingDirectory = r'%SRC_DIR%'; s.IconLocation = r'%ICON_FILE%,0'; s.Save()"
    echo [OK] Automatic startup configured successfully / Inicio automatico configurado.
)

echo.
echo ====================================================================
echo  SETUP COMPLETED / INSTALACION FINALIZADA CON EXITO
echo ====================================================================
set "LANZAR=Y"
set /p "LANZAR=Launch widget right now? / Deseas iniciar ahora? (Y/N / S/N) [Default: Y]: "

if /i "%LANZAR%"=="Y" set "LANZAR=S"
if /i "%LANZAR%"=="S" (
    echo [*] Starting agyIDE Voice Widget in background...
    start "" wscript "%RUNNER_VBS%"
)

echo.
echo Ready! You can close this window / Listo, ya puedes cerrar esta ventana.
timeout /t 3 >nul
exit /b 0
