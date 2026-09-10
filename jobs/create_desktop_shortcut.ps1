
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = $ws.CreateShortcut("$desktop\Alpha Finance Radar.lnk")
$lnk.TargetPath = "https://app.alpharadar.link"
$lnk.IconLocation = "C:\Program Files\Internet Explorer\iexplore.exe, 0"
$lnk.Description = "Alpha Finance Radar - 智能投研平台"
$lnk.Save()
Write-Output "OK: $desktop\Alpha Finance Radar.lnk"
