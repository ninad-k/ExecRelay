package ingress

import (
	"context"
	"crypto/hmac"
	"errors"
	"sync"
)

var (
	ErrLicenseNotFound = errors.New("license not found")
	ErrLicenseInactive = errors.New("license inactive")
)

type LicenseRecord struct {
	LicenseID         string
	Secret            string
	HMACSecret        string
	InstanceID        string
	Platform          string // mt4, mt5, dxtrade; defaults to "mt5" if empty
	Active            bool
	PendingHMACSecret string
	MaxSignalsPerDay  int // 0 = unlimited
}

type LicenseStore interface {
	Lookup(ctx context.Context, licenseID string) (LicenseRecord, error)
}

// StaticLicenseStore is an immutable store used in tests and simple deployments.
type StaticLicenseStore struct {
	records map[string]LicenseRecord
}

func NewStaticLicenseStore(records []LicenseRecord) *StaticLicenseStore {
	store := &StaticLicenseStore{records: make(map[string]LicenseRecord, len(records))}
	for _, record := range records {
		store.records[record.LicenseID] = record
	}
	return store
}

func (s *StaticLicenseStore) Lookup(_ context.Context, licenseID string) (LicenseRecord, error) {
	record, ok := s.records[licenseID]
	if !ok {
		return LicenseRecord{}, ErrLicenseNotFound
	}
	if !record.Active {
		return LicenseRecord{}, ErrLicenseInactive
	}
	return record, nil
}

// HotReloadLicenseStore supports atomic in-place updates via Reload — no restart needed.
type HotReloadLicenseStore struct {
	mu      sync.RWMutex
	records map[string]LicenseRecord
}

func NewHotReloadLicenseStore(records []LicenseRecord) *HotReloadLicenseStore {
	s := &HotReloadLicenseStore{}
	s.Reload(records)
	return s
}

func (s *HotReloadLicenseStore) Reload(records []LicenseRecord) {
	m := make(map[string]LicenseRecord, len(records))
	for _, r := range records {
		m[r.LicenseID] = r
	}
	s.mu.Lock()
	s.records = m
	s.mu.Unlock()
}

func (s *HotReloadLicenseStore) Lookup(_ context.Context, licenseID string) (LicenseRecord, error) {
	s.mu.RLock()
	record, ok := s.records[licenseID]
	s.mu.RUnlock()
	if !ok {
		return LicenseRecord{}, ErrLicenseNotFound
	}
	if !record.Active {
		return LicenseRecord{}, ErrLicenseInactive
	}
	return record, nil
}

func constantStringEqual(got, want string) bool {
	return hmac.Equal([]byte(got), []byte(want))
}
