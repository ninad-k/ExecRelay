package dxtrade

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"sync"
	"time"

	"github.com/sony/gobreaker"
)

const (
	maxRetries  = 3
	retryBaseMs = 200
)

// Client executes trading commands against the DXTrade REST API.
// It maintains a session token and re-authenticates on 401.
type Client struct {
	cfg   InstanceConfig
	http  *http.Client
	mu    sync.Mutex
	token string
	cb    *gobreaker.CircuitBreaker
}

func NewClient(cfg InstanceConfig) *Client {
	cb := gobreaker.NewCircuitBreaker(gobreaker.Settings{
		Name:        "dxtrade:" + cfg.InstanceID,
		MaxRequests: 1,
		Interval:    60 * time.Second,
		Timeout:     30 * time.Second,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			return counts.ConsecutiveFailures >= 5
		},
	})
	return &Client{
		cfg:  cfg,
		http: &http.Client{Timeout: 10 * time.Second},
		cb:   cb,
	}
}

type loginResp struct {
	SessionToken string `json:"sessionToken"`
}

func (c *Client) login(ctx context.Context) error {
	body, _ := json.Marshal(map[string]string{
		"username": c.cfg.Username,
		"password": c.cfg.Password,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL()+"/api/auth/login", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("dxtrade login: status %d", resp.StatusCode)
	}

	var lr loginResp
	if err := json.NewDecoder(resp.Body).Decode(&lr); err != nil {
		return fmt.Errorf("dxtrade login: decode: %w", err)
	}
	if lr.SessionToken == "" {
		return fmt.Errorf("dxtrade login: empty session token")
	}
	c.mu.Lock()
	c.token = lr.SessionToken
	c.mu.Unlock()
	return nil
}

// Execute sends a trading command to DXTrade.
// On 401 it re-authenticates and retries once.
// On transient errors it retries with exponential backoff (up to maxRetries).
func (c *Client) Execute(ctx context.Context, cmd *Command) (*Result, error) {
	var lastErr error
	for attempt := 0; attempt <= maxRetries; attempt++ {
		if attempt > 0 {
			delay := time.Duration(float64(retryBaseMs)*math.Pow(2, float64(attempt-1))) * time.Millisecond
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(delay):
			}
		}

		result, err := func() (*Result, error) {
			raw, cbErr := c.cb.Execute(func() (interface{}, error) {
				return c.execute(ctx, cmd)
			})
			if cbErr != nil {
				return nil, cbErr
			}
			return raw.(*Result), nil
		}()
		if err == nil {
			return result, nil
		}

		if _, ok := err.(authError); ok {
			if loginErr := c.login(ctx); loginErr != nil {
				return nil, fmt.Errorf("dxtrade reauth: %w", loginErr)
			}
			return c.execute(ctx, cmd)
		}

		lastErr = err
	}
	return nil, lastErr
}

func (c *Client) execute(ctx context.Context, cmd *Command) (*Result, error) {
	c.mu.Lock()
	token := c.token
	c.mu.Unlock()

	if token == "" {
		if err := c.login(ctx); err != nil {
			return nil, err
		}
		c.mu.Lock()
		token = c.token
		c.mu.Unlock()
	}

	switch cmd.Action {
	case ActionBuy, ActionSell, ActionBuyStop, ActionSellStop, ActionBuyLimit, ActionSellLimit:
		return c.placeOrder(ctx, token, cmd)
	case ActionCloseBuy, ActionCloseSell, ActionCloseAll:
		return c.closePositions(ctx, token, cmd)
	case ActionCancel:
		return c.cancelOrders(ctx, token, cmd)
	default:
		return nil, fmt.Errorf("dxtrade: unsupported action %q", cmd.Action)
	}
}

type placeOrderReq struct {
	Type   string  `json:"type"`
	Side   string  `json:"side"`
	Symbol string  `json:"symbol"`
	Qty    float64 `json:"qty"`
}

func (c *Client) placeOrder(ctx context.Context, token string, cmd *Command) (*Result, error) {
	orderType := "MARKET"
	if cmd.Action == ActionBuyStop || cmd.Action == ActionSellStop {
		orderType = "STOP"
	} else if cmd.Action == ActionBuyLimit || cmd.Action == ActionSellLimit {
		orderType = "LIMIT"
	}

	side := "BUY"
	if cmd.Action == ActionSell || cmd.Action == ActionSellStop || cmd.Action == ActionSellLimit {
		side = "SELL"
	}

	payload := placeOrderReq{
		Type:   orderType,
		Side:   side,
		Symbol: cmd.Symbol,
		Qty:    cmd.Volume,
	}

	body, _ := json.Marshal(payload)
	url := fmt.Sprintf("%s/api/trading/accounts/%s/orders", c.baseURL(), c.cfg.Account)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "DXAPI "+token)

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusUnauthorized {
		return nil, authError{}
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("dxtrade place order: status %d", resp.StatusCode)
	}

	var raw map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&raw)
	orderID := ""
	if id, ok := raw["orderId"]; ok {
		orderID = fmt.Sprintf("%v", id)
	}
	return &Result{Status: StatusFilled, BrokerOrderID: orderID}, nil
}

func (c *Client) closePositions(ctx context.Context, token string, cmd *Command) (*Result, error) {
	// GET positions, filter by symbol/side, then close each.
	url := fmt.Sprintf("%s/api/trading/accounts/%s/positions", c.baseURL(), c.cfg.Account)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "DXAPI "+token)

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusUnauthorized {
		return nil, authError{}
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("dxtrade get positions: status %d", resp.StatusCode)
	}

	var positions []map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&positions); err != nil {
		return nil, fmt.Errorf("dxtrade decode positions: %w", err)
	}

	wantSide := ""
	if cmd.Action == ActionCloseBuy {
		wantSide = "BUY"
	} else if cmd.Action == ActionCloseSell {
		wantSide = "SELL"
	}

	closed := 0
	for _, pos := range positions {
		sym, _ := pos["symbol"].(string)
		if sym != cmd.Symbol {
			continue
		}
		side, _ := pos["side"].(string)
		if wantSide != "" && side != wantSide {
			continue
		}
		posID := fmt.Sprintf("%v", pos["positionId"])
		closeURL := fmt.Sprintf("%s/api/trading/accounts/%s/positions/%s", c.baseURL(), c.cfg.Account, posID)
		closeReq, _ := http.NewRequestWithContext(ctx, http.MethodDelete, closeURL, nil)
		closeReq.Header.Set("Authorization", "DXAPI "+token)
		closeResp, err := c.http.Do(closeReq)
		if err != nil {
			continue
		}
		closeResp.Body.Close()
		if closeResp.StatusCode == http.StatusOK || closeResp.StatusCode == http.StatusNoContent {
			closed++
		}
	}

	return &Result{Status: StatusFilled, BrokerOrderID: fmt.Sprintf("closed:%d", closed)}, nil
}

func (c *Client) cancelOrders(ctx context.Context, token string, cmd *Command) (*Result, error) {
	url := fmt.Sprintf("%s/api/trading/accounts/%s/orders", c.baseURL(), c.cfg.Account)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "DXAPI "+token)

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusUnauthorized {
		return nil, authError{}
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("dxtrade get orders: status %d", resp.StatusCode)
	}

	var orders []map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&orders); err != nil {
		return nil, fmt.Errorf("dxtrade decode orders: %w", err)
	}

	cancelled := 0
	for _, order := range orders {
		sym, _ := order["symbol"].(string)
		if sym != cmd.Symbol {
			continue
		}
		orderID := fmt.Sprintf("%v", order["orderId"])
		delURL := fmt.Sprintf("%s/api/trading/accounts/%s/orders/%s", c.baseURL(), c.cfg.Account, orderID)
		delReq, _ := http.NewRequestWithContext(ctx, http.MethodDelete, delURL, nil)
		delReq.Header.Set("Authorization", "DXAPI "+token)
		delResp, err := c.http.Do(delReq)
		if err != nil {
			continue
		}
		delResp.Body.Close()
		if delResp.StatusCode == http.StatusOK || delResp.StatusCode == http.StatusNoContent {
			cancelled++
		}
	}

	return &Result{Status: StatusFilled, BrokerOrderID: fmt.Sprintf("cancelled:%d", cancelled)}, nil
}

func (c *Client) baseURL() string {
	return "https://" + c.cfg.Host
}

type authError struct{}

func (authError) Error() string { return "dxtrade: auth required" }
