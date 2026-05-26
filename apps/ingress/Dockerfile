# syntax=docker/dockerfile:1.7
FROM --platform=$BUILDPLATFORM golang:1.22.12-alpine3.21@sha256:1699c10032ca2582ec89a24a1312d986a3f094aed3d5c1147b19880afe40e052 AS build

ARG TARGETOS
ARG TARGETARCH

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY internal ./internal
COPY packages/parser-go ./packages/parser-go
COPY packages/proto ./packages/proto
COPY apps/ingress ./apps/ingress
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /out/ingress ./apps/ingress/cmd/ingress

FROM scratch
LABEL org.opencontainers.image.title="ExecRelay Ingress" \
      org.opencontainers.image.description="ExecRelay TradingView webhook ingress service" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ninadk/execrelay"
USER 65532:65532
COPY --from=build /out/ingress /ingress
EXPOSE 8080
ENV HTTP_ADDR=:8080
HEALTHCHECK --interval=10s --timeout=2s --retries=3 CMD ["/ingress", "-healthcheck"]
ENTRYPOINT ["/ingress"]
