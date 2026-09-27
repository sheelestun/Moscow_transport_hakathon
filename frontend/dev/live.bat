@echo off
chcp 65001 >nul
rem Дашборд из папки frontend на живых данных с сайта (api.mowtransit.ru).
rem Правите файлы -> обновляете страницу (Ctrl+Shift+R). Остановить: Ctrl+C.
echo.
echo   Откройте в браузере:
echo   http://localhost:3001/?mode=live^&api=/api^&ws=ws://localhost:3001/api/ws
echo.
docker run --rm --name dashboard-live -p 3001:80 -v "%~dp0..:/usr/share/nginx/html:ro" -v "%~dp0nginx-live.conf:/etc/nginx/conf.d/default.conf:ro" nginx:1.27-alpine
