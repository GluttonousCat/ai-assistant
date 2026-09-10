$ErrorActionPreference = "Stop"
$venvPy = "C:\Users\GluttonousCat\PycharmProjects\ai-assistant\.venv\Scripts\python.exe"
$proj = "C:\Users\GluttonousCat\PycharmProjects\ai-assistant"

# 以"当前用户登录时启动"注册计划任务, 隐藏窗口运行后端
$action = New-ScheduledTaskAction -Execute $venvPy `
  -Argument "-m uvicorn app:app --host 0.0.0.0 --port 8208" `
  -WorkingDirectory $proj
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "AIAssistantServer" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output "OK: AIAssistantServer 已注册 (用户 $env:USERNAME 登录时自动启动)"
