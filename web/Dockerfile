# Static frontend + reverse proxy to the API (CLAUDE.md §23).
# Build context is the repo root so we can serve web-mvp/ until the React app lands in web/.
FROM nginx:1.27-alpine

COPY web-mvp/ /usr/share/nginx/html/
COPY web/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
