# syntax=docker/dockerfile:1.7
FROM --platform=$BUILDPLATFORM golang:1.25-alpine3.21 AS build

ARG TARGETOS
ARG TARGETARCH

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY internal ./internal
COPY packages/proto ./packages/proto
COPY apps/dxtrade ./apps/dxtrade
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /out/dxtrade ./apps/dxtrade/cmd/dxtrade

FROM scratch
LABEL org.opencontainers.image.title="ExecRelay DXtrade Adapter" \
      org.opencontainers.image.description="ExecRelay DXtrade execution adapter service" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ninadk/execrelay"
USER 65532:65532
COPY --from=build /out/dxtrade /dxtrade
EXPOSE 8080
ENV HTTP_ADDR=:8080
HEALTHCHECK --interval=10s --timeout=2s --retries=3 CMD ["/dxtrade", "-healthcheck"]
ENTRYPOINT ["/dxtrade"]
