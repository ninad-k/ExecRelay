# syntax=docker/dockerfile:1.7
FROM node:22-alpine@sha256:968df39aedcea65eeb078fb336ed7191baf48f972b4479711397108be0966920 AS deps
WORKDIR /app
COPY apps/portal-web/package.json ./
RUN npm install

FROM node:22-alpine@sha256:968df39aedcea65eeb078fb336ed7191baf48f972b4479711397108be0966920 AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY apps/portal-web/ ./
RUN npm run build

FROM node:22-alpine@sha256:968df39aedcea65eeb078fb336ed7191baf48f972b4479711397108be0966920 AS runtime
LABEL org.opencontainers.image.title="ExecRelay Portal Web" \
      org.opencontainers.image.description="ExecRelay portal web frontend" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ninadk/execrelay"
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
HEALTHCHECK --interval=10s --timeout=2s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/health >/dev/null || exit 1
CMD ["node", "server.js"]
