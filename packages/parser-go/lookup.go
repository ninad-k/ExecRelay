package parser

func commandFrom(raw string) Command {
	switch {
	case eqFoldASCII(raw, "buy"), eqFoldASCII(raw, "long"), eqFoldASCII(raw, "bull"), eqFoldASCII(raw, "bullish"):
		return CommandBuy
	case eqFoldASCII(raw, "sell"), eqFoldASCII(raw, "short"), eqFoldASCII(raw, "bear"), eqFoldASCII(raw, "bearish"):
		return CommandSell
	case eqFoldASCII(raw, "buystop"):
		return CommandBuyStop
	case eqFoldASCII(raw, "buylimit"):
		return CommandBuyLimit
	case eqFoldASCII(raw, "sellstop"):
		return CommandSellStop
	case eqFoldASCII(raw, "selllimit"):
		return CommandSellLimit
	case eqFoldASCII(raw, "closeall"):
		return CommandCloseAll
	case eqFoldASCII(raw, "cancellong"):
		return CommandCancelLong
	case eqFoldASCII(raw, "cancelshort"):
		return CommandCancelShort
	case eqFoldASCII(raw, "closelong"):
		return CommandCloseLong
	case eqFoldASCII(raw, "closeshort"):
		return CommandCloseShort
	case eqFoldASCII(raw, "closelongshort"):
		return CommandCloseLongShort
	case eqFoldASCII(raw, "closelongpct"):
		return CommandCloseLongPct
	case eqFoldASCII(raw, "closeshortpct"):
		return CommandCloseShortPct
	case eqFoldASCII(raw, "closelongvol"):
		return CommandCloseLongVol
	case eqFoldASCII(raw, "closeshortvol"):
		return CommandCloseShortVol
	case eqFoldASCII(raw, "newsltplong"):
		return CommandNewSLTPLong
	case eqFoldASCII(raw, "newsltpshort"):
		return CommandNewSLTPShort
	case eqFoldASCII(raw, "newsltpbuystop"):
		return CommandNewSLTPBuyStop
	case eqFoldASCII(raw, "newsltpbuylimit"):
		return CommandNewSLTPBuyLimit
	case eqFoldASCII(raw, "newsltpsellstop"):
		return CommandNewSLTPSellStop
	case eqFoldASCII(raw, "newsltpselllimit"):
		return CommandNewSLTPSellLimit
	case eqFoldASCII(raw, "closelongopenlong"), eqFoldASCII(raw, "cl+ol"):
		return CommandCloseLongOpenLong
	case eqFoldASCII(raw, "closelongopenshort"), eqFoldASCII(raw, "cl+os"):
		return CommandCloseLongOpenShort
	case eqFoldASCII(raw, "closeshortopenlong"), eqFoldASCII(raw, "cs+ol"):
		return CommandCloseShortOpenLong
	case eqFoldASCII(raw, "closeshortopenshort"), eqFoldASCII(raw, "cs+os"):
		return CommandCloseShortOpenShort
	case eqFoldASCII(raw, "closelongshortopenlong"), eqFoldASCII(raw, "cls+ol"):
		return CommandCloseLongShortOpenLong
	case eqFoldASCII(raw, "closelongshortopenshort"), eqFoldASCII(raw, "cls+os"):
		return CommandCloseLongShortOpenShort
	case eqFoldASCII(raw, "cancellongbuystop"):
		return CommandCancelLongBuyStop
	case eqFoldASCII(raw, "cancellongbuylimit"):
		return CommandCancelLongBuyLimit
	case eqFoldASCII(raw, "cancelshortsellstop"):
		return CommandCancelShortSellStop
	case eqFoldASCII(raw, "cancelshortselllimit"):
		return CommandCancelShortSellLimit
	case eqFoldASCII(raw, "eaoff"):
		return CommandEAOff
	case eqFoldASCII(raw, "eaon"):
		return CommandEAOn
	case eqFoldASCII(raw, "closealleaoff"):
		return CommandCloseAllEAOff
	default:
		return CommandInvalid
	}
}

func paramKindFromKey(key string) ParamKind {
	switch {
	case eqFoldASCII(key, "risk"):
		return ParamRisk
	case eqFoldASCII(key, "vol_lots"):
		return ParamVolLots
	case eqFoldASCII(key, "vol_dollar"):
		return ParamVolDollar
	case eqFoldASCII(key, "vol_pct_bal_loss"):
		return ParamVolPctBalanceLoss
	case eqFoldASCII(key, "vol_pct_eq_loss"):
		return ParamVolPctEquityLoss
	case eqFoldASCII(key, "vol_pct_bal_margin"):
		return ParamVolPctBalanceMargin
	case eqFoldASCII(key, "sl"):
		return ParamSL
	case eqFoldASCII(key, "sl_pips"):
		return ParamSLPips
	case eqFoldASCII(key, "sl_price"):
		return ParamSLPrice
	case eqFoldASCII(key, "sl_pct"):
		return ParamSLPct
	case eqFoldASCII(key, "tp"):
		return ParamTP
	case eqFoldASCII(key, "tp_pips"):
		return ParamTPPips
	case eqFoldASCII(key, "tp_price"):
		return ParamTPPrice
	case eqFoldASCII(key, "tp_pct"):
		return ParamTPPct
	case eqFoldASCII(key, "pending"):
		return ParamPending
	case eqFoldASCII(key, "entry_price"), eqFoldASCII(key, "price"):
		return ParamEntryPrice
	case eqFoldASCII(key, "entry_pips"):
		return ParamEntryPips
	case eqFoldASCII(key, "entry_pct"):
		return ParamEntryPct
	case eqFoldASCII(key, "trailtrig"):
		return ParamTrailTrigger
	case eqFoldASCII(key, "traildist"):
		return ParamTrailDistance
	case eqFoldASCII(key, "trailstep"):
		return ParamTrailStep
	case eqFoldASCII(key, "atrtimeframe"):
		return ParamATRTimeframe
	case eqFoldASCII(key, "atrperiod"):
		return ParamATRPeriod
	case eqFoldASCII(key, "atrmultiplier"):
		return ParamATRMultiplier
	case eqFoldASCII(key, "atrshift"):
		return ParamATRShift
	case eqFoldASCII(key, "atrtrigger"):
		return ParamATRTrigger
	case eqFoldASCII(key, "betrigger"):
		return ParamBETrigger
	case eqFoldASCII(key, "beoffset"):
		return ParamBEOffset
	case eqFoldASCII(key, "secret"):
		return ParamSecret
	case eqFoldASCII(key, "comment"):
		return ParamComment
	case eqFoldASCII(key, "spread"):
		return ParamSpread
	case eqFoldASCII(key, "accfilter"):
		return ParamAccountFilter
	default:
		return ParamUnknown
	}
}

func eqFoldASCII(value, want string) bool {
	if len(value) != len(want) {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if 'A' <= c && c <= 'Z' {
			c += 'a' - 'A'
		}
		if c != want[i] {
			return false
		}
	}
	return true
}
