package bootstrap

import "testing"

func TestNormalizeAddr(t *testing.T) {
	got := normalizeAddr(":8080")
	if got != "127.0.0.1:8080" {
		t.Fatalf("normalizeAddr() = %q", got)
	}
}
