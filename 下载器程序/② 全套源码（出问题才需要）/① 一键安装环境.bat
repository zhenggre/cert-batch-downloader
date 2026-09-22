@echo off
chcp 936 >nul
title 证书下载器 - 安装环境
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo.
echo  ==============================================================
echo    比赛获奖证书批量下载器 - 一键安装环境
echo  ==============================================================
echo.
echo  本脚本会做好一切准备工作，全程自动，请勿关闭这个窗口。
echo  预计需要 2~5 分钟，取决于网速。
echo.

set "PYEXE="
set "PYVER="

rem ================= 步骤 1/4：找一个能用的 Python =================
echo  [1/4] 正在检查电脑上有没有 Python ...

if exist "%~dp0程序\runtime\python.exe" (
    set "PYEXE=%~dp0程序\runtime\python.exe"
    echo        发现自带运行时：程序\runtime\python.exe
    goto :check_py
)

rem 先试官方的 py 启动器（多版本共存时更靠谱）
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)"') do set "PYEXE=%%i"
    if defined PYEXE (
        echo        发现系统 Python^（py 启动器^）
        goto :check_py
    )
)

rem 再试 PATH 里的 python，但要排除微软商店那个假的存根
where python >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYEXE (
            echo %%i | findstr /i "WindowsApps" >nul
            if errorlevel 1 set "PYEXE=%%i"
        )
    )
    if defined PYEXE (
        echo        发现系统 Python：!PYEXE!
        goto :check_py
    )
)

echo        没有找到 Python，将自动下载一个便携版^（约 11MB^）。
goto :fetch_python

:check_py
"%PYEXE%" -c "import sys;assert sys.version_info>=(3,8)" >nul 2>nul
if errorlevel 1 (
    echo        这个 Python 版本太老^（需要 3.8 以上^），改用便携版。
    set "PYEXE="
    goto :fetch_python
)
rem 取版本号不能用 for /f：括号里的命令带引号 exe 路径时，cmd 会把整条命令的
rem 首尾引号剥掉，变成「python.exe" -c "import 不是内部或外部命令」——
rem 实测 usebackq、双外引号都救不了。改用临时文件中转，笨但绝对稳。
set "PYVER="
"%PYEXE%" -c "import sys;open(r'%TEMP%\pyver.txt','w').write(sys.version.split()[0])" >nul 2>nul
if exist "%TEMP%\pyver.txt" set /p PYVER=<"%TEMP%\pyver.txt"
del "%TEMP%\pyver.txt" >nul 2>nul
echo        版本 OK：Python %PYVER%
goto :install_deps

rem ================= 步骤 2/4：下载便携版 Python =================
:fetch_python
echo.
echo  [2/4] 正在下载便携版 Python ...
set "ZIPNAME=python-3.11.9-embed-amd64.zip"
set "ZIPURL=https://registry.npmmirror.com/-/binary/python/3.11.9/python-3.11.9-embed-amd64.zip"
set "ZIP2URL=https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip"

if not exist "%~dp0程序\_runtime_dl" mkdir "%~dp0程序\_runtime_dl"

echo        下载源 1/2 ......
powershell -NoProfile -ExecutionPolicy Bypass -Command "try{Invoke-WebRequest -Uri '%ZIPURL%' -OutFile '%~dp0程序\_runtime_dl\%ZIPNAME%' -UseBasicParsing -TimeoutSec 120}catch{exit 1}" >nul 2>nul
if not exist "%~dp0程序\_runtime_dl\%ZIPNAME%" (
    echo        下载源 1 失败，尝试下载源 2 ......
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try{Invoke-WebRequest -Uri '%ZIP2URL%' -OutFile '%~dp0程序\_runtime_dl\%ZIPNAME%' -UseBasicParsing -TimeoutSec 180}catch{exit 1}" >nul 2>nul
)
if not exist "%~dp0程序\_runtime_dl\%ZIPNAME%" (
    echo.
    echo   [X] Python 下载失败。多半是网络被限制。
    echo       两个办法，任选其一：
    echo         1^) 连手机热点后重新双击本脚本；
    echo         2^) 手动下载 python-3.11.9-embed-amd64.zip
    echo            地址: https://mirrors.huaweicloud.com/python/3.11.9/
    echo            放到「程序\_runtime_dl」文件夹后，并确认文件名是 python-3.11.9-embed-amd64.zip，
    echo            再重新双击本脚本。
    echo.
    pause
    exit /b 1
)

echo        解压中 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%~dp0程序\_runtime_dl\%ZIPNAME%' -DestinationPath '%~dp0程序\runtime' -Force" >nul 2>nul
if not exist "%~dp0程序\runtime\python.exe" (
    echo   [X] 解压失败，请手动把 zip 解压到「程序」文件夹里的 runtime 目录。
    pause
    exit /b 1
)

rem 便携版默认不能用 pip，得先把 ._pth 里的 import site 放开并加入 site-packages
echo        配置便携版运行时 ...
for %%f in ("%~dp0程序\runtime\python*._pth") do (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%%f'; $c=Get-Content $p; $c = $c -replace '#import site','import site'; if(-not ($c -match 'site-packages')){$c = $c + 'Lib\site-packages'}; Set-Content -Path $p -Value $c -Encoding ascii" >nul 2>nul
)
if exist "%~dp0程序\runtime\python311._pth" copy /y "%~dp0程序\runtime\python311._pth" "%~dp0程序\runtime\_pth_backup.txt" >nul 2>nul
set "PYEXE=%~dp0程序\runtime\python.exe"
echo        便携版就绪。

rem ================= 步骤 3/4：安装 pip 与依赖 =================
:install_deps
echo.
echo  [3/4] 正在安装运行需要的组件 ...

set "MIRROR=-i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn"

rem 检查 pip 是否可用（便携版第一次是没有的）
"%PYEXE%" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo        首次安装，正在获取 pip ...
    rem 这个缓存文件夹只在「下载 Python」那条路上创建；如果自带运行时已存在
    rem （步骤 2 被跳过），这里就不会有它 —— 往不存在的文件夹下载必挂。
    if not exist "%~dp0程序\_runtime_dl" mkdir "%~dp0程序\_runtime_dl"
    rem 千万别用 set + %变量% 在同一个括号块里传路径：cmd 的百分号展开发生在
    rem 整块解析时，变量会展开成空，PowerShell 拿到 -OutFile '' 必挂。
    rem 所以这里全部内联字面路径。阿里云源放第一位 —— 实测 bootstrap.pypa.io
    rem 在国内经常卡到超时，别让人白等两分钟。
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try{Invoke-WebRequest -Uri 'https://mirrors.aliyun.com/pypi/get-pip.py' -OutFile '%~dp0程序\_runtime_dl\get-pip.py' -UseBasicParsing -TimeoutSec 120}catch{exit 1}" >nul 2>nul
    if not exist "%~dp0程序\_runtime_dl\get-pip.py" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "try{Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%~dp0程序\_runtime_dl\get-pip.py' -UseBasicParsing -TimeoutSec 120}catch{exit 1}" >nul 2>nul
    )
    if not exist "%~dp0程序\_runtime_dl\get-pip.py" (
        echo   [X] 获取 pip 失败，请检查网络后重试。
        pause
        exit /b 1
    )
    "%PYEXE%" "%~dp0程序\_runtime_dl\get-pip.py" %MIRROR% --no-warn-script-location >nul 2>nul
)

"%PYEXE%" -m pip install requests openpyxl %MIRROR% --no-warn-script-location --quiet
"%PYEXE%" -c "import requests,openpyxl" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [X] 组件安装失败。请检查网络后重新双击本脚本。
    pause
    exit /b 1
)
echo        组件安装完成：requests + openpyxl

rem ================= 步骤 4/4：自检 =================
echo.
echo  [4/4] 正在自检 ...
"%PYEXE%" 程序\cert_downloader.py check
if errorlevel 1 (
    echo.
    echo   [!] 自检没完全通过，多数是网站暂时连不上，不影响安装本身。
    echo       可以稍后双击「② 启动工具.bat」再试。
)

echo.
echo  ==============================================================
echo    安装完成！
echo.
echo    以后使用：直接双击本文件夹里的「② 启动工具.bat」
echo  ==============================================================
echo.
pause
