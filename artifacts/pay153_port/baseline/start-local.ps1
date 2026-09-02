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

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$Port
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        return $task.Wait(500) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-ListeningProcessIds {
    param(
        [int]$Port
    )

    $listenerPids = New-Object 'System.Collections.Generic.List[int]'
    $connections = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    foreach ($connection in $connections) {
        if ($connection.OwningProcess -gt 0) {
            $null = $listenerPids.Add([int]$connection.OwningProcess)
        }
    }

    if ($listenerPids.Count -eq 0) {
        $netstatLines = @(netstat -ano -p tcp | Select-String -Pattern 'LISTENING')
        foreach ($line in $netstatLines) {
            $columns = $line.ToString().Trim() -split '\s+'
            if ($columns.Count -ge 5 -and
                $columns[0] -eq 'TCP' -and
                $columns[1] -match ":$Port$" -and
                $columns[3] -eq 'LISTENING' -and
                $columns[4] -match '^\d+$') {
                $null = $listenerPids.Add([int]$columns[4])
            }
        }
    }

    return @($listenerPids | Sort-Object -Unique)
}

function Stop-ListenersOnPort {
    param(
        [string]$HostName,
        [int]$Port
    )

    $listenerPids = @(Get-ListeningProcessIds -Port $Port)
    if ($listenerPids.Count -eq 0) {
        if (Test-TcpPort -HostName $HostName -Port $Port) {
            throw "Khong the xac dinh process dang giu cong $HostName`:$Port."
        }

        Write-Host "[PORT] Cong $HostName`:$Port dang trong."
        return
    }

    foreach ($listenerPid in $listenerPids) {
        if ($listenerPid -le 0 -or $listenerPid -eq $PID) {
            continue
        }

        $process = Get-Process -Id $listenerPid -ErrorAction SilentlyContinue
        if ($process) {
            Write-Host "[PORT] Force-close PID $listenerPid ($($process.ProcessName)) tren cong $Port..."
            Stop-Process -Id $listenerPid -Force -ErrorAction Stop
        }
    }

    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (-not (Test-TcpPort -HostName $HostName -Port $Port)) {
            Write-Host "[OK] Da giai phong cong $HostName`:$Port."
            return
        }

        Start-Sleep -Milliseconds 250
    }

    throw "Khong the giai phong cong $HostName`:$Port sau khi force-close process."
}

function Get-FileSha256 {
    param(
        [string]$Path
    )

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        return ([System.BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace("-", "")
    } finally {
        $sha256.Dispose()
    }
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

    $requirementsHash = Get-FileSha256 $requirementsPath
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

    Stop-ListenersOnPort -HostName $ListenHost -Port $Port

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
