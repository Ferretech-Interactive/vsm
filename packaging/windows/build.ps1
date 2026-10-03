<#
Builds the Windows desktop app, its installer and the portable zip.

    .\packaging\windows\build.ps1            # unsigned, for testing
    .\packaging\windows\build.ps1 -Sign      # signed with the code-signing certificate in the Windows store

Output (in <repo>\build\release):
    VSM <version> Setup.exe      installer (needs Inno Setup 6: winget install JRSoftware.InnoSetup)
    VSM <version> Portable.zip   the same program without an installer; data is kept beside the exe

Needs Python 3.11+ and Node.js 20.19+. Signing also needs signtool (Windows SDK) and, for a
certificate held on a token or in a cloud HSM, that device or its client to be connected.
#>
param(
    # Sign the executable, installer and uninstaller.
    [switch]$Sign,
    # SHA-1 thumbprint of the certificate to use. Default: the only code-signing certificate in the store.
    [string]$Thumbprint = $env:VSM_SIGN_THUMBPRINT,
    [string]$TimestampUrl = "http://time.certum.pl",
    # Fail instead of skipping when Inno Setup is missing.
    [switch]$RequireInstaller
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = Resolve-Path (Join-Path $here "..\..")
# Everything this script produces goes under <repo>\build, which git ignores.
$build = Join-Path $root "build\windows"
$work = Join-Path $build "work"
$output = Join-Path $root "build\release"
$app = Join-Path $build "app\VeinServerManager"

function Invoke-Step($name, [scriptblock]$block) {
    Write-Host "==> $name" -ForegroundColor Green
    $global:LASTEXITCODE = 0
    & $block
    if ($LASTEXITCODE) { throw "$name failed with exit code $LASTEXITCODE." }
}

$version = (Select-String -Path (Join-Path $root "pyproject.toml") -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
# Shown in each file's Properties > Details. Windows file versions are numeric only (0.1.0b1 -> 0.1.0.0).
$versionNumeric = ([regex]::Match($version, '^\d+\.\d+\.\d+').Value) + ".0"
$copyright = "Copyright (c) $((Get-Date).Year) FerreTech Interactive. Portions (c) 2025 Wolverinex1974."
$env:VSM_COPYRIGHT = $copyright   # read by vsm.spec for VeinServerManager.exe

# ---------------------------------------------------------------- signing setup

$signtool = $null
if ($Sign) {
    $signtool = (Get-Command signtool -ErrorAction SilentlyContinue).Source
    if (-not $signtool) {
        $signtool = (Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -Last 1).FullName
    }
    if (-not $signtool) { throw "signtool.exe was not found. Install the Windows SDK signing tools." }
    if (-not $Thumbprint) {
        $certs = @(Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert | Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) })
        if ($certs.Count -ne 1) { throw "Found $($certs.Count) usable code-signing certificates; pass -Thumbprint to choose one." }
        $Thumbprint = $certs[0].Thumbprint
        Write-Host "Signing as: $($certs[0].Subject.Split(',')[0])"
    }
}
function Add-Signature($file) {
    & $signtool sign /sha1 $Thumbprint /fd sha256 /tr $TimestampUrl /td sha256 /d "Vein Server Manager" $file
    if ($LASTEXITCODE) { throw "Signing $file failed." }
}
function Assert-Signed($file) {
    $sig = Get-AuthenticodeSignature $file
    if ($sig.Status -ne "Valid") { throw "$file is not validly signed ($($sig.Status))." }
}

# ---------------------------------------------------------------- application

Invoke-Step "Building the web UI" {
    Push-Location (Join-Path $root "web")
    try { npm ci --no-audit --no-fund; if (-not $LASTEXITCODE) { npm run build } } finally { Pop-Location }
}

# setuptools builds the package in <root>\build\lib and never removes what an earlier build left
# there, so files deleted from the source (old web UI bundles) would be packaged again.
function Clear-SetuptoolsScratch {
    $top = Join-Path $root "build"
    foreach ($stale in @(Join-Path $top "lib") + @(Get-ChildItem $top -Directory -Filter "bdist.*" -ErrorAction SilentlyContinue | ForEach-Object FullName)) {
        if (Test-Path $stale) { Remove-Item -Recurse -Force $stale }
    }
}

$venv = Join-Path $work "venv"
$python = Join-Path $venv "Scripts\python.exe"
Invoke-Step "Creating a clean build environment" {
    if (-not (Test-Path $python)) { python -m venv $venv }
    Clear-SetuptoolsScratch
    & $python -m pip install --quiet --disable-pip-version-check --upgrade "$($root)[desktop,discord]" pyinstaller
}

# ---------------------------------------------------------------- Linux package

# A wheel with the built web UI inside. A Linux manager downloads it to update itself
# (vsm/core/selfupdate.py), and the exe carries it to deploy to a Linux server over SSH
# (vsm/desktop/remote), which is why it is built first. It is published with every release.
Invoke-Step "Building the Linux package (wheel)" {
    $wheels = Join-Path $work "wheel"
    if (Test-Path $wheels) { Remove-Item -Recurse -Force $wheels }
    Clear-SetuptoolsScratch
    & $python -m pip wheel $root --no-deps --quiet --disable-pip-version-check --wheel-dir $wheels
    if (-not $LASTEXITCODE) {
        New-Item -ItemType Directory -Force $output | Out-Null
        Get-ChildItem $output -Filter "vein_server_manager-*.whl" -ErrorAction SilentlyContinue | Remove-Item
        Copy-Item (Join-Path $wheels "vein_server_manager-*.whl") $output
    }
}

Invoke-Step "Building VeinServerManager.exe $version" {
    & $python -m PyInstaller (Join-Path $here "vsm.spec") --noconfirm --distpath (Join-Path $build "app") --workpath (Join-Path $work "pyinstaller")
}

$exe = Join-Path $app "VeinServerManager.exe"
if ($Sign) {
    Invoke-Step "Signing VeinServerManager.exe" { Add-Signature $exe }
    Assert-Signed $exe
}

New-Item -ItemType Directory -Force $output | Out-Null

# ---------------------------------------------------------------- portable zip

$setupName = "VSM $version Setup.exe"
$portableName = "VSM $version Portable.zip"

Invoke-Step "Building $portableName" {
    $stage = Join-Path $work "portable\VeinServerManager"
    if (Test-Path (Split-Path $stage)) { Remove-Item -Recurse -Force (Split-Path $stage) }
    New-Item -ItemType Directory -Force (Split-Path $stage) | Out-Null
    Copy-Item $app $stage -Recurse
    # A relative data folder is resolved beside the executable, so the whole thing can be moved or deleted as one.
    Set-Content (Join-Path $stage "desktop.json") '{"data_dir": "data", "port": 8810}' -Encoding ascii
    $zip = Join-Path $output $portableName
    if (Test-Path $zip) { Remove-Item $zip }
    Compress-Archive -Path $stage -DestinationPath $zip
}

# ---------------------------------------------------------------- installer

# winget installs Inno Setup per-user; the classic installer and Chocolatey use Program Files.
$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    if ($RequireInstaller) { throw "Inno Setup 6 was not found." }
    Write-Warning "Inno Setup 6 not found; skipping the installer. Install it with: winget install JRSoftware.InnoSetup"
    return
}

# Runtime installers bundled into Setup.exe; only run on machines that lack them.
$redist = Join-Path $build "redist"
New-Item -ItemType Directory -Force $redist | Out-Null
$ProgressPreference = "SilentlyContinue"
foreach ($item in @(
        @{ File = "vc_redist.x64.exe"; Url = "https://aka.ms/vs/17/release/vc_redist.x64.exe" },
        @{ File = "MicrosoftEdgeWebview2Setup.exe"; Url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703" })) {
    $target = Join-Path $redist $item.File
    if (-not (Test-Path $target)) {
        Write-Host "==> Downloading $($item.File)" -ForegroundColor Green
        Invoke-WebRequest $item.Url -OutFile $target -UseBasicParsing
    }
    Assert-Signed $target
}

Invoke-Step "Building $setupName" {
    $arguments = @("/DAppDir=$app", "/DRedistDir=$redist", "/DAppVersion=$version", "/DAppVersionNumeric=$versionNumeric", "/DAppCopyright=$copyright", "/Qp")
    if ($Sign) {
        # Inno Setup signs the installer and its uninstaller itself; `$q` is its escape for a quote.
        $arguments += "/DSign=1"
        $arguments += "/Svsmsign=`$q$signtool`$q sign /sha1 $Thumbprint /fd sha256 /tr $TimestampUrl /td sha256 /d `$qVein Server Manager`$q `$f"
    }
    # Compiled in a scratch folder, then copied out: Inno Setup rewrites the file's resources in place,
    # which fails if anything else (antivirus, a Windows Sandbox share, Explorer) has the output folder open.
    $scratch = Join-Path $work "installer"
    $arguments += "/O$scratch"
    & $iscc @arguments (Join-Path $here "installer.iss")
    if (-not $LASTEXITCODE) { Copy-Item (Join-Path $scratch $setupName) $output -Force }
}
$setup = Join-Path $output $setupName
if ($Sign) { Assert-Signed $setup }

Write-Host ""
Write-Host "Built version $version$(if ($Sign) { ' (signed)' } else { ' (UNSIGNED: for testing only)' }):"
Get-ChildItem $output | ForEach-Object { Write-Host ("  {0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB)) }
