@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 嬗变台自动刷 · 启动

echo ==========================================
echo    嬗变台自动刷 · 一条龙
echo ==========================================
echo.
echo  注意：如果你的游戏开了 Magpie 之类的全屏放大工具，
echo        框选时如果屏幕没变暗，改用 run.bat 里的 [S] 或：
echo        python mcb.py quick --shot
echo.

python -u mcb.py quick

echo.
echo ------------------------------------------
echo 结束（窗口停在这里，方便看报错）
pause
