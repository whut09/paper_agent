param(
    [string]$PythonVersion,
    [switch]$CleanBabelDoc,
    [switch]$GenerateOfflineAssets,
    [switch]$DownloadVCRedist
)

Write-Host "==== Creating directories ===="
New-Item -Path "./build" -ItemType Directory -Force
New-Item -Path "./build/runtime" -ItemType Directory -Force
New-Item -Path "./dep_build" -ItemType Directory -Force

if ($CleanBabelDoc) {
    Write-Host "==== Cleaning babeldoctemp1234567 ===="
    if (Test-Path "./babeldoctemp1234567") {
        Remove-Item -Path "./babeldoctemp1234567" -Recurse -Force
        Write-Host "babeldoctemp1234567 deleted"
    }
}

Write-Host "==== Copying source to dep_build ===="
Get-ChildItem -Path "./" -Exclude "dep_build", "build" | Copy-Item -Destination "./dep_build" -Recurse -Force

Write-Host "==== Downloading and extracting Python $PythonVersion ===="
$pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
Write-Host "pythonUrl: $pythonUrl"
$pythonZip = "./dep_build/python.zip"
Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonZip
Expand-Archive -Path $pythonZip -DestinationPath "./build/runtime" -Force

if ($DownloadVCRedist) {
    Write-Host "==== Downloading Visual C++ Redistributable ===="
    $vcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $vcRedistPath = "./build/无法运行请安装vc_redist.x64.exe"
    Invoke-WebRequest -Uri $vcRedistUrl -OutFile $vcRedistPath
    Write-Host "Downloaded VC++ Redistributable to: $vcRedistPath"
}

Write-Host "==== Downloading and extracting PyStand ===="
$pystandUrl = "https://github.com/skywind3000/PyStand/releases/download/1.1.4/PyStand-v1.1.4-exe.zip"
$pystandZip = "./dep_build/PyStand.zip"
Invoke-WebRequest -Uri $pystandUrl -OutFile $pystandZip
Expand-Archive -Path $pystandZip -DestinationPath "./dep_build/PyStand" -Force

Write-Host "==== Copying PyStand.exe to build ===="
$pystandExe = "./dep_build/PyStand/PyStand-x64-CLI/PyStand.exe"
$destExe = "./build/paper_agent.exe"
if (Test-Path $pystandExe) {
    Copy-Item -Path $pystandExe -Destination $destExe -Force
} else {
    Write-Host "Error: PyStand.exe not found!"
    exit 1
}

Write-Host "==== Creating Python venv ===="
uv venv ./dep_build/venv
./dep_build/venv/Scripts/activate

Write-Host "==== Installing project dependencies ===="
uv pip install .

Write-Host "==== Copying site-packages to build ===="
Copy-Item -Path "./dep_build/venv/Lib/site-packages" -Destination "./build/site-packages" -Recurse -Force

Write-Host "==== Copying _pystand_static.int to build ===="
$staticFile = "./script/_pystand_static.int"
$destStatic = "./build/_pystand_static.int"
if (Test-Path $staticFile) {
    Copy-Item -Path $staticFile -Destination $destStatic -Force
} else {
    Write-Host "Error: script/_pystand_static.int not found!"
    exit 1
}

if ($GenerateOfflineAssets) {
    Write-Host "==== Generating offline assets ===="
    uv run --active babeldoc --generate-offline-assets ./build
}

Write-Host "==== Build complete ===="
