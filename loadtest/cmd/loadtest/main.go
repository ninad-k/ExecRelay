// loadtest sends signed webhook requests to the ExecRelay ingress and
// reports latency percentiles. Designed to run against a live stack.
//
// Usage:
//
//	go run ./loadtest/cmd/loadtest \
//	  -target http://localhost:8081/webhook \
//	  -license 60123456789 \
//	  -hmac-secret hmac-secret \
//	  -alert-secret alert-secret \
//	  -rate 100 \
//	  -duration 30s \
//	  -workers 10
package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

func main() {
	target := flag.String("target", "http://localhost:8081/webhook", "ingress webhook URL")
	license := flag.String("license", "60123456789", "license ID")
	hmacSecret := flag.String("hmac-secret", "hmac-secret", "HMAC signing secret")
	alertSecret := flag.String("alert-secret", "alert-secret", "alert body secret")
	rate := flag.Int("rate", 50, "requests per second")
	duration := flag.Duration("duration", 30*time.Second, "test duration")
	workers := flag.Int("workers", 10, "concurrent workers")
	flag.Parse()

	body := fmt.Sprintf("%s,buy,EURUSD,vol_lots=0.1,sl_pips=20,tp_pips=40,secret=%s",
		*license, *alertSecret)
	sig := sign(body, *hmacSecret)

	interval := time.Second / time.Duration(*rate)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	deadline := time.Now().Add(*duration)

	var (
		sent      int64
		success   int64
		failed    int64
		mu        sync.Mutex
		latencies []float64
	)

	client := &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			MaxIdleConnsPerHost: *workers,
		},
	}

	sem := make(chan struct{}, *workers)
	var wg sync.WaitGroup

	log.Printf("loadtest: target=%s rate=%d/s duration=%s workers=%d",
		*target, *rate, *duration, *workers)

	for range ticker.C {
		if time.Now().After(deadline) {
			break
		}
		atomic.AddInt64(&sent, 1)
		sem <- struct{}{}
		wg.Add(1)
		go func() {
			defer func() { <-sem; wg.Done() }()
			start := time.Now()
			req, err := http.NewRequest(http.MethodPost, *target, bytes.NewBufferString(body))
			if err != nil {
				atomic.AddInt64(&failed, 1)
				return
			}
			req.Header.Set("X-ExecRelay-Signature", sig)
			resp, err := client.Do(req)
			elapsed := time.Since(start).Seconds() * 1000 // ms
			if err != nil || resp.StatusCode != http.StatusOK {
				atomic.AddInt64(&failed, 1)
				if err == nil {
					resp.Body.Close()
				}
				return
			}
			resp.Body.Close()
			atomic.AddInt64(&success, 1)
			mu.Lock()
			latencies = append(latencies, elapsed)
			mu.Unlock()
		}()
	}

	wg.Wait()

	total := atomic.LoadInt64(&sent)
	ok := atomic.LoadInt64(&success)
	bad := atomic.LoadInt64(&failed)

	fmt.Printf("\n=== loadtest results ===\n")
	fmt.Printf("sent:    %d\n", total)
	fmt.Printf("success: %d (%.1f%%)\n", ok, float64(ok)/float64(total)*100)
	fmt.Printf("failed:  %d\n", bad)

	if len(latencies) == 0 {
		fmt.Println("no successful responses to measure latency")
		os.Exit(1)
	}

	sort.Float64s(latencies)
	fmt.Printf("latency (ms):\n")
	fmt.Printf("  p50:  %.2f\n", percentile(latencies, 0.50))
	fmt.Printf("  p95:  %.2f\n", percentile(latencies, 0.95))
	fmt.Printf("  p99:  %.2f\n", percentile(latencies, 0.99))
	fmt.Printf("  min:  %.2f\n", latencies[0])
	fmt.Printf("  max:  %.2f\n", latencies[len(latencies)-1])

	// Target: p99 ≤ 95ms for the ingress hot path.
	p99 := percentile(latencies, 0.99)
	if p99 > 95.0 {
		fmt.Printf("\nWARN: p99 %.2fms exceeds 95ms target\n", p99)
		os.Exit(2)
	}
	fmt.Printf("\nPASS: p99 %.2fms within 95ms target\n", p99)
}

func sign(body, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(body))
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	idx := p * float64(len(sorted)-1)
	lo := int(math.Floor(idx))
	hi := int(math.Ceil(idx))
	if lo == hi {
		return sorted[lo]
	}
	frac := idx - float64(lo)
	return sorted[lo]*(1-frac) + sorted[hi]*frac
}
