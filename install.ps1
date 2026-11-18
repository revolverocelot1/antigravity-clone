# PowerShell installer for antigravity-clone
$ErrorActionPreference = "Stop"

$target = Join-Path $HOME ".gemini\config\plugins\antigravity-clone"
$repo = "https://github.com/revolverocelot1/antigravity-clone.git"

Write-Host "Installing antigravity-clone to $target..." -ForegroundColor Cyan

if (Test-Path $target) {
    if (Test-Path (Join-Path $target ".git")) {
        Write-Host "Updating existing git clone..."
        git -C $target pull --ff-only
    } else {
        Write-Host "Existing plugin folder found without git; updating files..."
    }
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    git clone $repo $target
}

Write-Host "Verifying installation..." -ForegroundColor Cyan
python -u (Join-Path $target "test_clone_plugin.py")

Write-Host "`nAntigravity Clone installed successfully! Type /clone or .clone in any chat." -ForegroundColor Green
