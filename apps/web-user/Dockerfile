FROM node:22-bookworm-slim AS builder
WORKDIR /src
RUN npm install --global pnpm@10.33.2
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web-user/package.json ./apps/web-user/
COPY apps/web-admin/package.json ./apps/web-admin/
COPY packages/shared/package.json ./packages/shared/
RUN pnpm install --frozen-lockfile
COPY apps ./apps
COPY packages ./packages
ENV VITE_APP_BASE=/ VITE_ADMIN_URL=/admin/
RUN pnpm --filter @cyshuhe/web-user build
FROM nginx:1.28-alpine
COPY deploy/static.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /src/apps/web-user/dist /usr/share/nginx/html
