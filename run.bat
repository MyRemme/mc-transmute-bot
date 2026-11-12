@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 嬗变台自动刷

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [X] 找不到 python 命令。
  echo     请安装 Python 3.10+ 并在安装时勾选 "Add python.exe to PATH"
  echo.
  pause
  exit /b
)

:menu
cls
echo ============================================
echo    Minecraft 嬗变台 自动刷
echo ============================================
echo.
echo   [1] 一条龙：框槽位 -^> 给图 -^> 录循环 -^> 开刷
echo   [2] 框出掉落槽区域
echo   [3] 加入一张目标物品图片
echo   [4] 从截图里裁目标图标
echo   [5] 重新录一遍循环
echo   [6] 实时看匹配分数（调阈值）
echo   [7] 直接开刷
echo   [8] 框 GUI 指纹（防点穿）
echo   [9] 环境体检（出问题先跑这个）
echo   [S] 框选看不见？用截图方式框（Magpie/全屏遮挡时用）
echo   [T] 自检 + 离线演练
echo   [0] 退出
echo.
set /p c=选择：

if "%c%"=="1" ( python -u mcb.py quick & pause & goto menu )
if "%c%"=="2" ( python -u mcb.py region & pause & goto menu )
if "%c%"=="3" ( set /p f=图片路径（可直接把文件拖进来）： & python -u mcb.py add "%f%" & pause & goto menu )
if "%c%"=="4" ( python -u mcb.py shot & python -u mcb.py crop debug_watch.png & pause & goto menu )
if "%c%"=="5" ( python -u mcb.py record --name loop & pause & goto menu )
if "%c%"=="6" ( python -u mcb.py watch & pause & goto menu )
if "%c%"=="7" ( python -u mcb.py run & pause & goto menu )
if "%c%"=="8" ( python -u mcb.py guard & pause & goto menu )
if "%c%"=="9" ( python -u mcb.py doctor & pause & goto menu )
if /i "%c%"=="S" ( python -u mcb.py region --shot & pause & goto menu )
if /i "%c%"=="T" ( python -u mcb.py selftest & python -u test_offline.py & pause & goto menu )
if "%c%"=="0" exit /b
goto menu
