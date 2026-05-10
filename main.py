"""
Portal David — Bitget Futures Bot v2
Système adaptatif intelligent — TP/SL dynamiques, trailing stop, gestion capital progressive
Objectif: 94 USDT → 100,000 USDT
"""
import os, time, hmac, hashlib, base64, json, math, logging
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify, Response
from threading import Thread

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('PortalDavid')

# ── CONFIG ────────────────────────────────────────────────────────────
API_KEY    = os.environ.get('BITGET_API_KEY', '')
SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
BASE_URL   = 'https://api.bitget.com'

# ── RISK ENGINE — tout est dynamique ─────────────────────────────────
# Capital par tranche — le bot adapte sa stratégie
CAPITAL_TIERS = [
    # (max_capital, risk_pct, max_leverage, label)
    (200,    0.95, 10, 'Micro'),      # 94–200$   : agressif
    (500,    0.95, 15, 'Petit'),      # 200–500$  : très agressif
    (2000,   0.95, 20, 'Moyen'),      # 500–2k$   : maximum
    (10000,  0.95, 25, 'Croissance'), # 2k–10k$   : institutionnel
    (50000,  0.95, 25, 'Grand'),      # 10k–50k$  : élite
    (999999, 0.95, 25, 'Élite'),      # 50k+$     : élite
]

# TP/SL dynamiques selon la conviction (score)
# Format: (score_min, tp_pct, sl_pct, label)
CONVICTION_TIERS = [
    (85, 0.15, 0.05, 'Extrême'),   # score 85+  → TP 15%, SL 5%
    (75, 0.10, 0.04, 'Forte'),     # score 75+  → TP 10%, SL 4%
    (65, 0.07, 0.03, 'Bonne'),     # score 65+  → TP 7%,  SL 3%
    (55, 0.05, 0.025,'Modérée'),   # score 55+  → TP 5%,  SL 2.5% (partiel seulement)
]

SCAN_INTERVAL   = 45   # secondes
MIN_SCORE_ENTRY = 60   # score minimum pour entrer
MIN_VOLUME_24H  = 10_000_000  # volume min USDT
TRAILING_ACTIVE_AT = 0.03  # activer trailing après +3% de gain
TRAILING_DISTANCE  = 0.015 # trailing à 1.5% du plus haut

# ── STATE FILE ────────────────────────────────────────────────────────
STATE_FILE = '/tmp/state.json'

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {
            'status': 'Démarrage…',
            'balance': 0,
            'peak_balance': 94.36,
            'position': None,
            'history': [],
            'total_pnl': 0,
            'today_pnl': 0,
            'today_date': str(datetime.now(timezone.utc).date()),
            'last_scan': None,
            'signals_checked': 0,
            'consecutive_wins': 0,
            'consecutive_losses': 0,
            'total_trades': 0,
            'win_trades': 0,
            'tier': 'Micro',
            'mode': 'Normal',  # Normal, Aggressive, Protective
        }

def save_state(s):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(s, f, indent=2, default=str)
    except Exception as e:
        log.error(f'Save state error: {e}')

STATE = load_state()

# ── CAPITAL ENGINE ────────────────────────────────────────────────────
def get_capital_config(balance, state):
    for max_cap, risk, max_lev, label in CAPITAL_TIERS:
        if balance <= max_cap:
            # Ajuster selon la série
            mode = state.get('mode', 'Normal')
            if mode == 'Aggressive':
                risk = min(risk * 1.3, 0.12)
                max_lev = min(max_lev + 3, 30)
            elif mode == 'Protective':
                risk = risk * 0.5
                max_lev = max(max_lev - 3, 3)
            return {'risk': risk, 'max_leverage': max_lev, 'label': label}
    return {'risk': 0.05, 'max_leverage': 25, 'label': 'Élite'}

def get_conviction_config(score):
    for min_score, tp, sl, label in CONVICTION_TIERS:
        if score >= min_score:
            return {'tp': tp, 'sl': sl, 'label': label}
    return {'tp': 0.05, 'sl': 0.025, 'label': 'Faible'}

def update_mode(state):
    wins  = state.get('consecutive_wins', 0)
    losses= state.get('consecutive_losses', 0)
    if losses >= 2:
        state['mode'] = 'Protective'
        log.info('Mode: Protective (2 pertes consécutives)')
    elif wins >= 3:
        state['mode'] = 'Aggressive'
        log.info('Mode: Aggressive (3 gains consécutifs)')
    else:
        state['mode'] = 'Normal'
    return state

# ── BITGET API ────────────────────────────────────────────────────────
def sign(ts, method, path, body=''):
    msg = f'{ts}{method}{path}{body}'
    sig = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()

def make_headers(method, path, body=''):
    ts = str(int(time.time() * 1000))
    return {
        'ACCESS-KEY':        API_KEY,
        'ACCESS-SIGN':       sign(ts, method, path, body),
        'ACCESS-TIMESTAMP':  ts,
        'ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type':      'application/json',
        'locale':            'en-US',
    }

def api_get(path, params=None):
    qs = ('?' + '&'.join(f'{k}={v}' for k, v in params.items())) if params else ''
    try:
        r = requests.get(BASE_URL + path + qs, headers=make_headers('GET', path + qs), timeout=10)
        return r.json()
    except Exception as e:
        log.error(f'GET {path}: {e}')
        return {}

def api_post(path, body):
    b = json.dumps(body)
    try:
        r = requests.post(BASE_URL + path, headers=make_headers('POST', path, b), data=b, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f'POST {path}: {e}')
        return {}

# ── MARKET DATA ───────────────────────────────────────────────────────
def get_balance():
    try:
        r = api_get('/api/v2/mix/account/account', {'productType':'USDT-FUTURES','marginCoin':'USDT'})
        log.info(f'Balance ep1: {str(r)[:200]}')
        if r.get('code') == '00000' and r.get('data'):
            d = r['data']
            for k in ['available','availableAmount','crossedMaxAvailable']:
                if d.get(k) is not None and float(d.get(k,0)) >= 0:
                    log.info(f'Balance found via {k}: {d[k]}')
                    return float(d[k])
    except Exception as e:
        log.error(f'Balance ep1: {e}')
    try:
        r = api_get('/api/v2/mix/account/accounts', {'productType':'USDT-FUTURES'})
        log.info(f'Balance ep2: {str(r)[:200]}')
        if r.get('code') == '00000' and r.get('data'):
            for acc in r['data']:
                if acc.get('marginCoin','').upper() == 'USDT':
                    val = acc.get('available') or acc.get('availableAmount','0')
                    return float(val)
    except Exception as e:
        log.error(f'Balance ep2: {e}')
    return 0

def get_positions():
    try:
        r = api_get('/api/v2/mix/position/all-position', {'productType':'USDT-FUTURES','marginCoin':'USDT'})
        if r.get('code') == '00000':
            return [p for p in r['data'] if float(p.get('total',0)) > 0]
    except: pass
    return []

def get_tickers():
    try:
        r = api_get('/api/v2/mix/market/tickers', {'productType':'USDT-FUTURES'})
        if r.get('code') == '00000':
            return r['data']
    except: pass
    return []

def get_candles(symbol, gran, limit=100):
    try:
        r = api_get('/api/v2/mix/market/candles', {
            'symbol':symbol,'productType':'USDT-FUTURES',
            'granularity':gran,'limit':str(limit)
        })
        if r.get('code') == '00000':
            return r['data']
    except: pass
    return []

def get_open_interest(symbol):
    try:
        r = api_get('/api/v2/mix/market/open-interest', {'symbol':symbol,'productType':'USDT-FUTURES'})
        if r.get('code') == '00000':
            return float(r['data'].get('openInterestList',[{}])[0].get('size',0))
    except: pass
    return 0

def get_funding_rate(symbol):
    try:
        r = api_get('/api/v2/mix/market/current-fund-rate', {'symbol':symbol,'productType':'USDT-FUTURES'})
        if r.get('code') == '00000':
            return float(r['data'][0].get('fundingRate',0)) * 100
    except: pass
    return 0

def get_contract_info(symbol):
    try:
        r = api_get('/api/v2/mix/market/contracts', {'productType':'USDT-FUTURES','symbol':symbol})
        if r.get('code') == '00000' and r['data']:
            return r['data'][0]
    except: pass
    return None

# ── INDICATORS ────────────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100
    return 100 - (100 / (1 + ag/al))

def calc_ema(data, n):
    if not data: return []
    k = 2/(n+1)
    e = [data[0]]
    for v in data[1:]: e.append(v*k + e[-1]*(1-k))
    return e

def calc_macd(closes):
    if len(closes) < 35: return 0, 0, 0
    fast = calc_ema(closes, 12)
    slow = calc_ema(closes, 26)
    macd = [f-s for f,s in zip(fast,slow)]
    sig  = calc_ema(macd, 9)
    hist = macd[-1] - sig[-1]
    return macd[-1], sig[-1], hist

def calc_atr(candles, period=14):
    """Average True Range — mesure la volatilité réelle"""
    if len(candles) < period + 1: return 0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2])
        l = float(candles[i][3])
        pc= float(candles[i-1][4])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-period:]) / period

def calc_bollinger(closes, period=20, std_dev=2):
    if len(closes) < period: return 0, 0, 0
    recent = closes[-period:]
    mid = sum(recent) / period
    variance = sum((x-mid)**2 for x in recent) / period
    std = variance ** 0.5
    return mid + std_dev*std, mid, mid - std_dev*std

def detect_volume_spike(candles, lookback=20):
    if len(candles) < lookback+1: return 1.0
    vols = [float(c[5]) for c in candles]
    avg  = sum(vols[-lookback-1:-1]) / lookback
    last = vols[-1]
    return last / avg if avg > 0 else 1.0

def detect_breakout(closes, candles):
    """Détecte si le prix sort d'une consolidation"""
    if len(closes) < 30: return False, 0
    recent_high = max(closes[-20:-1])
    recent_low  = min(closes[-20:-1])
    current     = closes[-1]
    range_pct   = (recent_high - recent_low) / recent_low * 100

    # Breakout haussier
    if current > recent_high * 1.002 and range_pct < 8:
        return True, 1  # breakout long
    # Breakout baissier
    if current < recent_low * 0.998 and range_pct < 8:
        return True, -1  # breakout short
    return False, 0

# ── SCORING ENGINE ────────────────────────────────────────────────────
def score_symbol(ticker, c1m, c5m, c15m, c1h):
    score   = 0
    direction = 'long'
    reasons = []
    bonus_signals = []

    try:
        sym   = ticker.get('symbol','')
        price = float(ticker.get('lastPr', 0))
        vol24 = float(ticker.get('usdtVolume', 0))
        chg24 = float(ticker.get('change24h', 0)) * 100
        high24= float(ticker.get('high24h', price))
        low24 = float(ticker.get('low24h', price))

        if price <= 0 or vol24 < MIN_VOLUME_24H: return None

        # Extraire les closes de chaque TF
        def closes(candles): return [float(c[4]) for c in candles] if candles else []
        cl1m  = closes(c1m)
        cl5m  = closes(c5m)
        cl15m = closes(c15m)
        cl1h  = closes(c1h)

        # ── 1. RSI MULTI-TF (0-25pts) ──
        rsi1m  = calc_rsi(cl1m)  if len(cl1m)  > 15 else 50
        rsi5m  = calc_rsi(cl5m)  if len(cl5m)  > 15 else 50
        rsi15m = calc_rsi(cl15m) if len(cl15m) > 15 else 50
        rsi1h  = calc_rsi(cl1h)  if len(cl1h)  > 15 else 50

        # Survendu = opportunité long
        if rsi1m < 30 and rsi5m < 35:
            score += 25; direction = 'long'
            reasons.append(f'RSI survendu 1m:{rsi1m:.0f} 5m:{rsi5m:.0f}')
        elif rsi1m < 35 and rsi5m < 40:
            score += 18; direction = 'long'
            reasons.append(f'RSI bas 1m:{rsi1m:.0f}')
        # Surchauffé = opportunité short
        elif rsi1m > 75 and rsi5m > 70:
            score += 25; direction = 'short'
            reasons.append(f'RSI surachat 1m:{rsi1m:.0f}')
        elif rsi1m > 70 and rsi5m > 65:
            score += 18; direction = 'short'
            reasons.append(f'RSI élevé 1m:{rsi1m:.0f}')
        elif 42 <= rsi5m <= 58:
            score += 8

        # Alignement multi-TF
        if direction == 'long'  and rsi15m < 45 and rsi1h < 50:
            score += 10; reasons.append('RSI aligné multi-TF haussier')
        elif direction == 'short' and rsi15m > 55 and rsi1h > 50:
            score += 10; reasons.append('RSI aligné multi-TF baissier')

        # ── 2. MACD (0-20pts) ──
        _, _, hist5m = calc_macd(cl5m)
        _, _, hist15m= calc_macd(cl15m)
        if direction == 'long':
            if hist5m > 0 and hist15m > 0:
                score += 20; reasons.append('MACD haussier 5m+15m')
            elif hist5m > 0:
                score += 12
        else:
            if hist5m < 0 and hist15m < 0:
                score += 20; reasons.append('MACD baissier 5m+15m')
            elif hist5m < 0:
                score += 12

        # ── 3. BREAKOUT DETECTION (0-20pts) ──
        is_breakout, bo_dir = detect_breakout(cl15m, c15m)
        if is_breakout:
            if bo_dir == 1 and direction == 'long':
                score += 20
                reasons.append('BREAKOUT haussier détecté')
                bonus_signals.append('BREAKOUT')
            elif bo_dir == -1 and direction == 'short':
                score += 20
                reasons.append('BREAKOUT baissier détecté')
                bonus_signals.append('BREAKOUT')

        # ── 4. VOLUME SPIKE (0-15pts) ──
        vol_spike = detect_volume_spike(c5m)
        if vol_spike > 5:
            score += 15
            reasons.append(f'Volume x{vol_spike:.1f} explosif')
            bonus_signals.append(f'VOL x{vol_spike:.0f}')
        elif vol_spike > 3:
            score += 10
            reasons.append(f'Volume x{vol_spike:.1f}')
        elif vol_spike > 2:
            score += 6
        elif vol_spike < 0.5:
            score -= 8

        # ── 5. BOLLINGER BANDS (0-15pts) ──
        if len(cl1h) >= 20:
            bb_up, bb_mid, bb_low = calc_bollinger(cl1h)
            if direction == 'long'  and price < bb_low * 1.002:
                score += 15; reasons.append('Prix sous bande Bollinger basse')
                bonus_signals.append('BB OVERSOLD')
            elif direction == 'short' and price > bb_up * 0.998:
                score += 15; reasons.append('Prix au-dessus bande Bollinger haute')
                bonus_signals.append('BB OVERBOUGHT')
            elif bb_low < price < bb_mid and direction == 'long':
                score += 8

        # ── 6. ATR — VOLATILITÉ (bonus/malus) ──
        atr5m = calc_atr(c5m)
        atr_pct = (atr5m / price * 100) if price > 0 else 0
        if 0.3 <= atr_pct <= 2.0:
            score += 8  # volatilité idéale
        elif atr_pct > 4:
            score -= 10  # trop volatile = risqué

        # ── 7. POSITION DANS RANGE 24H (0-10pts) ──
        if high24 > low24:
            pos_range = (price - low24) / (high24 - low24)
            if direction == 'long'  and pos_range < 0.25:
                score += 10; reasons.append('Prix bas du range 24h')
            elif direction == 'short' and pos_range > 0.75:
                score += 10; reasons.append('Prix haut du range 24h')
            elif 0.3 <= pos_range <= 0.7:
                score += 5

        # ── 8. FUNDING RATE (0-10pts) ──
        funding = get_funding_rate(sym)
        if direction == 'long'  and funding < -0.02:
            score += 10; reasons.append(f'Funding {funding:.3f}% — longs favorisés')
            bonus_signals.append('FUNDING+')
        elif direction == 'short' and funding > 0.02:
            score += 10; reasons.append(f'Funding {funding:.3f}% — shorts favorisés')
            bonus_signals.append('FUNDING+')
        elif abs(funding) > 0.1:
            score -= 15  # funding extrême = danger

        # ── MALUS ──
        # Déjà trop pumpé sans volume
        if chg24 > 25 and vol_spike < 1.5 and direction == 'long':
            score -= 20
        # En chute libre
        if chg24 < -20 and direction == 'long':
            score -= 15

    except Exception as e:
        log.warning(f'Score error {ticker.get("symbol","?")}: {e}')
        return None

    final_score = min(100, max(0, round(score)))

    return {
        'score':     final_score,
        'direction': direction,
        'reasons':   reasons[:4],
        'bonus':     bonus_signals,
        'atr_pct':   round(atr_pct, 3) if 'atr_pct' in dir() else 0,
        'vol_spike': round(vol_spike, 2),
        'price':     price,
        'chg24':     chg24,
        'vol24':     vol24,
        'rsi5m':     round(rsi5m, 1) if 'rsi5m' in dir() else 50,
        'funding':   round(funding, 4) if 'funding' in dir() else 0,
    }

# ── SET LEVERAGE ──────────────────────────────────────────────────────
def set_leverage(symbol, leverage):
    for side in ['long','short']:
        api_post('/api/v2/mix/account/set-leverage', {
            'symbol':symbol,'productType':'USDT-FUTURES',
            'marginCoin':'USDT','leverage':str(leverage),'holdSide':side
        })
    time.sleep(0.3)

# ── PLACE ORDER ───────────────────────────────────────────────────────
def place_order(symbol, direction, balance, score_result, state):
    try:
        cap_cfg  = get_capital_config(balance, state)
        conv_cfg = get_conviction_config(score_result['score'])
        atr_pct  = score_result.get('atr_pct', 0.5)

        # Levier adaptatif selon conviction ET volatilité
        base_lev = cap_cfg['max_leverage']
        score    = score_result['score']
        if score >= 85:
            leverage = base_lev
        elif score >= 75:
            leverage = max(int(base_lev * 0.9), 10)
        else:
            leverage = max(int(base_lev * 0.8), 10)

        # Réduire levier si volatilité extrême seulement
        if atr_pct > 3.0:
            leverage = max(int(leverage * 0.75), 10)

        set_leverage(symbol, leverage)
        time.sleep(0.3)

        # Prix actuel
        tk = api_get('/api/v2/mix/market/ticker', {'symbol':symbol,'productType':'USDT-FUTURES'})
        if tk.get('code') != '00000': return None
        price = float(tk['data'][0]['lastPr'])

        # Taille position
        risk_usdt = balance * 0.95  # 95% du capital total
        pos_value = risk_usdt * leverage
        info      = get_contract_info(symbol)
        size_dec  = int(info.get('volumePlace',0)) if info else 1
        min_size  = float(info.get('minTradeNum',0.001)) if info else 0.001

        size = pos_value / price
        size = max(size, min_size)
        size = round(size, size_dec) if size_dec > 0 else max(1, int(size))

        # TP/SL dynamiques basés sur ATR + conviction
        tp_pct = conv_cfg['tp']
        sl_pct = conv_cfg['sl']

        # Ajuster avec l'ATR — si très volatile, TP plus large
        if atr_pct > 1.0:
            tp_pct = tp_pct * (1 + atr_pct * 0.3)
            sl_pct = sl_pct * (1 + atr_pct * 0.2)

        # Bonus breakout — laisser courir plus loin
        if 'BREAKOUT' in score_result.get('bonus',[]):
            tp_pct *= 1.5

        # Limiter les extremes
        tp_pct = min(tp_pct, 0.35)
        sl_pct = min(sl_pct, 0.08)

        if direction == 'long':
            tp_price = round(price * (1 + tp_pct), 6)
            sl_price = round(price * (1 - sl_pct), 6)
        else:
            tp_price = round(price * (1 - tp_pct), 6)
            sl_price = round(price * (1 + sl_pct), 6)

        side     = 'buy'  if direction == 'long'  else 'sell'

        order = {
            'symbol':      symbol,
            'productType': 'USDT-FUTURES',
            'marginMode':  'isolated',
            'marginCoin':  'USDT',
            'size':        str(size),
            'side':        side,
            'tradeSide':   'open',
            'orderType':   'market',
            'presetStopSurplusPrice': str(tp_price),
            'presetStopLossPrice':    str(sl_price),
        }

        r = api_post('/api/v2/mix/order/place-order', order)
        log.info(f'Order: {r}')

        if r.get('code') == '00000':
            return {
                'orderId':    r['data']['orderId'],
                'symbol':     symbol,
                'direction':  direction,
                'entryPrice': price,
                'currentPrice': price,
                'size':       size,
                'leverage':   leverage,
                'tp':         tp_price,
                'sl':         sl_price,
                'tp_pct':     round(tp_pct*100,2),
                'sl_pct':     round(sl_pct*100,2),
                'margin':     round(risk_usdt, 4),
                'openTime':   datetime.now(timezone.utc).isoformat(),
                'scoreAtEntry': score_result['score'],
                'reasons':    score_result['reasons'],
                'bonus':      score_result.get('bonus',[]),
                'conviction': conv_cfg['label'],
                'tier':       cap_cfg['label'],
                'trailing_high': price,
                'trailing_active': False,
                'unrealizedPnl': 0,
            }
        else:
            log.error(f'Order failed: {r}')
            return None

    except Exception as e:
        log.error(f'Place order error: {e}')
        return None

# ── TRAILING STOP ─────────────────────────────────────────────────────
def update_trailing_stop(pos, cur_price):
    """Met à jour le trailing stop si le gain dépasse le seuil"""
    if not pos: return pos, False
    try:
        entry = pos['entryPrice']
        direction = pos['direction']

        if direction == 'long':
            gain_pct = (cur_price - entry) / entry
            if gain_pct >= TRAILING_ACTIVE_AT:
                pos['trailing_active'] = True
                if cur_price > pos.get('trailing_high', entry):
                    pos['trailing_high'] = cur_price
                    new_sl = cur_price * (1 - TRAILING_DISTANCE)
                    if new_sl > pos['sl']:
                        pos['sl'] = round(new_sl, 6)
                        log.info(f'Trailing SL updated to {new_sl:.6f}')
                        return pos, True
        else:
            gain_pct = (entry - cur_price) / entry
            if gain_pct >= TRAILING_ACTIVE_AT:
                pos['trailing_active'] = True
                if cur_price < pos.get('trailing_high', entry):
                    pos['trailing_high'] = cur_price
                    new_sl = cur_price * (1 + TRAILING_DISTANCE)
                    if new_sl < pos['sl']:
                        pos['sl'] = round(new_sl, 6)
                        return pos, True
    except Exception as e:
        log.warning(f'Trailing error: {e}')
    return pos, False

# ── CHECK POSITION ────────────────────────────────────────────────────
def check_position(state):
    if not state['position']: return state

    positions = get_positions()
    sym = state['position']['symbol']
    open_pos = next((p for p in positions if p['symbol'] == sym), None)

    if not open_pos:
        # Position fermée — récupérer solde réel
        bal_new = get_balance()
        pnl     = round(bal_new - state['balance'], 4)
        pos     = state['position']

        result = {
            **pos,
            'closeTime':    datetime.now(timezone.utc).isoformat(),
            'pnl':          pnl,
            'pnlPct':       round((pnl / pos['margin']) * 100, 2) if pos.get('margin') else 0,
            'closeBalance': round(bal_new, 4),
            'exitReason':   'TP/SL/Trailing auto',
        }
        state['history'].insert(0, result)
        if len(state['history']) > 100:
            state['history'] = state['history'][:100]

        state['total_pnl'] = round(state.get('total_pnl', 0) + pnl, 4)

        today = str(datetime.now(timezone.utc).date())
        if state.get('today_date') != today:
            state['today_pnl'] = 0
            state['today_date'] = today
        state['today_pnl'] = round(state.get('today_pnl', 0) + pnl, 4)

        state['total_trades'] = state.get('total_trades', 0) + 1
        if pnl > 0:
            state['win_trades']          = state.get('win_trades', 0) + 1
            state['consecutive_wins']    = state.get('consecutive_wins', 0) + 1
            state['consecutive_losses']  = 0
            if bal_new > state.get('peak_balance', 0):
                state['peak_balance'] = round(bal_new, 4)
        else:
            state['consecutive_losses'] = state.get('consecutive_losses', 0) + 1
            state['consecutive_wins']   = 0

        state['position'] = None
        state['balance']  = round(bal_new, 4)
        state = update_mode(state)
        state['status'] = f'{"Gain" if pnl>0 else "Perte"}: {pnl:+.4f} USDT — Mode: {state["mode"]}'
        log.info(f'Trade closed: PNL={pnl:.4f} USDT | New balance: {bal_new}')

    else:
        # Position ouverte — sync depuis Bitget (source unique de vérité)
        unr       = float(open_pos.get('unrealizedPL', 0))
        cur_price = float(open_pos.get('markPrice', state['position']['entryPrice']))
        entry     = float(open_pos.get('openPriceAvg', state['position']['entryPrice']))
        margin    = float(open_pos.get('marginSize', state['position'].get('margin', 0)))
        leverage  = int(float(open_pos.get('leverage', state['position'].get('leverage', 10))))
        total_val = float(open_pos.get('total', 0))
        liq_price = float(open_pos.get('liquidationPrice', 0))

        # Sync toutes les valeurs depuis Bitget
        state['position']['unrealizedPnl'] = round(unr, 6)
        state['position']['currentPrice']  = cur_price
        state['position']['entryPrice']    = entry
        state['position']['margin']        = round(margin, 4)
        state['position']['leverage']      = leverage
        state['position']['liqPrice']      = liq_price
        state['position']['totalSize']     = total_val

        # Solde total = disponible + marge + PNL non réalisé
        avail = get_balance()
        state['balance_total'] = round(avail + margin + unr, 4)
        state['balance'] = round(avail, 4)

        # Trailing stop
        state['position'], updated = update_trailing_stop(state['position'], cur_price)
        if updated:
            try:
                api_post('/api/v2/mix/order/modify-tpsl-order', {
                    'symbol':               sym,
                    'productType':          'USDT-FUTURES',
                    'marginCoin':           'USDT',
                    'stopLossTriggerPrice': str(state['position']['sl']),
                    'holdSide':             state['position']['direction'],
                })
            except: pass

    return state

# ── MAIN SCAN ─────────────────────────────────────────────────────────
def scan_and_trade(state):
    state['last_scan'] = datetime.now(timezone.utc).isoformat()

    bal = get_balance()
    if bal > 0:
        state['balance'] = round(bal, 4)
        if 'balance_total' not in state or not state.get('position'):
            state['balance_total'] = round(bal, 4)
        cap_cfg = get_capital_config(bal, state)
        state['tier'] = cap_cfg['label']

    today = str(datetime.now(timezone.utc).date())
    if state.get('today_date') != today:
        state['today_pnl'] = 0
        state['today_date'] = today

    # Si position ouverte → juste surveiller
    if state['position']:
        state = check_position(state)
        pos   = state['position']
        if pos:
            state['status'] = (
                f'📊 {pos["symbol"]} {pos["direction"].upper()} x{pos["leverage"]} — '
                f'PNL: {pos.get("unrealizedPnl",0):+.4f} USDT'
                + (' 🎯 Trailing actif' if pos.get('trailing_active') else '')
            )
        return state

    # Scan du marché
    state['status'] = '🔍 Analyse du marché en cours…'
    tickers = get_tickers()
    if not tickers:
        state['status'] = '⚠️ Erreur API marché'
        return state

    # Top 25 par volume
    top_tickers = sorted(tickers, key=lambda x: float(x.get('usdtVolume',0)), reverse=True)[:25]

    candidates = []
    for ticker in top_tickers:
        sym = ticker.get('symbol','')
        if not sym.endswith('USDT') or sym in ['USDCUSDT','TUSDUSDT','BUSDUSDT']: continue

        state['signals_checked'] = state.get('signals_checked',0) + 1

        c1m  = get_candles(sym, '1m',  100)
        time.sleep(0.08)
        c5m  = get_candles(sym, '5m',  100)
        time.sleep(0.08)
        c15m = get_candles(sym, '15m', 60)
        time.sleep(0.08)
        c1h  = get_candles(sym, '1H',  50)
        time.sleep(0.08)

        result = score_symbol(ticker, c1m, c5m, c15m, c1h)
        if result and result['score'] >= MIN_SCORE_ENTRY:
            candidates.append({'symbol':sym, **result})
            log.info(f'Candidate: {sym} score={result["score"]} dir={result["direction"]}')

    state['status'] = f'Scan terminé — {len(candidates)} signaux sur {len(top_tickers)} paires'

    if not candidates:
        state['status'] = '⏳ Aucun signal fort — surveillance continue'
        return state

    # Meilleur candidat
    best = sorted(candidates, key=lambda x: x['score'], reverse=True)[0]
    state['status'] = f'🎯 Signal: {best["symbol"]} ({best["direction"].upper()}) {best["score"]}/100 — Ouverture…'
    log.info(f'Best signal: {best}')

    pos = place_order(best['symbol'], best['direction'], state['balance'], best, state)
    if pos:
        state['position'] = pos
        state['status'] = (
            f'✅ {pos["symbol"]} {pos["direction"].upper()} x{pos["leverage"]} — '
            f'TP: +{pos["tp_pct"]}% | SL: -{pos["sl_pct"]}% | '
            f'Conviction: {pos["conviction"]}'
        )
    else:
        state['status'] = f'⚠️ Échec ordre sur {best["symbol"]} — prochaine tentative'

    return state

# ── FLASK API ─────────────────────────────────────────────────────────
app = Flask(__name__)

def cors(data):
    resp = Response(json.dumps(data, default=str), mimetype='application/json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/')
def root():
    return cors({'status': 'Portal David Bot — Running', 'version': '2.0'})

@app.route('/api/state')
def api_state():
    return cors(load_state())

@app.route('/api/health')
def api_health():
    return cors({'ok': True, 'ts': datetime.now(timezone.utc).isoformat()})

# ── BOT LOOP ──────────────────────────────────────────────────────────
def bot_loop():
    global STATE
    log.info('Portal David Bot v2 starting…')
    time.sleep(8)

    while True:
        try:
            STATE = scan_and_trade(STATE)
            save_state(STATE)
        except Exception as e:
            log.error(f'Bot loop error: {e}')
            STATE['status'] = f'❌ Erreur: {str(e)[:100]}'
            save_state(STATE)
        time.sleep(SCAN_INTERVAL)

# ── DÉMARRAGE DU BOT — compatible gunicorn ───────────────────────────
# Le thread démarre dès que le module est importé (gunicorn importe main)
_bot_thread = Thread(target=bot_loop, daemon=True)
_bot_thread.start()
log.info('Bot thread started via module import')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
