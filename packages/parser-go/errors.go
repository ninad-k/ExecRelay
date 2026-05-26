package parser

type ErrorCode uint8

const (
	ErrEmptyInput ErrorCode = iota + 1
	ErrMissingField
	ErrUnknownCommand
	ErrMalformedParam
	ErrUnknownParam
	ErrTooManyParams
	ErrDuplicateVolume
	ErrDuplicateSL
	ErrDuplicateTP
	ErrDuplicateEntry
	ErrMissingVolume
	ErrPendingRequiresEntry
	ErrRiskVolumeRequiresSL
	ErrCloseAllRequiresChartSymbol
	ErrManagementSymbol
	ErrCommentTooLong
	ErrATRRequiresTimeframePeriod
	ErrModifyRequiresSLTP
	ErrPartialVolumeRequiresRisk
)

type ParseError struct {
	Code  ErrorCode
	Field string
}

func (e ParseError) Error() string {
	switch e.Code {
	case ErrEmptyInput:
		return "empty alert"
	case ErrMissingField:
		return "missing required field"
	case ErrUnknownCommand:
		return "unknown command"
	case ErrMalformedParam:
		return "malformed parameter"
	case ErrUnknownParam:
		return "unknown parameter"
	case ErrTooManyParams:
		return "too many parameters"
	case ErrDuplicateVolume:
		return "more than one volume parameter"
	case ErrDuplicateSL:
		return "more than one stop-loss parameter"
	case ErrDuplicateTP:
		return "more than one take-profit parameter"
	case ErrDuplicateEntry:
		return "more than one entry parameter"
	case ErrMissingVolume:
		return "missing volume parameter"
	case ErrPendingRequiresEntry:
		return "pending command requires entry parameter"
	case ErrRiskVolumeRequiresSL:
		return "risk-by-loss volume requires stop-loss"
	case ErrCloseAllRequiresChartSymbol:
		return "closeall command requires chart symbol"
	case ErrManagementSymbol:
		return "management command requires matching special symbol"
	case ErrCommentTooLong:
		return "comment exceeds 20 characters"
	case ErrATRRequiresTimeframePeriod:
		return "ATR trailing requires atrtimeframe and atrperiod"
	case ErrModifyRequiresSLTP:
		return "modify command requires SL or TP parameter"
	case ErrPartialVolumeRequiresRisk:
		return "partial volume close requires risk parameter"
	default:
		return "parse error"
	}
}
