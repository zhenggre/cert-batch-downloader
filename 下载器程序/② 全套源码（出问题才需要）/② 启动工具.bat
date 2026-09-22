@echo off
chcp 936 >nul
title 证书下载器
cd /d "%~dp0"

rem 优先用镜像自带的 exe（同事电脑什么都不用装）
if exist "证书下载器.exe" (
    "证书下载器.exe"
    goto :eof
)
if exist "%~dp0发给同事\证书下载器\证书下载器.exe" (
    "%~dp0发给同事\证书下载器\证书下载器.exe"
    goto :eof
)

rem 其次用本目录的便携版运行时
if exist "%~dp0程序\runtime\python.exe" (
    "%~dp0程序\runtime\python.exe" "%~dp0程序\web_app.py"
    goto :eof
)

rem 最后用系统的 Python
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0程序\web_app.py"
    goto :eof
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0程序\web_app.py"
    goto :eof
)

echo.
echo   没找到可用的运行环境。
echo   请先双击「① 一键安装环境.bat」完成安装。
echo.
pause
