<#
Builds and publishes a release from this machine.

    .\packaging\windows\release.ps1 -Tag v0.1.0-beta.1          # unsigned beta, as a DRAFT to review
    .\packaging\windows\release.ps1 -Tag v0.1.0-beta.1 -Sign    # a signed beta
    .\packaging\windows\release.ps1 -Tag v0.1.0 -Publish        # signed stable, published straight away

Stable releases are always signed. Pre-releases (alpha, beta, rc) are not, unless -Sign is given:
Windows then warns testers about an unknown publisher, and SHA256SUMS.txt lets them check the files.

Releases are made locally, not by GitHub Actions, because the EV code-signing certificate is tied
to this machine (token / SimplySign) and cannot be used from a hosted build runner.

Tag forms:  v1.2.3 (stable)   v1.2.3-alpha.N   v1.2.3-beta.N   v1.2.3-rc.N  (pre-releases)
The tag must match the version in pyproject.toml, which uses the Python spelling:
    v0.1.0-beta.1  <->  version = "0.1.0b1"

Needs the GitHub CLI (gh) signed in with push access.
#>
param(
    [Parameter(Mandatory)][string]$Tag,
    # Publish immediately instead of leaving a draft.
    [switch]$Publish,
    # Sign a pre-release too. Stable releases are always signed.
    [switch]$Sign,
    # Skip signing a stable release (for rehearsals only; the release is then always a draft).
    [switch]$Unsigned
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = Resolve-Path (Join-Path $here "..\..")
Set-Location $root

if ($Tag -notmatch '^v(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?$') {
    throw "Tag must look like v1.2.3, v1.2.3-alpha.N, v1.2.3-beta.N or v1.2.3-rc.N."
}
$prerelease = [bool]$Matches[2]
if ($Sign -and $Unsigned) { throw "Use either -Sign or -Unsigned, not both." }
$signed = if ($prerelease) { [bool]$Sign } else { -not $Unsigned }
# An unsigned build under a stable tag is a rehearsal and must not be published.
$draft = (-not $Publish) -or (-not $signed -and -not $prerelease)
$stage = @{ alpha = "a"; beta = "b"; rc = "rc" }
$expected = $Matches[1] + $(if ($prerelease) { $stage[$Matches[2]] + $Matches[3] })

$version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
if ($version -ne $expected) { throw "pyproject.toml says version $version but tag $Tag needs $expected. Update the version first (pyproject.toml, vsm/__init__.py, web/package.json)." }
$package = (Select-String -Path vsm\__init__.py -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
if ($package -ne $version) { throw "vsm/__init__.py says $package but pyproject.toml says $version." }

if (git status --porcelain) { throw "There are uncommitted changes. Commit them so the release matches the tagged source." }
if (git tag --list $Tag) { throw "Tag $Tag already exists." }
gh auth status *> $null
if ($LASTEXITCODE) { throw "The GitHub CLI is not signed in. Run: gh auth login" }

if ($signed) {
    & (Join-Path $here "build.ps1") -Sign -RequireInstaller
} else {
    Write-Warning $(if ($prerelease) { "Building an UNSIGNED pre-release." } else { "Building UNSIGNED. The release will be left as a draft." })
    & (Join-Path $here "build.ps1") -RequireInstaller
}

$wheel = Get-ChildItem (Join-Path $root "build\release") -Filter "vein_server_manager-$version-*.whl" | Select-Object -First 1
if (-not $wheel) { throw "The Linux package (wheel) for $version was not built." }
$assets = @(
    (Join-Path $root "build\release\VSM $version Setup.exe"),
    (Join-Path $root "build\release\VSM $version Portable.zip"),
    $wheel.FullName   # Linux managers update themselves from this
)
$sums = Join-Path $root "build\release\SHA256SUMS.txt"
# Listed under the names people download: GitHub stores uploads with dots for spaces.
$assets | ForEach-Object { "{0}  {1}" -f (Get-FileHash $_ -Algorithm SHA256).Hash.ToLower(), ((Split-Path $_ -Leaf) -replace ' ', '.') } | Set-Content $sums -Encoding ascii
$assets += $sums

git tag -a $Tag -m "Vein Server Manager $Tag"
git push origin $Tag
if ($LASTEXITCODE) { throw "Pushing the tag failed." }

$arguments = @("release", "create", $Tag) + $assets + @("--title", "Vein Server Manager $Tag", "--generate-notes", "--verify-tag")
if (-not $signed) {
    $arguments += @("--notes", "**This build is not code-signed.** Windows will warn about an unknown publisher (More info > Run anyway). Check the download against SHA256SUMS.txt: ``Get-FileHash '.\VSM.$version.Setup.exe'``")
}
if ($prerelease) { $arguments += "--prerelease" }
if ($draft) { $arguments += "--draft" }
gh @arguments
if ($LASTEXITCODE) { throw "Creating the GitHub release failed." }

Write-Host ""
if ($draft) {
    Write-Host "Draft release created. Review it on GitHub, then publish it there (or: gh release edit $Tag --draft=false)."
} else {
    Write-Host "Published $Tag$(if ($prerelease) { ' as a pre-release' })$(if (-not $signed) { ' (unsigned)' })."
}
