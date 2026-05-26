package ingress

import (
	"fmt"
	"sync"
	"time"
)

type dailyCounter struct {
	mu   sync.Mutex
	hits map[string]int // key: "licenseID:YYYY-MM-DD"
}

func newDailyCounter() *dailyCounter {
	dc := &dailyCounter{hits: make(map[string]int)}
	go dc.cleanupLoop()
	return dc
}

func (dc *dailyCounter) Increment(licenseID string, now time.Time) int {
	key := fmt.Sprintf("%s:%s", licenseID, now.UTC().Format("2006-01-02"))
	dc.mu.Lock()
	dc.hits[key]++
	v := dc.hits[key]
	dc.mu.Unlock()
	return v
}

func (dc *dailyCounter) cleanupLoop() {
	ticker := time.NewTicker(1 * time.Hour)
	defer ticker.Stop()
	for range ticker.C {
		today := time.Now().UTC().Format("2006-01-02")
		dc.mu.Lock()
		for k := range dc.hits {
			if len(k) >= 10 && k[len(k)-10:] != today {
				delete(dc.hits, k)
			}
		}
		dc.mu.Unlock()
	}
}
