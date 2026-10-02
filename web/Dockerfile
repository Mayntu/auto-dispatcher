# Frontend: the React app built for live mode, served by nginx, which also proxies /api and /ws to the backend.
FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Vite bakes VITE_* into the bundle at build time; logins come from build args (see docker-compose.yml / .env)
ARG VITE_DISPATCHER_LOGIN=dispatcher
ARG VITE_DISPATCHER_PASSWORD=change-me
ARG VITE_ADMIN_LOGIN=admin
ARG VITE_ADMIN_PASSWORD=change-me-too
ARG VITE_INSTRUCTOR_LOGIN=instructor
ARG VITE_INSTRUCTOR_PASSWORD=change-me-three
ARG VITE_SIM_EPOCH=07:55
ENV VITE_DATA_SOURCE=live VITE_API_BASE=/api VITE_WS_PATH=/ws \
    VITE_DISPATCHER_LOGIN=$VITE_DISPATCHER_LOGIN VITE_DISPATCHER_PASSWORD=$VITE_DISPATCHER_PASSWORD \
    VITE_ADMIN_LOGIN=$VITE_ADMIN_LOGIN VITE_ADMIN_PASSWORD=$VITE_ADMIN_PASSWORD \
    VITE_INSTRUCTOR_LOGIN=$VITE_INSTRUCTOR_LOGIN VITE_INSTRUCTOR_PASSWORD=$VITE_INSTRUCTOR_PASSWORD \
    VITE_SIM_EPOCH=$VITE_SIM_EPOCH
RUN rm -f .env && npm run build

FROM nginx:1.27-alpine
COPY --from=build /web/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
