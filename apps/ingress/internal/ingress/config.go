package ingress

import (
	"errors"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	defaultHTTPAddr     = ":8080"
	defaultNATSURL      = "nats://nats:4222"
	defaultRegion       = "local"
	defaultMaxBodyBytes = 4096
)

type Config struct {
	HTTPAddr        string
	NATSURL         string
	Region          string
	MaxBodyBytes    int64
	ReadTimeout     time.Duration
	WriteTimeout    time.Duration
	Licenses        []LicenseRecord
	TimestampWindow time.Duration
	RateLimit       int
	AllowedCIDRs    []*net.IPNet
	PerimeterToken  string
	TradingHalted   bool
	Debug           bool
}

func ConfigFromEnv() (Config, error) {
	cfg := Config{
		HTTPAddr:     getenv("HTTP_ADDR", defaultHTTPAddr),
		NATSURL:      getenv("NATS_URL", defaultNATSURL),
		Region:       getenv("INGRESS_REGION", defaultRegion),
		MaxBodyBytes: defaultMaxBodyBytes,
		ReadTimeout:  2 * time.Second,
		WriteTimeout: 2 * time.Second,
		Debug:        getenvBool("DEBUG", true),
	}

	if raw := os.Getenv("MAX_BODY_BYTES"); raw != "" {
		value, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || value <= 0 {
			return Config{}, errors.New("MAX_BODY_BYTES must be a positive integer")
		}
		cfg.MaxBodyBytes = value
	}

	if raw := os.Getenv("WEBHOOK_TIMESTAMP_WINDOW_SECS"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 0 {
			return Config{}, errors.New("WEBHOOK_TIMESTAMP_WINDOW_SECS must be a non-negative integer")
		}
		cfg.TimestampWindow = time.Duration(value) * time.Second
	}

	if raw := os.Getenv("WEBHOOK_RATE_LIMIT"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 0 {
			return Config{}, errors.New("WEBHOOK_RATE_LIMIT must be a non-negative integer")
		}
		cfg.RateLimit = value
	}

	if raw := os.Getenv("WEBHOOK_ALLOWED_CIDRS"); raw != "" {
		parts := strings.Split(raw, ",")
		nets := make([]*net.IPNet, 0, len(parts))
		for _, part := range parts {
			part = strings.TrimSpace(part)
			if part == "" {
				continue
			}
			_, ipNet, err := net.ParseCIDR(part)
			if err != nil {
				return Config{}, errors.New("WEBHOOK_ALLOWED_CIDRS invalid CIDR: " + part)
			}
			nets = append(nets, ipNet)
		}
		cfg.AllowedCIDRs = nets
	}

	cfg.PerimeterToken = strings.TrimSpace(os.Getenv("INGRESS_PERIMETER_TOKEN"))
	cfg.TradingHalted = getenvBool("INGRESS_TRADING_HALTED", false)

	licenses, err := LoadLicenses()
	if err != nil {
		return Config{}, err
	}
	cfg.Licenses = licenses
	return cfg, nil
}

// LoadLicenses reads license records from EXECRELAY_LICENSES_FILE (if set)
// or falls back to parsing the EXECRELAY_LICENSES env var directly.
func LoadLicenses() ([]LicenseRecord, error) {
	if path := os.Getenv("EXECRELAY_LICENSES_FILE"); path != "" {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		return ParseLicenseRecords(strings.TrimSpace(string(data)))
	}
	return ParseLicenseRecords(os.Getenv("EXECRELAY_LICENSES"))
}

func getenv(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}

func getenvBool(key string, defaultValue bool) bool {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "true", "1", "yes", "on":
		return true
	case "false", "0", "no", "off":
		return false
	default:
		return defaultValue
	}
}

func NewServer(cfg Config, handler http.Handler) *http.Server {
	return &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           handler,
		ReadHeaderTimeout: cfg.ReadTimeout,
		ReadTimeout:       cfg.ReadTimeout,
		WriteTimeout:      cfg.WriteTimeout,
	}
}

func ParseLicenseRecords(raw string) ([]LicenseRecord, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}

	parts := strings.Split(raw, ";")
	records := make([]LicenseRecord, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}

		fields := strings.Split(part, ":")
		if len(fields) < 4 || len(fields) > 7 {
			return nil, errors.New("EXECRELAY_LICENSES entries must be license:secret:hmac_secret:instance_id[:platform[:pendingHmacSecret[:maxSignalsPerDay]]]")
		}

		platform := "mt5"
		if len(fields) >= 5 {
			platform = strings.TrimSpace(fields[4])
		}
		record := LicenseRecord{
			LicenseID:  strings.TrimSpace(fields[0]),
			Secret:     strings.TrimSpace(fields[1]),
			HMACSecret: strings.TrimSpace(fields[2]),
			InstanceID: strings.TrimSpace(fields[3]),
			Platform:   platform,
			Active:     true,
		}
		if len(fields) >= 6 {
			record.PendingHMACSecret = strings.TrimSpace(fields[5])
		}
		if len(fields) == 7 {
			if v := strings.TrimSpace(fields[6]); v != "" {
				n, err := strconv.Atoi(v)
				if err != nil {
					return nil, errors.New("EXECRELAY_LICENSES maxSignalsPerDay must be an integer")
				}
				record.MaxSignalsPerDay = n
			}
		}
		if record.LicenseID == "" || record.InstanceID == "" {
			return nil, errors.New("EXECRELAY_LICENSES license and instance_id are required")
		}
		records = append(records, record)
	}
	return records, nil
}
