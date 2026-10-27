@echo off
title Instalar YouTube MP3
rem Instala YouTube MP3: baja Python 3.14 portable, ffmpeg, el programa y las librerias. Se instala en %LOCALAPPDATA%\YouTubeMp3
cd /d "%~dp0_archivos"
set PYX=
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
  if not defined PYX (
    py -%%V -c "import tkinter" >nul 2>nul && set PYX=py -%%V
  )
)
if not defined PYX (
  python -c "import tkinter" >nul 2>nul && set PYX=python
)
if not defined PYX (
  echo.
  echo No se encontro Python en esta PC para abrir el instalador.
  echo Instala Python desde https://www.python.org/downloads/ y marca "tcl/tk and IDLE".
  echo O usa el archivo YouTubeMp3-Setup.exe, que no necesita nada instalado.
  echo.
  pause
  exit /b 1
)
%PYX% YouTubeMp3Instalador.py %*
if errorlevel 1 pause
