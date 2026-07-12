package ingress

import (
	"bytes"
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	oldproto "github.com/golang/protobuf/proto"
	parser "github.com/ninadk/execrelay/packages/parser-go"
	execrelaypb "github.com/ninadk/execrelay/packages/proto"
)

// ---- fakeMLPredictor: an in-process MLPredictor test double -------------

type fakeMLPredictor struct {
	resp mlPredictResponse
	err  error
	// lastReq captures the last request the handler sent, for assertions.
	lastReq mlPredictWireRequest
}

func (f *fakeMLPredictor) Predict(_ context.Context, req mlPredictWireRequest) (mlPredictResponse, error) {
	f.lastReq = req
	return f.resp, f.err
}

func floatPtr(v float64) *float64 { return &v }
func strPtr(v string) *string     { return &v }

// mlTestHandler builds a Handler wired for /webhook/ml tests: a single
// license, a capturing NATS publisher, and an injectable fake predictor.
func mlTestHandler(predictor MLPredictor, enforce bool) (*Handler, *capturePublisher) {
	pub := &capturePublisher{}
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID:  "60123456789",
			Secret:     "alert-secret",
			HMACSecret: "hmac-secret",
			InstanceID: "mt5-a",
			Platform:   "mt5",
			Active:     true,
		}}),
		Publisher:    pub,
		Region:       "iad",
		MaxBodyBytes: 1 << 16,
		Now: func() time.Time {
			return time.Unix(1700000000, 123)
		},
		MLPredictor: predictor,
		MLEnforce:   enforce,
	})
	return h, pub
}

func mlBody(t *testing.T, overrides map[string]any) []byte {
	t.Helper()
	base := map[string]any{
		"license_id": "60123456789",
		"secret":     "alert-secret",
		"action":     "buy",
		"symbol":     "EURUSD",
		"volume":     0.1,
		"sl":         0.0,
		"tp":         0.0,
		"comment":    "AlgoCombo",
		"features":   map[string]any{"rsi_14": 55.5},
	}
	for k, v := range overrides {
		base[k] = v
	}
	data, err := json.Marshal(base)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func decodeSignal(t *testing.T, payload []byte) execrelaypb.Signal {
	t.Helper()
	var sig execrelaypb.Signal
	if err := oldproto.Unmarshal(payload, &sig); err != nil {
		t.Fatalf("unmarshal published signal: %v", err)
	}
	return sig
}

// ---- gating/auth preamble reuse -------------------------------------------

func TestWebhookML_RejectsBadSecretLikeFlatPath(t *testing.T) {
	h, _ := mlTestHandler(&fakeMLPredictor{}, true)
	body := mlBody(t, map[string]any{"secret": "wrong"})
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	req.Header.Set("X-ExecRelay-Signature", signature(string(body), "hmac-secret"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("secret_rejected")) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

func TestWebhookML_RejectsBadHMACLikeFlatPath(t *testing.T) {
	h, _ := mlTestHandler(&fakeMLPredictor{}, true)
	body := mlBody(t, nil)
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	req.Header.Set("X-ExecRelay-Signature", signature(string(body), "wrong-hmac-key"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("signature_rejected")) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

func TestWebhookML_RejectsUnknownLicense(t *testing.T) {
	h, _ := mlTestHandler(&fakeMLPredictor{}, true)
	body := mlBody(t, map[string]any{"license_id": "999"})
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
}

func TestWebhookML_PerimeterTokenGateReused(t *testing.T) {
	pub := &capturePublisher{}
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID: "60123456789", Secret: "alert-secret", HMACSecret: "hmac-secret",
			InstanceID: "mt5-a", Platform: "mt5", Active: true,
		}}),
		Publisher:      pub,
		Region:         "iad",
		PerimeterToken: "gate-secret",
		MLPredictor:    &fakeMLPredictor{},
		Now:            func() time.Time { return time.Unix(1700000000, 0) },
	})
	body := mlBody(t, nil)
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	req.Header.Set("X-ExecRelay-Signature", signature(string(body), "hmac-secret"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected perimeter rejection without token, got %d body=%s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("perimeter_rejected")) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

func TestWebhookML_KillSwitchHaltsLikeFlatPath(t *testing.T) {
	h, _ := mlTestHandler(&fakeMLPredictor{}, true)
	h.tradingHalted.Store(true)
	body := mlBody(t, nil)
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	req.Header.Set("X-ExecRelay-Signature", signature(string(body), "hmac-secret"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("trading_halted")) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

func TestWebhookML_MethodNotAllowed(t *testing.T) {
	h, _ := mlTestHandler(&fakeMLPredictor{}, true)
	req := httptest.NewRequest(http.MethodGet, "/webhook/ml", nil)
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d", rr.Code)
	}
}

// ---- malformed / missing-field rejections ---------------------------------

func TestWebhookML_MalformedJSONReturns400(t *testing.T) {
	h, _ := mlTestHandler(&fakeMLPredictor{}, true)
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", strings.NewReader("{not valid json"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("parse_error")) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

func TestWebhookML_InvalidActionReturns400(t *testing.T) {
	h, _ := mlTestHandler(&fakeMLPredictor{}, true)
	body := mlBody(t, map[string]any{"action": "hold"})
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("invalid_action")) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

func TestWebhookML_MissingSymbolReturns400(t *testing.T) {
	h, _ := mlTestHandler(&fakeMLPredictor{}, true)
	body := mlBody(t, map[string]any{"symbol": ""})
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("missing_field")) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

func TestWebhookML_MissingFeaturesFailsOpenViaPredictorError(t *testing.T) {
	// No features supplied: predictor rejects with an error, so the request
	// still fails open and publishes the caller's original action.
	predictor := &fakeMLPredictor{resp: mlPredictResponse{ActionSummary: "NOTHING", Error: strPtr("payload must include 'features' dict")}}
	h, pub := mlTestHandler(predictor, true)
	body := mlBody(t, map[string]any{"features": nil})
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	req.Header.Set("X-ExecRelay-Signature", signature(string(body), "hmac-secret"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"status":"accepted"`)) {
		t.Fatalf("expected fail-open accepted, body = %s", rr.Body.String())
	}
	sig := decodeSignal(t, pub.payload)
	if sig.Command != "buy" {
		t.Fatalf("expected fail-open to publish original buy action, got %q", sig.Command)
	}
}

// ---- decision -> command mapping (unit) -----------------------------------

func TestMapDecisionToCommand_AllBranches(t *testing.T) {
	cases := []struct {
		summary string
		pos     string
		wantCmd parser.Command
		wantPub bool
	}{
		{"OPEN_LONG", "", parser.CommandBuy, true},
		{"OPEN_SHORT", "", parser.CommandSell, true},
		{"FLIP_LONG", "SHORT", parser.CommandCloseShortOpenLong, true},
		{"FLIP_SHORT", "LONG", parser.CommandCloseLongOpenShort, true},
		{"CLOSE_ONLY", "LONG", parser.CommandCloseLong, true},
		{"CLOSE_ONLY", "SHORT", parser.CommandCloseShort, true},
		{"CLOSE_ONLY", "", parser.CommandInvalid, false},
		{"NOTHING", "LONG", parser.CommandInvalid, false},
		{"UNKNOWN_FUTURE_VALUE", "LONG", parser.CommandInvalid, false},
	}
	for _, tc := range cases {
		t.Run(tc.summary+"_"+tc.pos, func(t *testing.T) {
			cmd, pub := mapDecisionToCommand(tc.summary, tc.pos)
			if cmd != tc.wantCmd || pub != tc.wantPub {
				t.Fatalf("mapDecisionToCommand(%q,%q) = (%v,%v), want (%v,%v)", tc.summary, tc.pos, cmd, pub, tc.wantCmd, tc.wantPub)
			}
		})
	}
}

func TestMapActionToCommand(t *testing.T) {
	if mapActionToCommand("buy") != parser.CommandBuy {
		t.Fatal("buy should map to CommandBuy")
	}
	if mapActionToCommand("sell") != parser.CommandSell {
		t.Fatal("sell should map to CommandSell")
	}
}

func TestBuildMLSignal_ParamsSetAndZeroValuesOmitted(t *testing.T) {
	req := mlWebhookRequest{LicenseID: "L1", Symbol: "EURUSD", Volume: 0.5, SL: 10, TP: 0, Comment: "c1"}
	sig := buildMLSignal(req, parser.CommandBuy)
	if sig.LicenseID != "L1" || sig.Symbol != "EURUSD" || sig.Command != parser.CommandBuy {
		t.Fatalf("signal base fields wrong: %+v", sig)
	}
	var gotVol, gotSL, gotComment bool
	for i := 0; i < sig.ParamCount; i++ {
		switch sig.Params[i].Kind {
		case parser.ParamVolLots:
			gotVol = true
		case parser.ParamSL:
			gotSL = true
		case parser.ParamTP:
			t.Fatal("tp=0 should not produce a param")
		case parser.ParamComment:
			gotComment = true
		}
	}
	if !gotVol || !gotSL || !gotComment {
		t.Fatalf("expected vol/sl/comment params, got %d params: %+v", sig.ParamCount, sig.Params[:sig.ParamCount])
	}
}

func TestBuildMLSignal_TPParamSetWhenNonZero(t *testing.T) {
	req := mlWebhookRequest{LicenseID: "L1", Symbol: "EURUSD", TP: 40}
	sig := buildMLSignal(req, parser.CommandSell)
	var gotTP bool
	for i := 0; i < sig.ParamCount; i++ {
		if sig.Params[i].Kind == parser.ParamTP {
			gotTP = true
			if sig.Params[i].Value != "40" {
				t.Fatalf("tp value = %q, want 40", sig.Params[i].Value)
			}
		}
	}
	if !gotTP {
		t.Fatal("expected a tp param when TP != 0")
	}
}

// ---- publish failure downstream of a scored decision ----------------------

func TestWebhookML_PublishFailureReturns503(t *testing.T) {
	predictor := &fakeMLPredictor{resp: mlPredictResponse{ActionSummary: "OPEN_LONG", ProbWin: floatPtr(0.9), Threshold: 0.5}}
	pub := &capturePublisher{err: errors.New("nats down")}
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID: "60123456789", InstanceID: "mt5-a", Platform: "mt5", Active: true,
		}}),
		Publisher:   pub,
		Region:      "iad",
		MLPredictor: predictor,
		MLEnforce:   true,
		Now:         func() time.Time { return time.Unix(1700000000, 0) },
	})
	body := mlBody(t, nil)
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte("publish_failed")) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

// ---- full HTTP round trip: every action_summary branch in enforce mode ---

func TestWebhookML_EnforceMode_EachActionSummaryBranch(t *testing.T) {
	cases := []struct {
		name        string
		summary     string
		reqOverride map[string]any
		wantStatus  string
		wantCommand string
	}{
		{"open_long", "OPEN_LONG", nil, "accepted", "buy"},
		{"open_short", "OPEN_SHORT", map[string]any{"action": "sell"}, "accepted", "sell"},
		{"flip_long", "FLIP_LONG", map[string]any{"current_position": "SHORT"}, "accepted", "closeshortopenlong"},
		{"flip_short", "FLIP_SHORT", map[string]any{"action": "sell", "current_position": "LONG"}, "accepted", "closelongopenshort"},
		{"close_only_from_long", "CLOSE_ONLY", map[string]any{"action": "sell", "current_position": "LONG"}, "accepted", "closelong"},
		{"close_only_from_short", "CLOSE_ONLY", map[string]any{"current_position": "SHORT"}, "accepted", "closeshort"},
		{"nothing_skips", "NOTHING", nil, "skipped", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			predictor := &fakeMLPredictor{resp: mlPredictResponse{
				ActionSummary: tc.summary,
				ProbWin:       floatPtr(0.7),
				Threshold:     0.5,
			}}
			h, pub := mlTestHandler(predictor, true)
			body := mlBody(t, tc.reqOverride)
			req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
			req.Header.Set("X-ExecRelay-Signature", signature(string(body), "hmac-secret"))
			rr := httptest.NewRecorder()
			h.Routes().ServeHTTP(rr, req)
			if rr.Code != http.StatusOK {
				t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
			}
			if !bytes.Contains(rr.Body.Bytes(), []byte(`"status":"`+tc.wantStatus+`"`)) {
				t.Fatalf("expected status=%s, body = %s", tc.wantStatus, rr.Body.String())
			}
			if tc.wantCommand == "" {
				if pub.subject != "" {
					t.Fatalf("expected NOTHING to publish nothing, but got subject=%q", pub.subject)
				}
				return
			}
			sig := decodeSignal(t, pub.payload)
			if sig.Command != tc.wantCommand {
				t.Fatalf("published command = %q, want %q", sig.Command, tc.wantCommand)
			}
		})
	}
}

// ---- shadow vs enforce ------------------------------------------------

func TestWebhookML_ShadowMode_AlwaysPublishesOriginalAction(t *testing.T) {
	// Model recommends CLOSE_ONLY (i.e. would NOT open the caller's buy), but
	// shadow mode (the default) must still publish the caller's original buy.
	predictor := &fakeMLPredictor{resp: mlPredictResponse{
		ActionSummary: "CLOSE_ONLY",
		ProbWin:       floatPtr(0.2),
		Threshold:     0.5,
	}}
	h, pub := mlTestHandler(predictor, false) // enforce=false -> shadow
	body := mlBody(t, map[string]any{"current_position": "SHORT"})
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	req.Header.Set("X-ExecRelay-Signature", signature(string(body), "hmac-secret"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"status":"accepted"`)) {
		t.Fatalf("shadow mode should always accept, body = %s", rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"enforced":false`)) {
		t.Fatalf("expected enforced:false in response, body = %s", rr.Body.String())
	}
	sig := decodeSignal(t, pub.payload)
	if sig.Command != "buy" {
		t.Fatalf("shadow mode published %q, want caller's original buy", sig.Command)
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"action_summary":"CLOSE_ONLY"`)) {
		t.Fatalf("response should still report what the model would have done, body = %s", rr.Body.String())
	}
}

func TestWebhookML_EnforceMode_NothingPublishesNothing(t *testing.T) {
	predictor := &fakeMLPredictor{resp: mlPredictResponse{ActionSummary: "NOTHING", ProbWin: floatPtr(0.1), Threshold: 0.5}}
	h, pub := mlTestHandler(predictor, true)
	body := mlBody(t, nil)
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	req.Header.Set("X-ExecRelay-Signature", signature(string(body), "hmac-secret"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"status":"skipped"`)) {
		t.Fatalf("body = %s", rr.Body.String())
	}
	if pub.subject != "" {
		t.Fatalf("expected no publish, got subject=%q", pub.subject)
	}
}

// ---- fail-open on predictor error/timeout --------------------------------

func TestWebhookML_FailsOpenWhenPredictorErrors(t *testing.T) {
	predictor := &fakeMLPredictor{err: errors.New("connection refused")}
	h, pub := mlTestHandler(predictor, true) // even in enforce mode, errors fail open
	body := mlBody(t, map[string]any{"action": "sell"})
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	req.Header.Set("X-ExecRelay-Signature", signature(string(body), "hmac-secret"))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("fail-open must still return 200, got %d body=%s", rr.Code, rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"status":"accepted"`)) {
		t.Fatalf("body = %s", rr.Body.String())
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"error":"connection refused"`)) {
		t.Fatalf("expected predictor error surfaced in ml.error, body = %s", rr.Body.String())
	}
	sig := decodeSignal(t, pub.payload)
	if sig.Command != "sell" {
		t.Fatalf("fail-open must publish the caller's original action, got %q", sig.Command)
	}
}

func TestWebhookML_FailsOpenOnRealHTTPTimeout(t *testing.T) {
	// Exercise the real httpMLPredictor against an httptest server that never
	// responds, proving the ~timeout bound actually fires and the webhook
	// still fails open rather than hanging.
	block := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-block
	}))
	// srv.Close() blocks until outstanding requests finish, so the blocked
	// handler goroutine must be released *before* Close() runs. Deferring
	// close(block) after defer srv.Close() achieves that via LIFO order.
	defer srv.Close()
	defer close(block)

	pub := &capturePublisher{}
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID: "60123456789", InstanceID: "mt5-a", Platform: "mt5", Active: true,
		}}),
		Publisher:        pub,
		Region:           "iad",
		MLPredictorURL:   srv.URL,
		MLPredictTimeout: 50 * time.Millisecond,
		MLEnforce:        true,
		Now:              func() time.Time { return time.Unix(1700000000, 0) },
	})
	body := mlBody(t, nil)
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}
	sig := decodeSignal(t, pub.payload)
	if sig.Command != "buy" {
		t.Fatalf("expected fail-open buy, got %q", sig.Command)
	}
}

func TestHTTPMLPredictor_SuccessRoundTrip(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var got mlPredictWireRequest
		_ = json.NewDecoder(r.Body).Decode(&got)
		if got.Direction != 1 {
			t.Errorf("direction = %d, want 1", got.Direction)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(mlPredictResponse{
			ActionSummary: "OPEN_LONG", ProbWin: floatPtr(0.9), Threshold: 0.5, ModelVersion: "xgb-v3",
		})
	}))
	defer srv.Close()

	p := newHTTPMLPredictor(srv.URL, 2*time.Second)
	resp, err := p.Predict(context.Background(), mlPredictWireRequest{Direction: 1, Features: map[string]any{"a": 1.0}})
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if resp.ActionSummary != "OPEN_LONG" || resp.ModelVersion != "xgb-v3" {
		t.Fatalf("resp = %+v", resp)
	}
}

func TestHTTPMLPredictor_TolerantOfMissingModelVersion(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"action_summary":"NOTHING","threshold":0.5}`))
	}))
	defer srv.Close()

	p := newHTTPMLPredictor(srv.URL, 2*time.Second)
	resp, err := p.Predict(context.Background(), mlPredictWireRequest{Direction: 1})
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if resp.ModelVersion != "" {
		t.Fatalf("expected empty model_version when absent, got %q", resp.ModelVersion)
	}
}

func TestHTTPMLPredictor_NonOKStatusReturnsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(mlPredictResponse{ActionSummary: "NOTHING", Error: strPtr("bad payload")})
	}))
	defer srv.Close()

	p := newHTTPMLPredictor(srv.URL, 2*time.Second)
	_, err := p.Predict(context.Background(), mlPredictWireRequest{Direction: 1})
	if err == nil || !strings.Contains(err.Error(), "bad payload") {
		t.Fatalf("expected error containing 'bad payload', got %v", err)
	}
}

func TestHTTPMLPredictor_ConnectionRefused(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := srv.URL
	srv.Close() // now nothing is listening

	p := newHTTPMLPredictor(url, 500*time.Millisecond)
	_, err := p.Predict(context.Background(), mlPredictWireRequest{Direction: 1})
	if err == nil {
		t.Fatal("expected connection error")
	}
}

func TestHTTPMLPredictor_MalformedResponseBodyErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer srv.Close()

	p := newHTTPMLPredictor(srv.URL, 2*time.Second)
	_, err := p.Predict(context.Background(), mlPredictWireRequest{Direction: 1})
	if err == nil {
		t.Fatal("expected decode error")
	}
}

// ---- current_position resolution: caller / db / unknown ------------------

func TestResolveCurrentPosition_CallerWins(t *testing.T) {
	h := &Handler{now: time.Now}
	pos, source := h.resolveCurrentPosition(context.Background(), "L1", "A1", "EURUSD", strPtr("long"))
	if pos != "LONG" || source != "caller" {
		t.Fatalf("pos=%q source=%q", pos, source)
	}
}

func TestResolveCurrentPosition_CallerExplicitNullTreatedAsFlat(t *testing.T) {
	h := &Handler{now: time.Now}
	pos, source := h.resolveCurrentPosition(context.Background(), "L1", "A1", "EURUSD", strPtr(""))
	if pos != "" || source != "caller" {
		t.Fatalf("pos=%q source=%q", pos, source)
	}
}

func TestResolveCurrentPosition_NoDBIsUnknown(t *testing.T) {
	h := &Handler{now: time.Now}
	pos, source := h.resolveCurrentPosition(context.Background(), "L1", "A1", "EURUSD", nil)
	if pos != "" || source != "unknown" {
		t.Fatalf("pos=%q source=%q", pos, source)
	}
}

func TestResolveCurrentPosition_DBFallbackWhenCallerOmits(t *testing.T) {
	db := newFakeDB(t, fakeDBBehavior{
		query: func(query string, args []driver.Value) (driver.Rows, error) {
			if !strings.Contains(query, "account_positions") {
				return nil, sql.ErrNoRows
			}
			return &fakeRows{cols: []string{"position_size"}, vals: [][]driver.Value{{-1.5}}}, nil
		},
	})
	defer db.Close()

	h := &Handler{now: time.Now, db: db}
	pos, source := h.resolveCurrentPosition(context.Background(), "L1", "A1", "EURUSD", nil)
	if pos != "SHORT" || source != "db" {
		t.Fatalf("pos=%q source=%q", pos, source)
	}
}

func TestLookupPositionFromDB_NilDBReturnsNoRows(t *testing.T) {
	h := &Handler{now: time.Now}
	_, err := h.lookupPositionFromDB(context.Background(), "L1", "A1", "EURUSD")
	if !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("err = %v", err)
	}
}

func TestLookupPositionFromDB_PositiveIsLong(t *testing.T) {
	db := newFakeDB(t, fakeDBBehavior{
		query: func(query string, args []driver.Value) (driver.Rows, error) {
			return &fakeRows{cols: []string{"position_size"}, vals: [][]driver.Value{{2.0}}}, nil
		},
	})
	defer db.Close()
	h := &Handler{now: time.Now, db: db}
	pos, err := h.lookupPositionFromDB(context.Background(), "L1", "A1", "EURUSD")
	if err != nil || pos != "LONG" {
		t.Fatalf("pos=%q err=%v", pos, err)
	}
}

func TestLookupPositionFromDB_ZeroIsFlat(t *testing.T) {
	db := newFakeDB(t, fakeDBBehavior{
		query: func(query string, args []driver.Value) (driver.Rows, error) {
			return &fakeRows{cols: []string{"position_size"}, vals: [][]driver.Value{{0.0}}}, nil
		},
	})
	defer db.Close()
	h := &Handler{now: time.Now, db: db}
	pos, err := h.lookupPositionFromDB(context.Background(), "L1", "A1", "EURUSD")
	if err != nil || pos != "" {
		t.Fatalf("pos=%q err=%v", pos, err)
	}
}

func TestLookupPositionFromDB_NoRowPropagatesError(t *testing.T) {
	db := newFakeDB(t, fakeDBBehavior{
		query: func(query string, args []driver.Value) (driver.Rows, error) {
			return nil, sql.ErrNoRows
		},
	})
	defer db.Close()
	h := &Handler{now: time.Now, db: db}
	_, err := h.lookupPositionFromDB(context.Background(), "L1", "A1", "EURUSD")
	if err == nil {
		t.Fatal("expected error propagated")
	}
}

// ---- audit trail (ml_decisions) -------------------------------------------

func TestRecordMLDecision_NilDBIsNoOp(t *testing.T) {
	h := &Handler{now: time.Now}
	h.recordMLDecision(mlDecisionRow{TraceID: "t1"}) // must not panic
}

func TestRecordMLDecision_InsertsWhenDBPresent(t *testing.T) {
	inserted := make(chan []driver.Value, 1)
	db := newFakeDB(t, fakeDBBehavior{
		exec: func(query string, args []driver.Value) (driver.Result, error) {
			if strings.Contains(query, "ml_decisions") {
				inserted <- args
			}
			return driver.RowsAffected(1), nil
		},
	})
	defer db.Close()

	h := &Handler{now: time.Now, db: db}
	errMsg := "boom"
	h.recordMLDecision(mlDecisionRow{
		TraceID: "trace-1", LicenseID: "L1", Symbol: "EURUSD", Action: "buy",
		ProbWin: floatPtr(0.8), Threshold: 0.5, ActionSummary: "OPEN_LONG",
		PublishedCommand: strPtr("buy"), Enforced: true, ModelVersion: strPtr("v1"),
		PositionSource: "caller", Error: &errMsg,
	})

	select {
	case args := <-inserted:
		if len(args) != 12 {
			t.Fatalf("expected 12 bound args, got %d: %+v", len(args), args)
		}
		if args[0] != "trace-1" {
			t.Fatalf("trace_id arg = %v", args[0])
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for async ml_decisions insert")
	}
}

func TestWebhookML_AuditRowWrittenOnAccept(t *testing.T) {
	inserted := make(chan []driver.Value, 1)
	db := newFakeDB(t, fakeDBBehavior{
		exec: func(query string, args []driver.Value) (driver.Result, error) {
			if strings.Contains(query, "ml_decisions") {
				select {
				case inserted <- args:
				default:
				}
			}
			return driver.RowsAffected(1), nil
		},
		query: func(query string, args []driver.Value) (driver.Rows, error) {
			return nil, sql.ErrNoRows
		},
	})
	defer db.Close()

	predictor := &fakeMLPredictor{resp: mlPredictResponse{ActionSummary: "OPEN_LONG", ProbWin: floatPtr(0.77), Threshold: 0.5}}
	pub := &capturePublisher{}
	h := NewHandler(Options{
		Store: NewStaticLicenseStore([]LicenseRecord{{
			LicenseID: "60123456789", InstanceID: "mt5-a", Platform: "mt5", Active: true,
		}}),
		Publisher:   pub,
		Region:      "iad",
		DB:          db,
		MLPredictor: predictor,
		MLEnforce:   true,
		Now:         func() time.Time { return time.Unix(1700000000, 0) },
	})
	body := mlBody(t, nil)
	req := httptest.NewRequest(http.MethodPost, "/webhook/ml", bytes.NewReader(body))
	rr := httptest.NewRecorder()
	h.Routes().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d body = %s", rr.Code, rr.Body.String())
	}

	select {
	case args := <-inserted:
		if args[3] != "buy" { // action column
			t.Fatalf("action arg = %v", args[3])
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for ml_decisions insert from webhookML")
	}
}

// ---- writeJSONAny sanity ---------------------------------------------------

func TestWriteJSONAny_EncodesNestedPayload(t *testing.T) {
	rr := httptest.NewRecorder()
	writeJSONAny(rr, http.StatusOK, map[string]any{"status": "accepted", "ml": map[string]any{"prob_win": 0.5}})
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d", rr.Code)
	}
	if !bytes.Contains(rr.Body.Bytes(), []byte(`"prob_win":0.5`)) {
		t.Fatalf("body = %s", rr.Body.String())
	}
}

// ===========================================================================
// Fake database/sql driver: enough of the driver.Conn/Stmt/Rows surface to
// exercise lookupPositionFromDB (SELECT) and recordMLDecision (INSERT)
// against a real *sql.DB without a live Postgres. Mirrors the package's
// existing convention of tolerating (and, here, also exercising) both the
// nil-DB and DB-present paths.
// ===========================================================================

type fakeDBBehavior struct {
	query func(query string, args []driver.Value) (driver.Rows, error)
	exec  func(query string, args []driver.Value) (driver.Result, error)
}

var (
	fakeDriverRegisterOnce sync.Once
	fakeDriverMu           sync.Mutex
	fakeDriverBehaviors    = map[string]fakeDBBehavior{}
	fakeDriverSeq          int
)

type fakeSQLDriver struct{}

func (fakeSQLDriver) Open(name string) (driver.Conn, error) {
	fakeDriverMu.Lock()
	b := fakeDriverBehaviors[name]
	fakeDriverMu.Unlock()
	return &fakeConn{behavior: b}, nil
}

type fakeConn struct{ behavior fakeDBBehavior }

func (c *fakeConn) Prepare(query string) (driver.Stmt, error) {
	return &fakeStmt{conn: c, query: query}, nil
}
func (c *fakeConn) Close() error { return nil }
func (c *fakeConn) Begin() (driver.Tx, error) {
	return nil, errors.New("transactions not supported by fake driver")
}

type fakeStmt struct {
	conn  *fakeConn
	query string
}

func (s *fakeStmt) Close() error  { return nil }
func (s *fakeStmt) NumInput() int { return -1 }
func (s *fakeStmt) Exec(args []driver.Value) (driver.Result, error) {
	if s.conn.behavior.exec != nil {
		return s.conn.behavior.exec(s.query, args)
	}
	return driver.RowsAffected(0), nil
}
func (s *fakeStmt) Query(args []driver.Value) (driver.Rows, error) {
	if s.conn.behavior.query != nil {
		return s.conn.behavior.query(s.query, args)
	}
	return nil, sql.ErrNoRows
}

// fakeRows implements driver.Rows for a small fixed result set.
type fakeRows struct {
	cols []string
	vals [][]driver.Value
	pos  int
}

func (r *fakeRows) Columns() []string { return r.cols }
func (r *fakeRows) Close() error      { return nil }
func (r *fakeRows) Next(dest []driver.Value) error {
	if r.pos >= len(r.vals) {
		return io.EOF
	}
	copy(dest, r.vals[r.pos])
	r.pos++
	return nil
}

// newFakeDB registers a uniquely-named fake driver instance (drivers are
// process-global in database/sql) and opens a *sql.DB bound to the given
// behavior.
func newFakeDB(t *testing.T, behavior fakeDBBehavior) *sql.DB {
	t.Helper()
	fakeDriverRegisterOnce.Do(func() {
		sql.Register("execrelay_fake_ingress_test", fakeSQLDriver{})
	})
	fakeDriverMu.Lock()
	fakeDriverSeq++
	name := fmt.Sprintf("fake-%d", fakeDriverSeq)
	fakeDriverBehaviors[name] = behavior
	fakeDriverMu.Unlock()

	db, err := sql.Open("execrelay_fake_ingress_test", name)
	if err != nil {
		t.Fatalf("open fake db: %v", err)
	}
	return db
}
