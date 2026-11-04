@echo off
rem Запуск локального агента из папки проекта; лог пишется в agent.log.
cd /d "%~dp0"
py -3 -m agent >> agent.log 2>&1
