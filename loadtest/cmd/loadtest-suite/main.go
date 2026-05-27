package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/ninadk/execrelay/loadtest/pkg/scenarios"
)

func main() {
	target := flag.String("target", "http://localhost:8081/webhook", "ingress webhook URL")
	license := flag.String("license", "60123456789", "license ID")
	hmacSecret := flag.String("hmac-secret", "hmac-secret", "HMAC signing secret")
	alertSecret := flag.String("alert-secret", "alert-secret", "alert body secret")
	outfile := flag.String("output", "loadtest-results.txt", "output file for results")
	flag.Parse()

	f, err := os.Create(*outfile)
	if err != nil {
		log.Fatalf("failed to create output file: %v", err)
	}
	defer f.Close()

	rates := []int{10, 50, 100, 500}
	duration := 1 * time.Minute

	fmt.Fprintf(f, "=== ExecRelay Load Test Suite ===\n")
	fmt.Fprintf(f, "Target: %s\n", *target)
	fmt.Fprintf(f, "Duration: %s\n", duration)
	fmt.Fprintf(f, "Rates tested: %v req/s\n\n", rates)

	for _, rate := range rates {
		fmt.Printf("Running webhook throughput test at %d req/s...\n", rate)
		fmt.Fprintf(f, "--- Webhook Throughput Test: %d req/s ---\n", rate)

		config := scenarios.WebhookConfig{
			Target:      *target,
			License:     *license,
			HMACSecret:  *hmacSecret,
			AlertSecret: *alertSecret,
			Rate:        rate,
			Duration:    duration,
			Workers:     10,
			Symbol:      "EURUSD",
			Command:     "buy",
		}

		result := scenarios.RunWebhookScenario(config)
		fmt.Fprintf(f, "%s\n", result.String())
		fmt.Fprintf(f, "PASS: %.1f%% success rate, p99=%.2fms\n\n",
			result.SuccessRate(), result.P99)

		if result.SuccessRate() < 99.0 {
			fmt.Fprintf(f, "WARNING: Success rate below 99%%\n\n")
		}
		if result.P99 > 100.0 {
			fmt.Fprintf(f, "WARNING: p99 latency exceeds 100ms\n\n")
		}
	}

	fmt.Printf("Test suite complete. Results written to %s\n", *outfile)
}
