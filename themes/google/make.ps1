# Compile a deck that uses the Google themes: .\themes\google\make.ps1 path\to\deck.tex [outdir]
# (LuaLaTeX twice, with this folder on TEXINPUTS so \usetheme{gdg} and its fonts are found.)
param([Parameter(Mandatory = $true)][string]$Tex, [string]$OutDir = "")
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$miktex = Join-Path $env:LOCALAPPDATA "Programs\MiKTeX\miktex\bin\x64"
if (-not (Get-Command lualatex -ErrorAction SilentlyContinue) -and (Test-Path $miktex)) { $env:PATH = "$miktex;$env:PATH" }
$env:TEXINPUTS = ($here -replace '\\', '/') + "//;"
# The fonts are not committed (third-party binaries): build them the first time.
if (-not (Test-Path (Join-Path $here "fonts\GoogleSansFlex-SemiBoldItalic.ttf"))) {
    $venv = Join-Path $here "..\..\.venv\Scripts\python.exe"
    $python = if (Test-Path $venv) { $venv } else { "python" }
    & $python (Join-Path $here "fonts\build_fonts.py")
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
$texPath = Resolve-Path $Tex
if (-not $OutDir) { $OutDir = Split-Path -Parent $texPath }
New-Item -ItemType Directory -Force $OutDir | Out-Null
$out = (Resolve-Path $OutDir).Path -replace '\\', '/'
Push-Location (Split-Path -Parent $texPath)
try {
    foreach ($pass in 1, 2) {
        $log = lualatex -interaction=nonstopmode -halt-on-error "-output-directory=$out" (Split-Path -Leaf $texPath)
        if ($LASTEXITCODE -ne 0) { $log | Select-String -Pattern '^!' -Context 0, 6; exit 1 }
    }
    $log | Select-String -Pattern 'Output written'
} finally { Pop-Location }
