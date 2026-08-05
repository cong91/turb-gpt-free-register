#requires -Version 5.1
[CmdletBinding()]
param(
    [Alias("Host")]
    [string]$ListenHost = "127.0.0.1",

    [ValidateRange(1, 65535)]
    [int]$Port = 5057,

    [string]$AuthCode,

    [switch]$NoBrowser,

    [switch]$SkipInstall,

    [switch]$VerboseLogs,

    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$venvRoot = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$requirementsStamp = Join-Path $venvRoot ".requirements.sha256"

function Get-BootstrapPython {
    $launcher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($launcher) {
        return @($launcher.Source, "-3")
    }

    $python = Get-Command "python" -ErrorAction SilentlyContinue
    if ($python) {
        return @($python.Source)
    }

    throw "Khong tim thay Python. Hay cai Python 3.10+ va chay lai script."
}

function Assert-PythonVersion {
    param(
        [string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    $version = & $Executable @PrefixArguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0 -or -not $version) {
        throw "Khong the kiem tra phien ban Python."
    }

    $parts = $version.Trim().Split(".")
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
        throw "Can Python 3.10+; phien ban hien tai la $version."
    }

    Write-Host "[OK] Python $version"
}

function Assert-NodeVersion {
    $node = Get-Command "node" -ErrorAction SilentlyContinue
    if (-not $node) {
        throw "Khong tim thay Node.js. Hay cai Node.js 18+ va chay lai script."
    }

    $version = (& $node.Source --version).TrimStart("v")
    if ($LASTEXITCODE -ne 0 -or -not $version) {
        throw "Khong the kiem tra phien ban Node.js."
    }

    $major = [int]($version.Split(".")[0])
    if ($major -lt 18) {
        throw "Can Node.js 18+; phien ban hien tai la $version."
    }

    Write-Host "[OK] Node.js $version"
}

Push-Location $projectRoot
try {
    Assert-NodeVersion

    if (-not (Test-Path $venvPython)) {
        [string[]]$bootstrap = @(Get-BootstrapPython)
        $bootstrapExecutable = $bootstrap[0]
        if ($bootstrap.Count -eq 1) {
            $bootstrapArguments = @()
        } else {
            $bootstrapArguments = @($bootstrap[1..($bootstrap.Count - 1)])
        }
        Assert-PythonVersion -Executable $bootstrapExecutable -PrefixArguments $bootstrapArguments

        Write-Host "[SETUP] Dang tao virtual environment .venv..."
        & $bootstrapExecutable @bootstrapArguments -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Khong the tao .venv."
        }
    }

    Assert-PythonVersion -Executable $venvPython

    $requirementsHash = (Get-FileHash $requirementsPath -Algorithm SHA256).Hash
    $installedHash = if (Test-Path $requirementsStamp) {
        (Get-Content $requirementsStamp -Raw).Trim()
    } else {
        ""
    }

    if (-not $SkipInstall -and $requirementsHash -ne $installedHash) {
        Write-Host "[SETUP] Dang cai Python dependencies..."
        & $venvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            throw "Khong the cap nhat pip."
        }

        & $venvPython -m pip install -r $requirementsPath
        if ($LASTEXITCODE -ne 0) {
            throw "Khong the cai dependencies tu requirements.txt."
        }

        Set-Content -Path $requirementsStamp -Value $requirementsHash -Encoding ASCII
    } elseif ($SkipInstall) {
        Write-Host "[SKIP] Bo qua cai dependencies theo yeu cau."
    } else {
        Write-Host "[OK] Dependencies khong thay doi."
    }

    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependencies dang bi thieu hoac xung dot. Chay lai khong dung -SkipInstall."
    }

    $envPath = Join-Path $projectRoot ".env"
    if (-not (Test-Path $envPath)) {
        Copy-Item (Join-Path $projectRoot ".env.example") $envPath
        Write-Host "[SETUP] Da tao .env tu .env.example."
    } else {
        Write-Host "[OK] Giu nguyen file .env hien tai."
    }

    if ($CheckOnly) {
        Write-Host "[OK] He thong da san sang. Khong khoi dong WebUI vi dang dung -CheckOnly."
        exit 0
    }

    $webArguments = @("web.py", "--host", $ListenHost, "--port", $Port.ToString())
    if ($AuthCode) {
        $webArguments += @("--auth-code", $AuthCode)
    }
    if (-not $NoBrowser) {
        $webArguments += "--open-browser"
    }
    if ($VerboseLogs) {
        $webArguments += "--verbose"
    }

    Write-Host "[START] WebUI: http://$ListenHost`:$Port"
    & $venvPython @webArguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
