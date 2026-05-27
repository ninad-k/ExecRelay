package scenarios

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"net/http"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

type WebhookConfig struct {
	Target      string
	License     string
	HMACSecret  string
	AlertSecret string
	Rate        int // req/s
	Duration    time.Duration
	Workers     int
	Symbol      string
	Command     string
}

type Result struct {
	Total         int64
	Success       int64
	Failed        int64
	Latencies     []float64
	ErrorRates    map[int]int64
	P50, P95, P99 float64
	MinLatency    float64
	MaxLatency    float64
}

func RunWebhookScenario(config WebhookConfig) *Result {
	body := fmt.Sprintf("%s,%s,%s,vol_lots=0.1,sl_pips=20,tp_pips=40,secret=%s",
		config.License, config.Command, config.Symbol, config.AlertSecret)
	sig := sign(body, config.HMACSecret)

	interval := time.Second / time.Duration(config.Rate)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	deadline := time.Now().Add(config.Duration)

	var (
		sent       int64
		success    int64
		failed     int64
		mu         sync.Mutex
		latencies  []float64
		errorRates = make(map[int]int64)
	)

	client := &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			MaxIdleConnsPerHost: config.Workers,
		},
	}

	sem := make(chan struct{}, config.Workers)
	var wg sync.WaitGroup

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
			req, err := http.NewRequest(http.MethodPost, config.Target, bytes.NewBufferString(body))
			if err != nil {
				atomic.AddInt64(&failed, 1)
				return
			}
			req.Header.Set("X-ExecRelay-Signature", sig)
			resp, err := client.Do(req)
			elapsed := time.Since(start).Seconds() * 1000 // ms

			if err != nil || resp.StatusCode != http.StatusOK {
				atomic.AddInt64(&failed, 1)
				if resp != nil {
					mu.Lock()
					errorRates[resp.StatusCode]++
					mu.Unlock()
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

	result := &Result{
		Total:      total,
		Success:    ok,
		Failed:     bad,
		Latencies:  latencies,
		ErrorRates: errorRates,
	}

	if len(latencies) > 0 {
		sort.Float64s(latencies)
		result.P50 = percentile(latencies, 0.50)
		result.P95 = percentile(latencies, 0.95)
		result.P99 = percentile(latencies, 0.99)
		result.MinLatency = latencies[0]
		result.MaxLatency = latencies[len(latencies)-1]
	}

	return result
}

func (r *Result) SuccessRate() float64 {
	if r.Total == 0 {
		return 0
	}
	return float64(r.Success) / float64(r.Total) * 100
}

func (r *Result) String() string {
	output := fmt.Sprintf("Total: %d | Success: %d (%.1f%%) | Failed: %d\n",
		r.Total, r.Success, r.SuccessRate(), r.Failed)

	if len(r.Latencies) > 0 {
		output += fmt.Sprintf("Latency (ms): p50=%.2f p95=%.2f p99=%.2f min=%.2f max=%.2f\n",
			r.P50, r.P95, r.P99, r.MinLatency, r.MaxLatency)
	}

	if len(r.ErrorRates) > 0 {
		output += "Error codes: "
		for code, count := range r.ErrorRates {
			output += fmt.Sprintf("%d: %d ", code, count)
		}
		output += "\n"
	}

	return output
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
