# parser-go

PineConnector-compatible alert parser for the ExecRelay Go hot path.

The parser accepts:

```text
license_id,command,symbol_or_special,param=value,...
```

It returns a structured `Signal` without normalizing or copying user-provided
values. Parsed string fields reference the original alert body.

Phase 1 contract:

- Market, pending, close, modify, macro, cancel, and EA management commands.
- PineConnector aliases such as `long`, `bullish`, `short`, `bearish`, `CL+OL`,
  `CS+OS`, `CLS+OL`, and `CLS+OS`.
- Explicit and legacy volume, SL, TP, entry, trailing, ATR trailing, breakeven,
  secret, comment, spread, and account filter parameters.
- Validation for mutually exclusive volume/SL/TP/entry parameters, pending entry
  requirements, risk-by-loss SL requirements, `closeall` chart symbol, management
  command special symbols, ATR required fields, and comment length.
- Legacy pending `price=` is accepted as an alias of `entry_price=`.
