# install-autostart.ps1 — start crew-forge automatically at logon (Windows).
#
# After a reboot the OS must start ONE thing: crew-forge. When it comes up it reconciles the
# fleet (launches any stopped crews, skips the running ones), so the whole fleet returns on its
# own. This registers a Scheduled Task that runs crew-forge under the watchdog at logon.
#
# Usage:   ./scripts/install-autostart.ps1
#          ./scripts/install-autostart.ps1 -Crew crews/crew_forge_crew.py -TaskName crewaimeat-forge
# Remove:  Unregister-ScheduledTask -TaskName crewaimeat-forge -Confirm:$false

param(
    [string]$Crew = "crews/crew_forge_crew.py",
    [string]$TaskName = "crewaimeat-forge"
)

$root = (Resolve-Path "$PSScriptRoot\..").Path
$watchdog = Join-Path $root "scripts\watchdog.ps1"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watchdog`" $Crew" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' to start $Crew at logon."
Write-Host "On boot, crew-forge starts and reconciles the fleet (brings up the other crews)."
Write-Host "Remove with:  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
