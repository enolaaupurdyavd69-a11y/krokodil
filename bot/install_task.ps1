# Регистрирует задачу «KrokodilMafiaBot» в Планировщике заданий Windows.
# Бот — постоянный фоновый процесс: задача запускает его при входе в Windows и раз в
# 15 минут проверяет, что он жив (второй экземпляр сразу выходит — мьютекс в коде).
# С задачей Печкина (MailEventsAgent) не связана. Запускать из PowerShell в этой папке.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $ScriptDir "token.txt"))) {
    Write-Host "Нет token.txt рядом со скриптом — положите туда токен бота и запустите снова."
    exit 1
}
# pythonw — без консольного окна
$PythonExe = Join-Path (Split-Path -Parent (Get-Command python).Source) "pythonw.exe"
$ScriptPath = Join-Path $ScriptDir "mafia_bot.py"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$ScriptPath`"" -WorkingDirectory $ScriptDir
$Triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650))
)
# ExecutionTimeLimit 0 — иначе Windows через 72 часа убивает процесс
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "KrokodilMafiaBot" `
    -Action $Action -Trigger $Triggers -Settings $Settings `
    -Description "Мафия 18+ для Telegram-групп (@krokodil_party_18_bot), постоянный процесс" `
    -Force | Out-Null

Write-Host "Готово. Задача 'KrokodilMafiaBot' зарегистрирована."
