package ingress

import (
	"sort"
	"testing"
)

func TestAuditLicensesFlagsNoAuth(t *testing.T) {
	warnings := AuditLicenses([]LicenseRecord{{
		LicenseID:  "open-license",
		InstanceID: "mt5-a",
		Active:     true,
	}})
	if len(warnings) != 1 {
		t.Fatalf("expected 1 warning, got %d", len(warnings))
	}
	if warnings[0].Issue != "no_auth" {
		t.Fatalf("expected no_auth issue, got %q", warnings[0].Issue)
	}
	if warnings[0].LicenseID != "open-license" {
		t.Fatalf("license_id = %q", warnings[0].LicenseID)
	}
}

func TestAuditLicensesFlagsMissingHMAC(t *testing.T) {
	warnings := AuditLicenses([]LicenseRecord{{
		LicenseID:  "secret-only",
		Secret:     "shh",
		InstanceID: "mt5-a",
	}})
	if len(warnings) != 1 || warnings[0].Issue != "no_hmac" {
		t.Fatalf("expected no_hmac warning, got %#v", warnings)
	}
}

func TestAuditLicensesFlagsMissingSecret(t *testing.T) {
	warnings := AuditLicenses([]LicenseRecord{{
		LicenseID:  "hmac-only",
		HMACSecret: "k",
		InstanceID: "mt5-a",
	}})
	if len(warnings) != 1 || warnings[0].Issue != "no_secret" {
		t.Fatalf("expected no_secret warning, got %#v", warnings)
	}
}

func TestAuditLicensesFlagsRotationInProgress(t *testing.T) {
	warnings := AuditLicenses([]LicenseRecord{{
		LicenseID:         "rotating",
		Secret:            "s",
		HMACSecret:        "k1",
		PendingHMACSecret: "k2",
		InstanceID:        "mt5-a",
	}})
	if len(warnings) != 1 || warnings[0].Issue != "rotation_active" {
		t.Fatalf("expected rotation_active, got %#v", warnings)
	}
}

func TestAuditLicensesCleanWhenFullyConfigured(t *testing.T) {
	warnings := AuditLicenses([]LicenseRecord{{
		LicenseID:  "well-configured",
		Secret:     "s",
		HMACSecret: "k",
		InstanceID: "mt5-a",
	}})
	if len(warnings) != 0 {
		t.Fatalf("expected zero warnings, got %#v", warnings)
	}
}

func TestAuditLicensesMixedFleet(t *testing.T) {
	warnings := AuditLicenses([]LicenseRecord{
		{LicenseID: "a", Secret: "s", HMACSecret: "k", InstanceID: "x"},
		{LicenseID: "b", InstanceID: "y"},                       // no_auth
		{LicenseID: "c", Secret: "s", InstanceID: "z"},          // no_hmac
		{LicenseID: "d", HMACSecret: "k", InstanceID: "w"},      // no_secret
	})
	if len(warnings) != 3 {
		t.Fatalf("expected 3 warnings, got %d (%#v)", len(warnings), warnings)
	}
	gotIssues := make([]string, len(warnings))
	for i, w := range warnings {
		gotIssues[i] = w.LicenseID + ":" + w.Issue
	}
	sort.Strings(gotIssues)
	want := []string{"b:no_auth", "c:no_hmac", "d:no_secret"}
	for i := range want {
		if gotIssues[i] != want[i] {
			t.Fatalf("warnings = %v, want %v", gotIssues, want)
		}
	}
}
