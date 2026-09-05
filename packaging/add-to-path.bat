@echo off
rem 把本目录加入当前用户 PATH (仅用户级, 不动系统 PATH, 不截断)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = Split-Path -Parent '%~f0'; $p = [Environment]::GetEnvironmentVariable('Path','User'); if (-not $p) { $p = '' }; if (((';' + $p + ';') -like ('*;' + $d + ';*'))) { Write-Host ('已在用户 PATH 中: ' + $d) } else { [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';' + $d), 'User'); Write-Host ('已加入用户 PATH: ' + $d) }; Write-Host '重新打开 PowerShell / cmd 后即可使用 xsf 命令'"
pause
