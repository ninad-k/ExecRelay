package main

import (
	"context"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/ninadk/execrelay/apps/bridge/internal/bridge"
)

func main() {
	healthcheck := flag.Bool("healthcheck", false, "run a local health probe")
	flag.Parse()

	cfg := bridge.ConfigFromEnv()

	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))

	if *healthcheck {
		addr := cfg.HTTPAddr
		if len(addr) > 0 && addr[0] == ':' {
			addr = "127.0.0.1" + addr
		}
		client := &http.Client{Timeout: 1500 * time.Millisecond}
		resp, err := client.Get("http://" + addr + "/health")
		if err != nil {
			slog.Error("healthcheck failed", "err", err)
			os.Exit(1)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			slog.Error("healthcheck failed", "status", resp.StatusCode)
			os.Exit(1)
		}
		return
	}

	nc, err := nats.Connect(
		cfg.NATSURL,
		nats.Name("execrelay-bridge"),
		nats.Timeout(3*time.Second),
		nats.ReconnectWait(500*time.Millisecond),
		nats.MaxReconnects(-1),
	)
	if err != nil {
		slog.Error("nats connect", "err", err)
		os.Exit(1)
	}
	defer nc.Drain()

	js, err := nc.JetStream()
	if err != nil {
		slog.Error("jetstream", "err", err)
		os.Exit(1)
	}

	if err := bridge.EnsureStream(js, cfg.StreamName); err != nil {
		slog.Error("stream setup", "err", err)
		os.Exit(1)
	}
	if err := bridge.EnsureFillsStream(js); err != nil {
		slog.Error("fills stream setup", "err", err)
		os.Exit(1)
	}
	if err := bridge.EnsureEventsStream(js); err != nil {
		slog.Error("events stream setup", "err", err)
		os.Exit(1)
	}

	hub := bridge.NewHub()
	subs, err := bridge.NewSubscriber(js, hub, cfg.StreamName, cfg.ConsumerName).Subscribe()
	if err != nil {
		slog.Error("subscribe", "err", err)
		os.Exit(1)
	}
	for _, s := range subs {
		defer s.Drain()
	}

	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		consumers := []string{cfg.ConsumerName + "-mt5", cfg.ConsumerName + "-mt4"}
		for range ticker.C {
			for _, c := range consumers {
				info, err := js.ConsumerInfo(cfg.StreamName, c)
				if err != nil {
					continue
				}
				bridge.SetConsumerLag(c, int64(info.NumPending))
			}
		}
	}()

	if cfg.AuthToken == "" {
		slog.Warn("BRIDGE_AUTH_TOKEN not set — EA connections are unauthenticated")
	}

	server := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           bridge.NewHandlerWithAuth(hub, nc, cfg.AuthToken).Routes(),
		ReadHeaderTimeout: cfg.ReadTimeout,
	}

	errs := make(chan error, 1)
	go func() {
		slog.Info("bridge listening", "addr", cfg.HTTPAddr)
		errs <- server.ListenAndServe()
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)

	select {
	case sig := <-stop:
		slog.Info("shutting down", "signal", sig)
	case err := <-errs:
		if err != nil && err != http.ErrServerClosed {
			slog.Error("server error", "err", err)
			os.Exit(1)
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		slog.Error("server shutdown", "err", err)
		os.Exit(1)
	}
}
