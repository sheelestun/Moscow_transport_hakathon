# Дашборд — статические файлы, сборка не нужна. nginx слушает 80,
# наружу пробрасывается как 3000 (см. docker-compose.yml: "3000:80").
# entrypoint.sh подставляет env-переменные (MODE/API_BASE/WS_URL) в config.js
# при старте контейнера — так один и тот же образ работает в dev, staging, prod.
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/
COPY css /usr/share/nginx/html/css
COPY js /usr/share/nginx/html/js
COPY entrypoint.sh /docker-entrypoint.d/40-inject-config.sh
# sed убирает \r: на Windows git отдаёт файл с CRLF, и /bin/sh такой скрипт не запускает
RUN sed -i 's/\r$//' /docker-entrypoint.d/40-inject-config.sh && chmod +x /docker-entrypoint.d/40-inject-config.sh
EXPOSE 80
