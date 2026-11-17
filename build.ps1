# Сборка одного .exe (Python + Pillow должны быть установлены)
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
$name = 'WallpaperCarousel-1.0.0-win64'
$root = $PSScriptRoot

python -m pip install --quiet --disable-pip-version-check pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name $name `
    --icon (Join-Path $root 'icon.ico') `
    --distpath (Join-Path $root 'dist') `
    --workpath (Join-Path $root 'build') `
    --specpath (Join-Path $root 'build') `
    (Join-Path $root 'carousel.py')

Write-Host ("готово: " + (Join-Path $root "dist\$name.exe"))
