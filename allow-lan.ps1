$ErrorActionPreference = 'Stop'
$taskRuleName = 'LingxiWorkspace-LAN-TCP-5500'
$taskRule = Get-NetFirewallRule -Name $taskRuleName -ErrorAction SilentlyContinue
if ($taskRule) {
    Set-NetFirewallRule -Name $taskRuleName -Enabled True -Direction Inbound -Action Allow -Profile Any -RemoteAddress LocalSubnet -Protocol TCP -LocalPort 5500 | Out-Null
} else {
    New-NetFirewallRule -Name $taskRuleName -DisplayName 'Lingxi Workspace LAN TCP 5500' -Enabled True -Direction Inbound -Action Allow -Profile Any -RemoteAddress LocalSubnet -Protocol TCP -LocalPort 5500 | Out-Null
}
Write-Output 'LAN inbound TCP 5500 is enabled.'
