package bridge

import (
	"os"
	"time"
)

const (
	defaultHTTPAddr   = ":8080"
	defaultNATSURL    = "nats://nats:4222"
	defaultRegion     = "local"
	defaultStreamName = "SIGNALS"
	defaultConsumer   = "bridge"
)

type Config struct {
	HTTPAddr     string
	NATSURL      string
	Region       string
	StreamName   string
	ConsumerName string
	ReadTimeout  time.Duration
	AuthToken    string
}

func ConfigFromEnv() Config {
	return Config{
		HTTPAddr:     getenv("HTTP_ADDR", defaultHTTPAddr),
		NATSURL:      getenv("NATS_URL", defaultNATSURL),
		Region:       getenv("BRIDGE_REGION", defaultRegion),
		StreamName:   getenv("SIGNALS_STREAM", defaultStreamName),
		ConsumerName: getenv("SIGNALS_CONSUMER", defaultConsumer),
		ReadTimeout:  2 * time.Second,
		AuthToken:    os.Getenv("BRIDGE_AUTH_TOKEN"),
	}
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
