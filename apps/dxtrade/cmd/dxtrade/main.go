package main

import (
	"context"
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/ninadk/execrelay/apps/dxtrade/internal/dxtrade"
	"github.com/ninadk/execrelay/internal/obs"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

func main() {
	healthcheck := flag.Bool("healthcheck", false, "run a local health probe")
	flag.Parse()

	cfg, err := dxtrade.ConfigFromEnv()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	if *healthcheck {
		addr := cfg.HTTPAddr
		if len(addr) > 0 && addr[0] == ':' {
			addr = "127.0.0.1" + addr
		}
		client := &http.Client{Timeout: 1500 * time.Millisecond}
		resp, err := client.Get("http://" + addr + "/health")
		if err != nil {
			log.Printf("healthcheck failed: %v", err)
			os.Exit(1)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			log.Printf("healthcheck failed: status %d", resp.StatusCode)
			os.Exit(1)
		}
		return
	}

	nc, err := nats.Connect(
		cfg.NATSURL,
		nats.Name("execrelay-dxtrade"),
		nats.Timeout(3*time.Second),
		nats.ReconnectWait(500*time.Millisecond),
		nats.MaxReconnects(-1),
	)
	if err != nil {
		log.Fatalf("nats: %v", err)
	}
	defer nc.Drain()

	js, err := nc.JetStream()
	if err != nil {
		log.Fatalf("jetstream: %v", err)
	}

	clients := make(map[string]*dxtrade.Client, len(cfg.Instances))
	for _, inst := range cfg.Instances {
		clients[inst.InstanceID] = dxtrade.NewClient(inst)
	}

	sub, err := dxtrade.NewSubscriber(js, clients, nc, cfg.StreamName, cfg.ConsumerName).Subscribe()
	if err != nil {
		log.Fatalf("subscribe: %v", err)
	}
	defer sub.Drain()

	mux := http.NewServeMux()
	health := func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"service": "dxtrade", "status": "ok"})
	}
	mux.HandleFunc("/health", health)
	mux.HandleFunc("/healthz", health)
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		ok := nc.IsConnected()
		if !ok {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"service": "dxtrade",
			"ok":      ok,
			"checks":  map[string]any{"nats": map[string]any{"ok": ok}},
		})
	})
	mux.Handle("/metrics", promhttp.Handler())

	server := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           obs.Middleware("dxtrade")(mux),
		ReadHeaderTimeout: cfg.ReadTimeout,
	}

	errs := make(chan error, 1)
	go func() {
		log.Printf("dxtrade listening on %s", cfg.HTTPAddr)
		errs <- server.ListenAndServe()
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)

	select {
	case sig := <-stop:
		log.Printf("received %s, shutting down", sig)
	case err := <-errs:
		if err != nil && err != http.ErrServerClosed {
			log.Fatal(err)
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		log.Fatal(err)
	}
}
