# Build the static bundle, then serve it from nginx. VITE_API_BASE is baked
# in at build time (Vite inlines env vars into the JS bundle) -- pass it as
# a build arg so the built assets point at wherever the API actually lives.
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_BASE=http://localhost:8000
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
