@echo off
rem Borra la instalacion de prueba (%LOCALAPPDATA%\YouTubeMp3) y los accesos directos. Ojo: borra tambien la musica que haya en esa carpeta.
echo Se va a borrar %LOCALAPPDATA%\YouTubeMp3 (incluida la musica descargada ahi).
choice /m "Continuar"
if errorlevel 2 exit /b 0
taskkill /f /im pythonw.exe >nul 2>nul
rmdir /s /q "%LOCALAPPDATA%\YouTubeMp3"
del "%USERPROFILE%\Desktop\YouTube MP3.lnk" 2>nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\YouTube MP3.lnk" 2>nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\YouTubeMp3" /f >nul 2>nul
echo Listo.
pause
