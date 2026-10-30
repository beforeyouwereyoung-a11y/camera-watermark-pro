<#
.SYNOPSIS
    下载并安装 ExifTool 到项目本地的 ./exiftool/ 目录（可选依赖）。

.DESCRIPTION
    程序优先调用 ExifTool 读写 EXIF；未安装时自动降级用 Pillow，功能不受阻。
    本脚本从 exiftool.org 获取最新 Windows 64 位包，解压为项目根目录下的
    exiftool\exiftool.exe + exiftool\exiftool_files\ 结构。

.PARAMETER Version
    指定 ExifTool 版本（如 13.30）。省略则自动查询官网最新版。

.PARAMETER Dest
    安装目标目录，默认是仓库根目录下的 exiftool\。

.EXAMPLE
    pwsh scripts/setup_exiftool.ps1
    pwsh scripts/setup_exiftool.ps1 -Version 13.30
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Dest = (Join-Path (Split-Path -Parent $PSScriptRoot) "exiftool")
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

if (-not $Version) {
    Write-Host "查询 ExifTool 最新版本 ..."
    $home = Invoke-WebRequest -Uri "https://exiftool.org/" -UseBasicParsing
    $m = [regex]::Match($home.Content, 'exiftool-([\d.]+)_64\.zip')
    if (-not $m.Success) {
        throw "无法从 exiftool.org 解析版本号，请手动下载：https://exiftool.org/"
    }
    $Version = $m.Groups[1].Value
}

$url = "https://exiftool.org/exiftool-${Version}_64.zip"
$tmp = Join-Path $env:TEMP ("exiftool-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$zip = Join-Path $tmp "exiftool.zip"

try {
    Write-Host "下载 $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force

    $inner = Get-ChildItem -LiteralPath $tmp -Directory |
        Where-Object { $_.Name -like 'exiftool-*' } | Select-Object -First 1
    if (-not $inner) { throw "解压后未找到 exiftool-* 目录" }

    # 官方包里可执行文件名通常是 exiftool(-k).exe
    $exe = Get-ChildItem -LiteralPath $inner.FullName -File |
        Where-Object { $_.Name -like 'exiftool*.exe' } | Select-Object -First 1
    if (-not $exe) { throw "解压后未找到 exiftool 可执行文件" }

    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    Copy-Item -LiteralPath $exe.FullName -Destination (Join-Path $Dest 'exiftool.exe') -Force
    Copy-Item -LiteralPath (Join-Path $inner.FullName 'exiftool_files') -Destination (Join-Path $Dest 'exiftool_files') -Recurse -Force

    Write-Host ""
    Write-Host "✅ ExifTool $Version 已安装到: $Dest"
    & (Join-Path $Dest 'exiftool.exe') -ver
}
finally {
    if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
