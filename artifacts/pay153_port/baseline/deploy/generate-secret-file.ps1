param(
    [Parameter(Mandatory = $true)]
    [string]$SourceEnv,
    [Parameter(Mandatory = $true)]
    [string]$DestinationEnv,
    [Parameter(Mandatory = $true)]
    [string]$WebUiAuthCode
)

$ErrorActionPreference = "Stop"

function Set-EnvLine {
    param(
        [string]$Content,
        [string]$Key,
        [string]$Value
    )

    $escapedKey = [regex]::Escape($Key)
    $escapedValue = ([string]$Value).Replace('\', '\\').Replace('"', '\"')
    $replacement = $Key + '="' + $escapedValue + '"'
    $pattern = '(?m)^\s*(?:export\s+)?' + $escapedKey + '\s*=.*$'
    $evaluator = [Text.RegularExpressions.MatchEvaluator]{ param($match) $replacement }
    if ([Text.RegularExpressions.Regex]::IsMatch($Content, $pattern)) {
        return [Text.RegularExpressions.Regex]::Replace($Content, $pattern, $evaluator)
    }
    return $Content.TrimEnd() + [Environment]::NewLine + $replacement + [Environment]::NewLine
}

$sourcePath = [IO.Path]::GetFullPath($SourceEnv)
$destinationPath = [IO.Path]::GetFullPath($DestinationEnv)
if (-not [IO.File]::Exists($sourcePath)) {
    throw "Source env file does not exist"
}

$content = [IO.File]::ReadAllText($sourcePath)
$updates = [ordered]@{
    WEBUI_AUTH_CODE = $WebUiAuthCode
    WEBUI_SECURE_COOKIE = "True"
    CLOAK_HEADLESS = "True"
    CLOAK_USER_DATA_DIR = ""
    NORDVPN_ENABLED = "False"
    NORDVPN_AUTO_ROTATE_ENABLED = "False"
    NORDVPN_ACCESS_TOKEN = ""
    NORDVPN_WG_ENABLED = "False"
    NORDVPN_WG_CONFIGS_DIR = "/var/lib/turb/nordvpn-wireguard"
    NORDVPN_WG_WIREPROXY_EXE = "wireproxy"
    NORDVPN_WG_AUTO_DOWNLOAD = "False"
}
foreach ($entry in $updates.GetEnumerator()) {
    $content = Set-EnvLine -Content $content -Key $entry.Key -Value $entry.Value
}

$parent = [IO.Path]::GetDirectoryName($destinationPath)
[IO.Directory]::CreateDirectory($parent) | Out-Null
[IO.File]::WriteAllText($destinationPath, $content, [Text.UTF8Encoding]::new($false))
