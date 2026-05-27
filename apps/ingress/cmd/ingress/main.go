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

	"github.com/ninadk/execrelay/apps/ingress/internal/ingress"
)

var logger *slog.Logger

func main() {
	healthcheck := flag.Bool("healthcheck", false, "run a local health probe")
	flag.Parse()

	cfg, err := ingress.ConfigFromEnv()
	if err != nil {
		slog.Error("config", "err", err)
		os.Exit(1)
	}

	logLevel := slog.LevelInfo
	if cfg.Debug {
		logLevel = slog.LevelDebug
	}
	logger = slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: logLevel}))
	slog.SetDefault(logger)

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

	publisher, err := ingress.NewNatsPublisher(cfg.NATSURL)
	if err != nil {
		slog.Error("nats connect", "err", err)
		os.Exit(1)
	}

	licenseStore := ingress.NewHotReloadLicenseStore(cfg.Licenses)
	logLicenseAudit(cfg.Licenses)

	handler := ingress.NewHandler(ingress.Options{
		Store:           licenseStore,
		Publisher:       publisher,
		EventPublisher:  publisher,
		Region:          cfg.Region,
		MaxBodyBytes:    cfg.MaxBodyBytes,
		TimestampWindow: cfg.TimestampWindow,
		RateLimit:       cfg.RateLimit,
		AllowedCIDRs:    cfg.AllowedCIDRs,
		PerimeterToken:  cfg.PerimeterToken,
		Debug:           cfg.Debug,
	})
	if cfg.PerimeterToken == "" {
		slog.Warn("INGRESS_PERIMETER_TOKEN unset; perimeter gate disabled (per-license auth still applies)")
	} else {
		slog.Info("perimeter token gate enabled")
	}

	if cfg.Debug {
		slog.Info("debug logging enabled")
	}

	server := ingress.NewServer(cfg, handler.Routes())

	errs := make(chan error, 1)
	go func() {
		slog.Info("ingress listening", "addr", cfg.HTTPAddr)
		errs <- server.ListenAndServe()
	}()

	stop := make(chan os.Signal, 1)
	reload := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	signal.Notify(reload, syscall.SIGHUP)

	for {
		select {
		case <-reload:
			updated, err := ingress.LoadLicenses()
			if err != nil {
				slog.Warn("license reload failed", "err", err)
				continue
			}
			licenseStore.Reload(updated)
			logLicenseAudit(updated)
			slog.Info("licenses reloaded", "count", len(updated))

		case sig := <-stop:
			slog.Info("shutting down", "signal", sig)
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := ingress.Shutdown(ctx, publisher); err != nil {
				slog.Warn("publisher shutdown", "err", err)
			}
			if err := server.Shutdown(ctx); err != nil {
				slog.Error("server shutdown", "err", err)
				os.Exit(1)
			}
			return

		case err := <-errs:
			if err != nil && err != http.ErrServerClosed {
				slog.Error("server error", "err", err)
				os.Exit(1)
			}
			return
		}
	}
}

// logLicenseAudit emits a slog warning per license configuration issue and
// publishes the same warnings as Prometheus gauges. Called at startup and on
// SIGHUP-triggered license reload.
func logLicenseAudit(records []ingress.LicenseRecord) {
	warnings := ingress.AuditLicenses(records)
	ingress.ReportLicenseWarnings(warnings)
	for _, w := range warnings {
		slog.Warn("license config warning", "license", w.LicenseID, "issue", w.Issue, "detail", w.Detail)
	}
	if len(warnings) == 0 {
		slog.Info("license audit clean", "count", len(records))
	} else {
		slog.Info("license audit complete", "count", len(records), "warnings", len(warnings))
	}
}
