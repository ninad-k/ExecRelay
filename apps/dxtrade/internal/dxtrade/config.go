package dxtrade

import (
	"errors"
	"os"
	"strings"
	"time"
)

const (
	defaultHTTPAddr  = ":8080"
	defaultNATSURL   = "nats://nats:4222"
	defaultRegion    = "local"
	defaultStreamName = "SIGNALS"
	defaultConsumer  = "dxtrade"
)

type Config struct {
	HTTPAddr     string
	NATSURL      string
	Region       string
	StreamName   string
	ConsumerName string
	ReadTimeout  time.Duration
	Instances    []InstanceConfig
}

// InstanceConfig holds DXTrade credentials for a single EA instance.
type InstanceConfig struct {
	InstanceID string
	Host       string // e.g. "demo.dxtrade.com"
	Username   string
	Password   string
	Account    string // trading account code
}

func ConfigFromEnv() (Config, error) {
	cfg := Config{
		HTTPAddr:     getenv("HTTP_ADDR", defaultHTTPAddr),
		NATSURL:      getenv("NATS_URL", defaultNATSURL),
		Region:       getenv("DXTRADE_REGION", defaultRegion),
		StreamName:   getenv("SIGNALS_STREAM", defaultStreamName),
		ConsumerName: getenv("SIGNALS_CONSUMER", defaultConsumer),
		ReadTimeout:  2 * time.Second,
	}

	instances, err := ParseInstanceConfigs(os.Getenv("DXTRADE_INSTANCES"))
	if err != nil {
		return Config{}, err
	}
	cfg.Instances = instances
	return cfg, nil
}

// ParseInstanceConfigs parses "instanceID:host:username:password:account;..." entries.
func ParseInstanceConfigs(raw string) ([]InstanceConfig, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	parts := strings.Split(raw, ";")
	out := make([]InstanceConfig, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		fields := strings.SplitN(part, ":", 5)
		if len(fields) != 5 {
			return nil, errors.New("DXTRADE_INSTANCES entries must be instanceID:host:username:password:account")
		}
		inst := InstanceConfig{
			InstanceID: strings.TrimSpace(fields[0]),
			Host:       strings.TrimSpace(fields[1]),
			Username:   strings.TrimSpace(fields[2]),
			Password:   strings.TrimSpace(fields[3]),
			Account:    strings.TrimSpace(fields[4]),
		}
		if inst.InstanceID == "" || inst.Host == "" {
			return nil, errors.New("DXTRADE_INSTANCES instance_id and host are required")
		}
		out = append(out, inst)
	}
	return out, nil
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
