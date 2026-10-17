<#
  Baixa o VLC portatil (sem instalacao) e prepara a subpasta "vlc" usada pelo IPTV Player.
  Uso:  powershell -ExecutionPolicy Bypass -File preparar_vlc.ps1 [-Arch win32|win64] [-Version 3.0.24]
#>
param(
    [ValidateSet("win32", "win64")]
    [string]$Arch = "win32",
    [string]$Version = "3.0.24"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $root "vlc"
$marker = Join-Path $dest "vlc-versao.txt"
if ((Test-Path (Join-Path $dest "libvlc.dll")) -and (Test-Path $marker)) {
    if ((Get-Content $marker -Raw).Trim() -eq "$Version-$Arch") {
        Write-Host "VLC $Version ($Arch) ja esta na pasta vlc."
        exit 0
    }
}
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }

$name = "vlc-$Version-$Arch.zip"
$work = Join-Path ([IO.Path]::GetTempPath()) "iptv-vlc-$Version-$Arch"
New-Item -ItemType Directory -Force -Path $work | Out-Null
$zip = Join-Path $work $name
$bases = @(
    "https://download.videolan.org/pub/videolan/vlc/$Version/$Arch",
    "https://get.videolan.org/vlc/$Version/$Arch",
    "http://download.videolan.org/pub/videolan/vlc/$Version/$Arch"
)

function Get-RemoteFile([string]$url, [string]$out) {
    if (Test-Path $out) { Remove-Item $out -Force }
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -L --fail --retry 3 --connect-timeout 30 -s -S -o $out $url
        return ($LASTEXITCODE -eq 0 -and (Test-Path $out))
    }
    try {
        Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
        return (Test-Path $out)
    } catch {
        return $false
    }
}

$ok = $false
foreach ($b in $bases) {
    Write-Host "Baixando $b/$name ..."
    if (-not (Get-RemoteFile "$b/$name" $zip)) { continue }
    if ((Get-Item $zip).Length -lt 20MB) { Write-Warning "Arquivo incompleto, tentando outro servidor..."; continue }
    $shaFile = "$zip.sha256"
    if (Get-RemoteFile "$b/$name.sha256" $shaFile) {
        $expected = (((Get-Content $shaFile -Raw).Trim()) -split '\s+')[0].ToLower()
        $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if ($expected -match '^[0-9a-f]{64}$') {
            if ($expected -ne $actual) { Write-Warning "SHA-256 nao confere, tentando outro servidor..."; continue }
            Write-Host "SHA-256 conferido."
        }
    }
    $ok = $true
    break
}
if (-not $ok) { throw "Nao foi possivel baixar o VLC $Version ($Arch). Verifique a internet e tente de novo." }

$ext = Join-Path $work "extraido"
if (Test-Path $ext) { Remove-Item $ext -Recurse -Force }
New-Item -ItemType Directory -Force -Path $ext | Out-Null
Write-Host "Extraindo..."
$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
$extracted = $false
if ($tar) {
    & $tar.Source -xf $zip -C $ext
    $extracted = ($LASTEXITCODE -eq 0)
}
if (-not $extracted) { Expand-Archive -Path $zip -DestinationPath $ext -Force }

$src = Get-ChildItem $ext -Directory | Where-Object { Test-Path (Join-Path $_.FullName "libvlc.dll") } | Select-Object -First 1
if (-not $src) { throw "libvlc.dll nao encontrada no pacote do VLC." }

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $src.FullName "libvlc.dll") $dest
Copy-Item (Join-Path $src.FullName "libvlccore.dll") $dest
Copy-Item (Join-Path $src.FullName "plugins") $dest -Recurse
foreach ($n in @("COPYING.txt", "AUTHORS.txt", "NEWS.txt")) {
    $f = Join-Path $src.FullName $n
    if (Test-Path $f) { Copy-Item $f $dest }
}
# a interface grafica do VLC nao e usada pelo player embutido (economiza ~40 MB)
foreach ($d in @("gui", "visualization")) {
    $p = Join-Path $dest "plugins\$d"
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}
# cache de plugins: o VLC abre mais rapido (opcional; ignora se nao der)
$gen = Join-Path $src.FullName "vlc-cache-gen.exe"
if (Test-Path $gen) {
    try {
        $plug = Join-Path $dest "plugins"
        Start-Process -FilePath $gen -ArgumentList "`"$plug`"" -Wait -WindowStyle Hidden
    } catch { }
}
Set-Content -Path $marker -Value "$Version-$Arch" -Encoding ASCII
$size = (Get-ChildItem $dest -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("VLC {0} ({1}) pronto em {2} ({3:N0} MB)" -f $Version, $Arch, $dest, $size)
