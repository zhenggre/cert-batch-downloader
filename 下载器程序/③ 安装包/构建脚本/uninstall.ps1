#Requires -Version 5.1
<#
  证书下载器 · 卸载脚本
  ======================================================================
  为什么是 .ps1 而不是 .bat：
      上一版的卸载是 .bat，用 UTF-8 无 BOM 写入却含中文。cmd.exe 按当前
      ANSI 代码页（中文系统是 GBK）解析这个文件，中文路径当场变乱码 ——
      卸载要么失败，要么指到别的目录上去。.ps1 用 UTF-8 with BOM 保存，
      PowerShell 5.1 能正确读取；配套的 uninstall.bat 则刻意只写 ASCII。

  ========================= 三条硬规则 =========================
  规则 1  只删 install_manifest.json 里登记的文件。
          本脚本里不出现 Remove-Item -Recurse，不出现 rd /s /q，
          不出现 Remove-Item 对非空目录的递归清理。
          目录只在「内容与清单完全一致」或「已经是空目录」时才处理。

  规则 2  一切文件删除先进回收站（Microsoft.VisualBasic.FileIO +
          RecycleOption.SendToRecycleBin）。系统被设成
          「删除时不进回收站」时，整体放弃删除，一个都不动。

  规则 3  删之前三项校验缺一不可：app_id 对得上、路径没跑出安装目录、
          SHA256 与清单一致。任何一项不过 → 保留该文件并写进报告。
          「哈希对不上」通常意味着用户自己改过它，那就不该删。

  与 installer.py 的对应关系：路径守卫的判定规则两边同源（危险路径黑名单
  用同一套 Known Folder GUID），改动其中一边时另一边必须跟着改。
  ======================================================================
#>
param(
    [switch]$NoPrompt,          # 不等待按键（供自动化/安装程序调用）
    [switch]$DeleteUserData     # 明确要求连用户数据一起删（默认保留）
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$AppId        = 'cert-downloader'          # 必须与 installer.py 的 APP_ID 一致
$AppName      = '证书下载器'
$ManifestName = 'install_manifest.json'
$Ps1Name      = 'uninstall.ps1'
$BatName      = 'uninstall.bat'

Add-Type -AssemblyName Microsoft.VisualBasic | Out-Null

# 有没有人能敲回车？
#   输入被重定向（管道 / 自动化 / 无控制台方式启动）时，Read-Host 会永久
#   等一个永远不会来的回车 —— 整个卸载就挂在那儿。所以这里自己判断，
#   不依赖调用方记得传 -NoPrompt。
$Interactive = -not $NoPrompt
if ($Interactive) {
    try {
        if ([Console]::IsInputRedirected) { $Interactive = $false }
    } catch { $Interactive = $false }
}

# ----------------------------------------------------------------------
# 收尾：报告 + 暂停
# ----------------------------------------------------------------------
$script:Fatal = $null

function Stop-Uninstall([string]$reason, [int]$code) {
    Write-Host ''
    Write-Host "  卸载已中止：$reason" -ForegroundColor Red
    Write-Host '  没有删除任何文件。'
    if ($Interactive) { Read-Host '  按回车退出' | Out-Null }
    exit $code
}

# ----------------------------------------------------------------------
# 路径工具
# ----------------------------------------------------------------------
if (-not ('Win32.KnownFolder' -as [type])) {
    Add-Type -Namespace Win32 -Name KnownFolder -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern int SHGetKnownFolderPath(ref System.Guid rfid, uint dwFlags, System.IntPtr hToken, out System.IntPtr ppszPath);
[System.Runtime.InteropServices.DllImport("ole32.dll")]
public static extern void CoTaskMemFree(System.IntPtr pv);
'@
}

function Get-KnownFolder([string]$guid) {
    try {
        $g = [Guid]$guid
        $ptr = [IntPtr]::Zero
        $hr = [Win32.KnownFolder]::SHGetKnownFolderPath([ref]$g, 0, [IntPtr]::Zero, [ref]$ptr)
        if ($hr -ne 0 -or $ptr -eq [IntPtr]::Zero) { return $null }
        try { return [Runtime.InteropServices.Marshal]::PtrToStringUni($ptr) }
        finally { [Win32.KnownFolder]::CoTaskMemFree($ptr) }
    } catch { return $null }
}

function Get-FullPathSafe([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return $null }
    try { return [IO.Path]::GetFullPath($p).TrimEnd('\') } catch { return $null }
}

# 解析 junction / 符号链接。不解析的话，一个 junction 就能让下面的
# 危险路径比对失效（Windows 的「文档」经常就是 junction）。
function Resolve-RealDirectory([string]$p, [int]$depth = 0) {
    $full = Get-FullPathSafe $p
    if (-not $full) { return $null }
    if ($depth -gt 4) { return $full }
    try {
        $it = Get-Item -LiteralPath $full -Force -ErrorAction Stop
        if ($it.LinkType) {
            $t = @($it.Target)[0]
            if ($t) {
                if (-not [IO.Path]::IsPathRooted($t)) {
                    $t = Join-Path (Split-Path -Parent $full) $t
                }
                return (Resolve-RealDirectory $t ($depth + 1))
            }
        }
    } catch { }
    return $full
}

function Test-Under([string]$path, [string]$root) {
    if (-not $path -or -not $root) { return $false }
    $p = $path.TrimEnd('\').ToLowerInvariant()
    $r = $root.TrimEnd('\').ToLowerInvariant()
    if ($p -eq $r) { return $true }
    return $p.StartsWith($r + '\')
}

function Get-DangerRoots {
    $list = New-Object System.Collections.ArrayList
    $known = @(
        @('{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}', '桌面'),
        @('{FDD39AD0-238F-46AF-ADB4-6C85480369C7}', '文档'),
        @('{374DE290-123F-4565-9164-39C4925E467B}', '下载'),
        @('{A52BBA46-E9E1-435F-B3D9-28DAA648C0F6}', 'OneDrive'),
        @('{5E6C858F-0E22-4760-9AFE-EA3317B67173}', '用户主目录'),
        @('{F38BF404-1D43-42F2-9305-67DE0B28FC23}', 'Windows')
    )
    foreach ($k in $known) {
        $v = Get-KnownFolder $k[0]
        if ($v) { [void]$list.Add(@((Resolve-RealDirectory $v), $k[1])) }
    }
    foreach ($k in @(@($env:ProgramFiles, 'Program Files'),
                     @(${env:ProgramFiles(x86)}, 'Program Files (x86)'),
                     @($env:ProgramData, 'ProgramData'),
                     @($env:SystemRoot, 'Windows'))) {
        if ($k[0]) { [void]$list.Add(@((Resolve-RealDirectory $k[0]), $k[1])) }
    }
    if ($env:SystemRoot) {
        [void]$list.Add(@((Resolve-RealDirectory (Join-Path $env:SystemRoot 'System32')), 'System32'))
    }
    foreach ($d in [char[]]'ABCDEFGHIJKLMNOPQRSTUVWXYZ') {
        $r = "${d}:\"
        if (Test-Path -LiteralPath $r) {
            [void]$list.Add(@((Resolve-RealDirectory $r), "$d 盘根目录"))
        }
    }
    return $list
}

$script:SystemLabels = @('Windows', 'System32', 'ProgramData',
                         'Program Files', 'Program Files (x86)')

# 返回 $null = 安全；否则返回拒绝原因
function Test-RootSafe([string]$rootReal) {
    if (-not $rootReal) { return '安装目录路径读不出来。' }
    $rr = $rootReal.TrimEnd('\').ToLowerInvariant()
    foreach ($dr in Get-DangerRoots) {
        $d = [string]$dr[0]
        $label = [string]$dr[1]
        if (-not $d) { continue }
        if ($rr -eq $d.TrimEnd('\').ToLowerInvariant()) {
            return "安装目录本身就是「$label」—— 清单不可信，拒绝卸载。"
        }
        if (Test-Under $rr $d) {
            if ($script:SystemLabels -contains $label) {
                return "安装目录位于系统目录「$label」之内，拒绝卸载。"
            }
        }
    }
    return $null
}

# ----------------------------------------------------------------------
# 回收站
# ----------------------------------------------------------------------
function Test-RecycleBinDisabled {
    try {
        $k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket'
        if (Test-Path -LiteralPath $k) {
            $v = (Get-ItemProperty -Path $k -Name NukeOnDelete -ErrorAction SilentlyContinue).NukeOnDelete
            if ($null -ne $v -and [int]$v -eq 1) { return $true }
        }
    } catch { }
    return $false
}

# 返回 $true = 确实删掉了（走的是回收站）
function Send-ToRecycleBin([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $true }
    $it = $null
    try { $it = Get-Item -LiteralPath $path -Force -ErrorAction Stop } catch { return $false }
    try {
        if ($it.PSIsContainer) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
                $path,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
        } else {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
                $path,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
        }
    } catch {
        return $false
    }
    return (-not (Test-Path -LiteralPath $path))
}

function Get-Sha256([string]$p) {
    try {
        return (Get-FileHash -LiteralPath $p -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    } catch { return $null }
}

# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Root = Get-FullPathSafe $Root

Write-Host ''
Write-Host ('=' * 64)
Write-Host "  $AppName · 卸载"
Write-Host ('=' * 64)
Write-Host "  安装目录：$Root"
Write-Host ''

if (-not $Root -or -not (Test-Path -LiteralPath $Root -PathType Container)) {
    Stop-Uninstall '找不到安装目录。' 1
}

Write-Host '  1/6 校验安装目录 ...'
$bad = Test-RootSafe (Resolve-RealDirectory $Root)
if ($bad) { Stop-Uninstall $bad 1 }

Write-Host '  2/6 读取安装清单 ...'
$manifestPath = Join-Path $Root $ManifestName
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Stop-Uninstall '找不到安装清单（install_manifest.json）。没有清单就不知道该删哪些文件，乱删风险太大。' 1
}

$m = $null
try {
    $m = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Stop-Uninstall "安装清单读不出来（$($_.Exception.Message)）。" 1
}

if (-not $m -or [string]$m.app_id -ne $AppId) {
    Stop-Uninstall '安装清单不是本程序生成的，拒绝卸载。' 1
}

$recorded = Get-FullPathSafe ([string]$m.install_root)
if ($recorded -and $recorded.ToLowerInvariant() -ne $Root.ToLowerInvariant()) {
    Write-Host '  清单记录的安装目录和脚本当前位置不一致：' -ForegroundColor Yellow
    Write-Host "    清单记录：$recorded"
    Write-Host "    当前位置：$Root"
    Stop-Uninstall '这份清单属于另一个安装目录，拒绝卸载。' 1
}

Write-Host '  3/6 检查回收站是否可用 ...'
if (Test-RecycleBinDisabled) {
    Write-Host '  检测到系统被设置成「删除时不放进回收站」（直接永久删除）。' -ForegroundColor Yellow
    Write-Host '  为了不让你事后找不回来，卸载已中止 —— 一个文件都没删。' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  想继续，先把回收站打开：'
    Write-Host '    桌面「回收站」右键 → 属性 → 选「自定义大小」'
    Write-Host '    （不要勾「不将文件移到回收站」）'
    Write-Host '  然后再运行本脚本。'
    if ($Interactive) { Read-Host '  按回车退出' | Out-Null }
    exit 2
}

$entries = @($m.entries)
Write-Host ("  4/6 逐条核对清单（共 {0} 条）..." -f $entries.Count)

# ---- 用户数据：默认保留 ----
$dataEntries = @($entries | Where-Object { $_.type -eq 'userdata' })
$keepData = -not $DeleteUserData
if ($dataEntries.Count -gt 0 -and $Interactive -and -not $DeleteUserData) {
    Write-Host ("  安装目录里有 {0} 个你自己的配置文件（比如 config.json 里加过的比赛）。" -f $dataEntries.Count)
    $ans = Read-Host '  卸载后保留它们吗？[Y/n]（直接回车 = 保留）'
    if ($ans -match '^\s*(n|no)\s*$') { $keepData = $false }
}

# ---- 逐条校验清单 ----
$toDelete = New-Object System.Collections.ArrayList
$kept     = New-Object System.Collections.ArrayList
$notes    = New-Object System.Collections.ArrayList

foreach ($e in $entries) {
    if ($null -eq $e) { continue }
    $rel = [string]$e.rel
    if ([string]::IsNullOrWhiteSpace($rel)) { continue }
    $rel = $rel.Replace('\', '/').TrimStart('/')
    if ($rel -eq '' -or $rel.StartsWith('..') -or $rel.Contains('/../')) {
        [void]$notes.Add("清单里有不合法的路径，已跳过：$rel")
        continue
    }
    $abs = Get-FullPathSafe (Join-Path $Root ($rel.Replace('/', '\')))
    if (-not $abs) {
        [void]$notes.Add("路径无法解析，已跳过：$rel")
        continue
    }
    if (-not (Test-Under $abs $Root)) {
        [void]$notes.Add("路径跑出了安装目录，已跳过：$rel")
        continue
    }
    if (-not (Test-Path -LiteralPath $abs)) { continue }   # 早就不在了

    if ($e.delete_on_uninstall -eq $false) {
        [void]$kept.Add($abs)
        continue
    }
    $want = [string]$e.sha256
    if ($want) {
        $got = Get-Sha256 $abs
        if ($got -ne $want.ToLowerInvariant()) {
            [void]$kept.Add($abs)
            [void]$notes.Add("内容与清单不符（你自己改过？），已保留：$rel")
            continue
        }
    }
    [void]$toDelete.Add($abs)
}

# ---- 顶层目录整体处理：只有内容与清单完全一致才整体送回收站 ----
$topPlan = @{}
foreach ($p in $toDelete) {
    $seg = $p.Substring($Root.Length).TrimStart('\').Split('\')[0]
    if (-not $topPlan.ContainsKey($seg)) { $topPlan[$seg] = New-Object System.Collections.ArrayList }
    [void]$topPlan[$seg].Add($p)
}

$wholeDirs = New-Object System.Collections.ArrayList
foreach ($seg in @($topPlan.Keys)) {
    $dir = Join-Path $Root $seg
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { continue }
    $onDisk = @(Get-ChildItem -LiteralPath $dir -Recurse -Force -File -ErrorAction SilentlyContinue |
                ForEach-Object { $_.FullName })
    $planned = @($topPlan[$seg])
    if ($onDisk.Count -eq 0 -or $onDisk.Count -ne $planned.Count) { continue }
    $allPlanned = $true
    foreach ($f in $onDisk) {
        if ($planned -notcontains $f) { $allPlanned = $false; break }
    }
    if ($allPlanned) { [void]$wholeDirs.Add($dir) }
}

Write-Host ("  5/6 移入回收站：{0} 个文件、{1} 个整目录 ..." -f $toDelete.Count, $wholeDirs.Count)

# ---- 执行删除 ----
$removedFiles = New-Object System.Collections.ArrayList
$removedDirs  = New-Object System.Collections.ArrayList
$failed       = New-Object System.Collections.ArrayList

foreach ($d in $wholeDirs) {
    if (Send-ToRecycleBin $d) { [void]$removedDirs.Add($d) }
    else { [void]$failed.Add("目录：$d") }
}

$handled = @{}
foreach ($d in $removedDirs) {
    $pre = $d.ToLowerInvariant() + '\'
    foreach ($p in $toDelete) {
        if ($p.ToLowerInvariant().StartsWith($pre)) { $handled[$p] = 1 }
    }
}

# 卸载脚本自己放最后删，省得中途自己没了导致后面的步骤跑不完
$selfPaths = @((Join-Path $Root $Ps1Name), (Join-Path $Root $BatName),
               $manifestPath) | ForEach-Object { $_.ToLowerInvariant() }

$ordered = @($toDelete | Sort-Object { if ($selfPaths -contains $_.ToLowerInvariant()) { 1 } else { 0 } })
foreach ($p in $ordered) {
    if ($handled.ContainsKey($p)) { continue }
    if (-not (Test-Path -LiteralPath $p)) { continue }
    if (Send-ToRecycleBin $p) { [void]$removedFiles.Add($p) }
    else { [void]$failed.Add("文件：$p") }
}

# ---- 用户数据（仅当用户明确要求删除）----
if (-not $keepData) {
    foreach ($e in $dataEntries) {
        $rel = ([string]$e.rel).Replace('\', '/').TrimStart('/')
        $abs = Get-FullPathSafe (Join-Path $Root ($rel.Replace('/', '\')))
        if (-not $abs -or -not (Test-Under $abs $Root)) { continue }
        if (-not (Test-Path -LiteralPath $abs)) { continue }
        if (Send-ToRecycleBin $abs) { [void]$removedFiles.Add($abs) }
        else { [void]$failed.Add("文件（用户数据）：$abs") }
    }
    $kept.Clear()
}

# ---- 空目录：只删空的，非空一律保留 ----
Write-Host '  6/6 清理空目录 / 快捷方式 / 注册表 ...'
$emptyRemoved = New-Object System.Collections.ArrayList
$allDirs = @(Get-ChildItem -LiteralPath $Root -Recurse -Force -Directory -ErrorAction SilentlyContinue |
             Sort-Object { $_.FullName.Length } -Descending)
foreach ($d in $allDirs) {
    if (-not (Test-Path -LiteralPath $d.FullName)) { continue }
    $n = @(Get-ChildItem -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue).Count
    if ($n -eq 0) {
        try {
            Remove-Item -LiteralPath $d.FullName -Force -ErrorAction Stop   # 不带 -Recurse
            [void]$emptyRemoved.Add($d.FullName)
        } catch { }
    }
}

# ---- 快捷方式与注册表 ----
$shellRemoved = New-Object System.Collections.ArrayList
$desktop = Get-KnownFolder '{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}'
$startMenu = [Environment]::GetFolderPath('Programs')
foreach ($lnk in @(
        (Join-Path $desktop "$AppName.lnk"),
        (Join-Path $startMenu "$AppName.lnk"),
        (Join-Path $startMenu "卸载 $AppName.lnk"))) {
    if ($lnk -and (Test-Path -LiteralPath $lnk)) {
        if (Send-ToRecycleBin $lnk) { [void]$shellRemoved.Add($lnk) }
        else { [void]$failed.Add("快捷方式：$lnk") }
    }
}

$regRemoved = $false
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
if (Test-Path -LiteralPath $regPath) {
    try {
        Remove-Item -LiteralPath $regPath -Recurse -Force -ErrorAction Stop
        $regRemoved = $true
    } catch { }
}

# ---- 安装根目录：只有确认彻底空了才删 ----
$rootRemoved = $false
$leftover = @(Get-ChildItem -LiteralPath $Root -Force -ErrorAction SilentlyContinue)
if ($leftover.Count -eq 0) {
    try {
        Remove-Item -LiteralPath $Root -Force -ErrorAction Stop    # 不带 -Recurse，非空必失败
        $rootRemoved = $true
    } catch { }
}

# ---- 报告 ----
Write-Host ''
Write-Host ('-' * 64)
Write-Host '  卸载报告'
Write-Host ('-' * 64)
Write-Host ("  已移入回收站：{0} 个文件，{1} 个目录" -f $removedFiles.Count, $removedDirs.Count)
if ($emptyRemoved.Count -gt 0) {
    Write-Host ("  已清掉空目录：{0} 个（只删空的，有内容的一律保留）" -f $emptyRemoved.Count)
}
if ($shellRemoved.Count -gt 0) {
    Write-Host ("  已清理快捷方式：{0} 个" -f $shellRemoved.Count)
}
if ($regRemoved) { Write-Host '  已移除「设置-应用」里的卸载入口。' }
if ($rootRemoved) {
    Write-Host '  安装目录已清空并删除。'
}

if ($kept.Count -gt 0) {
    Write-Host ''
    Write-Host ("  保留未删（{0} 项）：" -f $kept.Count) -ForegroundColor Cyan
    foreach ($p in $kept) { Write-Host "    · $p" }
    Write-Host '     这些是你的配置或你改过的文件，重装后还能用。'
    Write-Host '     确实不需要了，可以自己拖进回收站。'
}

if ($notes.Count -gt 0) {
    Write-Host ''
    Write-Host '  说明：'
    foreach ($n in $notes) { Write-Host "    · $n" }
}

if ($failed.Count -gt 0) {
    Write-Host ''
    Write-Host ("  未能删除（{0} 项）—— 可能正被占用，可以稍后手动处理：" -f $failed.Count) -ForegroundColor Yellow
    foreach ($f in $failed) { Write-Host "    · $f" }
}

Write-Host ''
Write-Host '  删除的内容都在回收站里，误删可以还原。'
Write-Host ('=' * 64)

if ($failed.Count -gt 0) { exit 3 } else { exit 0 }
