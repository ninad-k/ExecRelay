package parser

func Parse(input string) (Signal, error) {
	var signal Signal
	input = trimASCII(input)
	if input == "" {
		return signal, ParseError{Code: ErrEmptyInput}
	}

	pos := 0
	var ok bool
	signal.LicenseID, pos, ok = nextField(input, pos)
	if !ok || signal.LicenseID == "" {
		return signal, ParseError{Code: ErrMissingField, Field: "license_id"}
	}

	signal.RawCommand, pos, ok = nextField(input, pos)
	if !ok || signal.RawCommand == "" {
		return signal, ParseError{Code: ErrMissingField, Field: "command"}
	}
	signal.Command = commandFrom(signal.RawCommand)
	if signal.Command == CommandInvalid {
		return signal, ParseError{Code: ErrUnknownCommand, Field: signal.RawCommand}
	}

	signal.Symbol, pos, ok = nextField(input, pos)
	if !ok || signal.Symbol == "" {
		return signal, ParseError{Code: ErrMissingField, Field: "symbol"}
	}

	state := validationState{}
	for pos <= len(input) {
		var raw string
		raw, pos, ok = nextField(input, pos)
		if !ok {
			break
		}
		if raw == "" {
			return signal, ParseError{Code: ErrMalformedParam}
		}
		if signal.ParamCount == MaxParams {
			return signal, ParseError{Code: ErrTooManyParams}
		}

		key, value, valid := splitParam(raw)
		if !valid {
			return signal, ParseError{Code: ErrMalformedParam, Field: raw}
		}

		kind := paramKindFromKey(key)
		if kind == ParamUnknown {
			return signal, ParseError{Code: ErrUnknownParam, Field: key}
		}

		if err := state.add(kind, value); err != nil {
			return signal, err
		}

		signal.Params[signal.ParamCount] = Param{Kind: kind, Key: key, Value: value}
		signal.ParamCount++
	}

	if err := validateSignal(&signal, state); err != nil {
		return signal, err
	}
	return signal, nil
}

type validationState struct {
	hasVolume    bool
	hasSL        bool
	hasTP        bool
	hasEntry     bool
	hasRisk      bool
	hasLossRisk  bool
	hasATR       bool
	hasATRPeriod bool
	hasATRFrame  bool
}

func (s *validationState) add(kind ParamKind, value string) error {
	if value == "" {
		return ParseError{Code: ErrMalformedParam, Field: kind.String()}
	}

	if kind.IsVolume() {
		if s.hasVolume {
			return ParseError{Code: ErrDuplicateVolume, Field: kind.String()}
		}
		s.hasVolume = true
	}
	if kind.IsSL() {
		if s.hasSL {
			return ParseError{Code: ErrDuplicateSL, Field: kind.String()}
		}
		s.hasSL = true
	}
	if kind.IsTP() {
		if s.hasTP {
			return ParseError{Code: ErrDuplicateTP, Field: kind.String()}
		}
		s.hasTP = true
	}
	if kind.IsEntry() {
		if s.hasEntry {
			return ParseError{Code: ErrDuplicateEntry, Field: kind.String()}
		}
		s.hasEntry = true
	}

	switch kind {
	case ParamRisk:
		s.hasRisk = true
	case ParamVolDollar, ParamVolPctBalanceLoss, ParamVolPctEquityLoss:
		s.hasLossRisk = true
	case ParamComment:
		if len(value) > MaxComment {
			return ParseError{Code: ErrCommentTooLong, Field: kind.String()}
		}
	case ParamATRTimeframe:
		s.hasATR = true
		s.hasATRFrame = true
	case ParamATRPeriod:
		s.hasATR = true
		s.hasATRPeriod = true
	case ParamATRMultiplier, ParamATRShift, ParamATRTrigger:
		s.hasATR = true
	}

	return nil
}

func validateSignal(signal *Signal, state validationState) error {
	switch signal.Command {
	case CommandCloseAll, CommandCloseAllEAOff:
		if signal.Symbol == "" {
			return ParseError{Code: ErrCloseAllRequiresChartSymbol, Field: "symbol"}
		}
	case CommandEAOff:
		if !eqFoldASCII(signal.Symbol, "eaoff") {
			return ParseError{Code: ErrManagementSymbol, Field: "eaoff"}
		}
	case CommandEAOn:
		if !eqFoldASCII(signal.Symbol, "eaon") {
			return ParseError{Code: ErrManagementSymbol, Field: "eaon"}
		}
	}

	if signal.Command.OpensOrder() && !state.hasVolume {
		return ParseError{Code: ErrMissingVolume, Field: "volume"}
	}
	if signal.Command.IsPendingOpen() && !state.hasEntry {
		return ParseError{Code: ErrPendingRequiresEntry, Field: "entry"}
	}
	if state.hasLossRisk && !state.hasSL {
		return ParseError{Code: ErrRiskVolumeRequiresSL, Field: "sl"}
	}
	if state.hasATR && (!state.hasATRFrame || !state.hasATRPeriod) {
		return ParseError{Code: ErrATRRequiresTimeframePeriod, Field: "atr"}
	}
	if signal.Command.IsModify() && !state.hasSL && !state.hasTP {
		return ParseError{Code: ErrModifyRequiresSLTP, Field: "sl_tp"}
	}
	switch signal.Command {
	case CommandCloseLongVol, CommandCloseShortVol:
		if !state.hasRisk {
			return ParseError{Code: ErrPartialVolumeRequiresRisk, Field: "risk"}
		}
	}
	return nil
}

func nextField(input string, pos int) (string, int, bool) {
	if pos > len(input) {
		return "", pos, false
	}
	start := pos
	for pos < len(input) && input[pos] != ',' {
		pos++
	}
	field := trimASCII(input[start:pos])
	if pos == len(input) {
		return field, pos + 1, true
	}
	return field, pos + 1, true
}

func splitParam(raw string) (string, string, bool) {
	for i := 0; i < len(raw); i++ {
		if raw[i] == '=' {
			key := trimASCII(raw[:i])
			value := trimASCII(raw[i+1:])
			return key, value, key != "" && value != ""
		}
	}
	return "", "", false
}

func trimASCII(value string) string {
	start := 0
	for start < len(value) && isASCIISpace(value[start]) {
		start++
	}
	end := len(value)
	for end > start && isASCIISpace(value[end-1]) {
		end--
	}
	return value[start:end]
}

func isASCIISpace(c byte) bool {
	switch c {
	case ' ', '\t', '\n', '\r':
		return true
	default:
		return false
	}
}
