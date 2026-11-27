@echo off
rem AI 签到（Trae / WorkBuddy）- 打包成单文件 exe
setlocal
cd /d "%~dp0"

python -m PyInstaller --onefile --noconsole --name ai-checkin --noconfirm ^
    --hidden-import pystray._win32 ^
    main.py
if errorlevel 1 (
    echo 打包失败
    exit /b 1
)

echo.
echo 打包完成: dist\ai-checkin.exe
echo   dist\ai-checkin.exe            打开窗口 / 托盘常驻
echo   dist\ai-checkin.exe --silent   只驻留托盘
echo   dist\ai-checkin.exe --status   查看今日状态与记录
echo   dist\ai-checkin.exe --install  注册开机自启
endlocal
