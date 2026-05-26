package parser

const (
	MaxParams  = 48
	MaxComment = 20
)

type Command uint8

const (
	CommandInvalid Command = iota
	CommandBuy
	CommandSell
	CommandBuyStop
	CommandBuyLimit
	CommandSellStop
	CommandSellLimit
	CommandCloseAll
	CommandCancelLong
	CommandCancelShort
	CommandCloseLong
	CommandCloseShort
	CommandCloseLongShort
	CommandCloseLongPct
	CommandCloseShortPct
	CommandCloseLongVol
	CommandCloseShortVol
	CommandNewSLTPLong
	CommandNewSLTPShort
	CommandNewSLTPBuyStop
	CommandNewSLTPBuyLimit
	CommandNewSLTPSellStop
	CommandNewSLTPSellLimit
	CommandCloseLongOpenLong
	CommandCloseLongOpenShort
	CommandCloseShortOpenLong
	CommandCloseShortOpenShort
	CommandCloseLongShortOpenLong
	CommandCloseLongShortOpenShort
	CommandCancelLongBuyStop
	CommandCancelLongBuyLimit
	CommandCancelShortSellStop
	CommandCancelShortSellLimit
	CommandEAOff
	CommandEAOn
	CommandCloseAllEAOff
)

func (c Command) String() string {
	switch c {
	case CommandBuy:
		return "buy"
	case CommandSell:
		return "sell"
	case CommandBuyStop:
		return "buystop"
	case CommandBuyLimit:
		return "buylimit"
	case CommandSellStop:
		return "sellstop"
	case CommandSellLimit:
		return "selllimit"
	case CommandCloseAll:
		return "closeall"
	case CommandCancelLong:
		return "cancellong"
	case CommandCancelShort:
		return "cancelshort"
	case CommandCloseLong:
		return "closelong"
	case CommandCloseShort:
		return "closeshort"
	case CommandCloseLongShort:
		return "closelongshort"
	case CommandCloseLongPct:
		return "closelongpct"
	case CommandCloseShortPct:
		return "closeshortpct"
	case CommandCloseLongVol:
		return "closelongvol"
	case CommandCloseShortVol:
		return "closeshortvol"
	case CommandNewSLTPLong:
		return "newsltplong"
	case CommandNewSLTPShort:
		return "newsltpshort"
	case CommandNewSLTPBuyStop:
		return "newsltpbuystop"
	case CommandNewSLTPBuyLimit:
		return "newsltpbuylimit"
	case CommandNewSLTPSellStop:
		return "newsltpsellstop"
	case CommandNewSLTPSellLimit:
		return "newsltpselllimit"
	case CommandCloseLongOpenLong:
		return "closelongopenlong"
	case CommandCloseLongOpenShort:
		return "closelongopenshort"
	case CommandCloseShortOpenLong:
		return "closeshortopenlong"
	case CommandCloseShortOpenShort:
		return "closeshortopenshort"
	case CommandCloseLongShortOpenLong:
		return "closelongshortopenlong"
	case CommandCloseLongShortOpenShort:
		return "closelongshortopenshort"
	case CommandCancelLongBuyStop:
		return "cancellongbuystop"
	case CommandCancelLongBuyLimit:
		return "cancellongbuylimit"
	case CommandCancelShortSellStop:
		return "cancelshortsellstop"
	case CommandCancelShortSellLimit:
		return "cancelshortselllimit"
	case CommandEAOff:
		return "eaoff"
	case CommandEAOn:
		return "eaon"
	case CommandCloseAllEAOff:
		return "closealleaoff"
	default:
		return "invalid"
	}
}

func (c Command) OpensOrder() bool {
	switch c {
	case CommandBuy, CommandSell,
		CommandBuyStop, CommandBuyLimit, CommandSellStop, CommandSellLimit,
		CommandCloseLongOpenLong, CommandCloseLongOpenShort,
		CommandCloseShortOpenLong, CommandCloseShortOpenShort,
		CommandCloseLongShortOpenLong, CommandCloseLongShortOpenShort,
		CommandCancelLongBuyStop, CommandCancelLongBuyLimit,
		CommandCancelShortSellStop, CommandCancelShortSellLimit:
		return true
	default:
		return false
	}
}

func (c Command) IsPendingOpen() bool {
	switch c {
	case CommandBuyStop, CommandBuyLimit, CommandSellStop, CommandSellLimit,
		CommandCancelLongBuyStop, CommandCancelLongBuyLimit,
		CommandCancelShortSellStop, CommandCancelShortSellLimit:
		return true
	default:
		return false
	}
}

func (c Command) IsModify() bool {
	switch c {
	case CommandNewSLTPLong, CommandNewSLTPShort, CommandNewSLTPBuyStop,
		CommandNewSLTPBuyLimit, CommandNewSLTPSellStop, CommandNewSLTPSellLimit:
		return true
	default:
		return false
	}
}

type ParamKind uint8

const (
	ParamUnknown ParamKind = iota
	ParamRisk
	ParamVolLots
	ParamVolDollar
	ParamVolPctBalanceLoss
	ParamVolPctEquityLoss
	ParamVolPctBalanceMargin
	ParamSL
	ParamSLPips
	ParamSLPrice
	ParamSLPct
	ParamTP
	ParamTPPips
	ParamTPPrice
	ParamTPPct
	ParamPending
	ParamEntryPrice
	ParamEntryPips
	ParamEntryPct
	ParamTrailTrigger
	ParamTrailDistance
	ParamTrailStep
	ParamATRTimeframe
	ParamATRPeriod
	ParamATRMultiplier
	ParamATRShift
	ParamATRTrigger
	ParamBETrigger
	ParamBEOffset
	ParamSecret
	ParamComment
	ParamSpread
	ParamAccountFilter
)

func (k ParamKind) String() string {
	switch k {
	case ParamRisk:
		return "risk"
	case ParamVolLots:
		return "vol_lots"
	case ParamVolDollar:
		return "vol_dollar"
	case ParamVolPctBalanceLoss:
		return "vol_pct_bal_loss"
	case ParamVolPctEquityLoss:
		return "vol_pct_eq_loss"
	case ParamVolPctBalanceMargin:
		return "vol_pct_bal_margin"
	case ParamSL:
		return "sl"
	case ParamSLPips:
		return "sl_pips"
	case ParamSLPrice:
		return "sl_price"
	case ParamSLPct:
		return "sl_pct"
	case ParamTP:
		return "tp"
	case ParamTPPips:
		return "tp_pips"
	case ParamTPPrice:
		return "tp_price"
	case ParamTPPct:
		return "tp_pct"
	case ParamPending:
		return "pending"
	case ParamEntryPrice:
		return "entry_price"
	case ParamEntryPips:
		return "entry_pips"
	case ParamEntryPct:
		return "entry_pct"
	case ParamTrailTrigger:
		return "trailtrig"
	case ParamTrailDistance:
		return "traildist"
	case ParamTrailStep:
		return "trailstep"
	case ParamATRTimeframe:
		return "atrtimeframe"
	case ParamATRPeriod:
		return "atrperiod"
	case ParamATRMultiplier:
		return "atrmultiplier"
	case ParamATRShift:
		return "atrshift"
	case ParamATRTrigger:
		return "atrtrigger"
	case ParamBETrigger:
		return "betrigger"
	case ParamBEOffset:
		return "beoffset"
	case ParamSecret:
		return "secret"
	case ParamComment:
		return "comment"
	case ParamSpread:
		return "spread"
	case ParamAccountFilter:
		return "accfilter"
	default:
		return "unknown"
	}
}

func (k ParamKind) IsVolume() bool {
	switch k {
	case ParamRisk, ParamVolLots, ParamVolDollar, ParamVolPctBalanceLoss,
		ParamVolPctEquityLoss, ParamVolPctBalanceMargin:
		return true
	default:
		return false
	}
}

func (k ParamKind) IsSL() bool {
	switch k {
	case ParamSL, ParamSLPips, ParamSLPrice, ParamSLPct:
		return true
	default:
		return false
	}
}

func (k ParamKind) IsTP() bool {
	switch k {
	case ParamTP, ParamTPPips, ParamTPPrice, ParamTPPct:
		return true
	default:
		return false
	}
}

func (k ParamKind) IsEntry() bool {
	switch k {
	case ParamPending, ParamEntryPrice, ParamEntryPips, ParamEntryPct:
		return true
	default:
		return false
	}
}

type Param struct {
	Kind  ParamKind
	Key   string
	Value string
}

type Signal struct {
	LicenseID  string
	Command    Command
	RawCommand string
	Symbol     string
	Params     [MaxParams]Param
	ParamCount int
}

func (s *Signal) Param(kind ParamKind) (Param, bool) {
	for i := 0; i < s.ParamCount; i++ {
		if s.Params[i].Kind == kind {
			return s.Params[i], true
		}
	}
	return Param{}, false
}

func (s *Signal) HasParam(kind ParamKind) bool {
	_, ok := s.Param(kind)
	return ok
}
