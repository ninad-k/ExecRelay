package dxtrade

import (
	"testing"
)

func TestParseInstanceConfigs_empty(t *testing.T) {
	got, err := ParseInstanceConfigs("")
	if err != nil || got != nil {
		t.Fatalf("expected nil, nil; got %v, %v", got, err)
	}
}

func TestParseInstanceConfigs_whitespaceOnly(t *testing.T) {
	got, err := ParseInstanceConfigs("  \t  ")
	if err != nil || got != nil {
		t.Fatalf("expected nil, nil; got %v, %v", got, err)
	}
}

func TestParseInstanceConfigs_single(t *testing.T) {
	raw := "inst-a:demo.dxtrade.com:user1:pass1:ACC001"
	got, err := ParseInstanceConfigs(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected 1 instance, got %d", len(got))
	}
	inst := got[0]
	if inst.InstanceID != "inst-a" || inst.Host != "demo.dxtrade.com" ||
		inst.Username != "user1" || inst.Password != "pass1" || inst.Account != "ACC001" {
		t.Fatalf("unexpected instance: %+v", inst)
	}
}

func TestParseInstanceConfigs_multiple(t *testing.T) {
	raw := "inst-a:host1:u1:p1:ACC1;inst-b:host2:u2:p2:ACC2"
	got, err := ParseInstanceConfigs(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("expected 2 instances, got %d", len(got))
	}
	if got[0].InstanceID != "inst-a" || got[1].InstanceID != "inst-b" {
		t.Fatalf("wrong instance IDs: %v %v", got[0].InstanceID, got[1].InstanceID)
	}
}

func TestParseInstanceConfigs_trailingSemicolon(t *testing.T) {
	raw := "inst-a:host:u:p:ACC1;"
	got, err := ParseInstanceConfigs(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected 1 instance, got %d", len(got))
	}
}

func TestParseInstanceConfigs_wrongFieldCount(t *testing.T) {
	_, err := ParseInstanceConfigs("inst-a:host:user:pass") // only 4 fields
	if err == nil {
		t.Fatal("expected error for wrong field count")
	}
}

func TestParseInstanceConfigs_missingInstanceID(t *testing.T) {
	_, err := ParseInstanceConfigs(":host:user:pass:ACC")
	if err == nil {
		t.Fatal("expected error for empty instance_id")
	}
}

func TestParseInstanceConfigs_missingHost(t *testing.T) {
	_, err := ParseInstanceConfigs("inst-a::user:pass:ACC")
	if err == nil {
		t.Fatal("expected error for empty host")
	}
}

func TestParseInstanceConfigs_whitespaceInFields(t *testing.T) {
	raw := " inst-a : host.com : user : pass : ACC "
	got, err := ParseInstanceConfigs(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got[0].InstanceID != "inst-a" || got[0].Host != "host.com" {
		t.Fatalf("whitespace not trimmed: %+v", got[0])
	}
}
