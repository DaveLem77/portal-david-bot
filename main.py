"""
Portal David — Bitget Futures Bot v3
TP/SL fiables, levier x15-20, vise 3-8% de move = 45-160% sur marge
"""
import os, time, hmac, hashlib, base64, json, math, logging
from datetime import datetime, timezone
import requests
from flask import Flask, Response
from threading import Thread

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('PD')

# ══ CONFIG ════════════════════════════════════════════════════════════
API_KEY    = os.environ.get('BITGET_API_KEY', '')
SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
BASE       = 'https://api.bitget.com'

LEVERAGE      = 15       # levier de base (sera overridé dynamiquement)
RISK_PCT      = 0.55     # 55% du capital par trade
TP_PCT        = 0.04     # take profit +4% — plus réaliste
SL_PCT        = 0.035    # stop loss -3.5% — sécuritaire avec x15 (liq à ~6.5%)
MIN_SCORE     = 70       # score minimum — signaux solides seulement
SCAN_SEC      = 30       # scan toutes les 30 secondes
MIN_VOL_24H   = 8_000_000  # volume minimum USDT

# Trailing stop — laisse les gros moves se développer
REQUIRE_TREND = True     # exiger confirmation de tendance 1h
TRAIL_START   = 0.05     # trailing commence à +5%
TRAIL_STEP1   = 0.05     # à +5%:  SL monte à breakeven
TRAIL_STEP2   = 0.10     # à +10%: SL monte à +4%
TRAIL_STEP3   = 0.20     # à +20%: SL monte à +12%
TRAIL_STEP4   = 0.35     # à +35%: SL monte à +25%
MAX_GAIN_PCT  = 0.70     # sortie forcée à +70% seulement


import urllib.request as _ur
def fetch_cad_rate():
    """Récupère le taux USD/CAD depuis une API publique"""
    try:
        url = 'https://api.exchangerate-api.com/v4/latest/USD'
        with _ur.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            return float(data['rates']['CAD'])
    except:
        return 1.3650  # fallback

STATE_FILE = '/tmp/pdv4.json'  # v4 — nouveau départ 175$
# ══ TWILIO SMS ════════════════════════════════════════════════════════
TWILIO_SID   = os.environ.get('TWILIO_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_TOKEN', '')
TWILIO_FROM  = '+15794851777'
TWILIO_TO    = '+18733391815'

def send_sms(msg):
    """Envoie un SMS via Twilio"""
    if not TWILIO_SID or not TWILIO_TOKEN:
        log.warning('Twilio not configured — SMS skipped')
        return
    try:
        import base64
        url  = f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json'
        auth = base64.b64encode(f'{TWILIO_SID}:{TWILIO_TOKEN}'.encode()).decode()
        data = f'To={TWILIO_TO}&From={TWILIO_FROM}&Body={msg}'
        req  = __import__('urllib.request', fromlist=['urlopen', 'Request'])
        r    = req.urlopen(
            req.Request(url, data=data.encode(), headers={
                'Authorization': f'Basic {auth}',
                'Content-Type':  'application/x-www-form-urlencoded'
            }), timeout=10
        )
        log.info(f'SMS sent: {msg[:60]}')
    except Exception as e:
        log.warning(f'SMS failed: {e}')



# ══ STATE ══════════════════════════════════════════════════════════════
def empty_state():
    return {
        'status':            'Démarrage…',
        'balance':           175.0,
        'balance_total':     175.0,
        'initial_balance':   94.36,
        'position':          None,
        'history':           [],
        'total_pnl':         0.0,
        'today_pnl':         0.0,
        'today_date':        str(datetime.now(timezone.utc).date()),
        'signals_checked':   0,
        'total_trades':      0,
        'win_trades':        0,
        'consecutive_wins':  0,
        'consecutive_losses':0,
        'mode':              'Normal',
        'last_scan':         None,
        'score_weights':     {
            'rsi': 1.0, 'macd': 1.0, 'volume': 1.0,
            'breakout': 1.0, 'range': 1.0, 'funding': 1.0
        }
    }

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            # Assurer que tous les champs existent
            base = empty_state()
            base.update(s)
            return base
    except:
        return empty_state()

def save_state(s):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(s, f, default=str)
    except Exception as e:
        log.error(f'Save: {e}')

S = load_state()

# ══ BITGET API ══════════════════════════════════════════════════════════
def _sign(ts, method, path, body=''):
    msg = f'{ts}{method}{path}{body}'
    return base64.b64encode(
        hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def _hdrs(method, path, body=''):
    ts = str(int(time.time() * 1000))
    return {
        'ACCESS-KEY':        API_KEY,
        'ACCESS-SIGN':       _sign(ts, method, path, body),
        'ACCESS-TIMESTAMP':  ts,
        'ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type':      'application/json',
        'locale':            'en-US',
    }

def GET(path, params=None):
    qs = ('?' + '&'.join(f'{k}={v}' for k,v in params.items())) if params else ''
    try:
        r = requests.get(BASE + path + qs, headers=_hdrs('GET', path + qs), timeout=12)
        return r.json()
    except Exception as e:
        log.error(f'GET {path}: {e}')
        return {}

def POST(path, body):
    b = json.dumps(body)
    try:
        r = requests.post(BASE + path, headers=_hdrs('POST', path, b), data=b, timeout=12)
        return r.json()
    except Exception as e:
        log.error(f'POST {path}: {e}')
        return {}

# ══ MARKET DATA ══════════════════════════════════════════════════════════
def get_balance():
    # Endpoint 1
    r = GET('/api/v2/mix/account/accounts', {'productType': 'USDT-FUTURES'})
    if r.get('code') == '00000':
        for acc in (r.get('data') or []):
            if acc.get('marginCoin', '').upper() == 'USDT':
                val = acc.get('available') or acc.get('crossedMaxAvailable') or '0'
                v = float(val)
                if v > 0:
                    log.info(f'Balance: {v} USDT')
                    return v
    # Endpoint 2
    r2 = GET('/api/v2/mix/account/account', {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'})
    if r2.get('code') == '00000' and r2.get('data'):
        val = r2['data'].get('available', '0')
        v = float(val)
        log.info(f'Balance ep2: {v}')
        return v
    log.warning(f'Balance failed: {r}')
    return 0.0

def get_positions():
    r = GET('/api/v2/mix/position/all-position', {
        'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'
    })
    if r.get('code') == '00000':
        return [p for p in (r.get('data') or []) if float(p.get('total', 0)) > 0]
    return []

def get_tickers():
    r = GET('/api/v2/mix/market/tickers', {'productType': 'USDT-FUTURES'})
    if r.get('code') == '00000':
        return r.get('data', [])
    return []

def get_candles(symbol, gran, limit=100):
    r = GET('/api/v2/mix/market/candles', {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'granularity': gran, 'limit': str(limit)
    })
    if r.get('code') == '00000':
        return r.get('data', [])
    return []

def get_funding(symbol):
    r = GET('/api/v2/mix/market/current-fund-rate', {
        'symbol': symbol, 'productType': 'USDT-FUTURES'
    })
    try:
        return float(r['data'][0].get('fundingRate', 0)) * 100
    except:
        return 0.0

# ══ ADVANCED SIGNALS — ce que les autres bots ne font pas ══════════════

def get_open_interest(symbol):
    """Open interest — mesure l'argent réel dans le marché"""
    r = GET('/api/v2/mix/market/open-interest', {
        'symbol': symbol, 'productType': 'USDT-FUTURES'
    })
    try:
        items = r.get('data', {}).get('openInterestList', [])
        if items:
            return float(items[0].get('size', 0))
    except:
        pass
    return 0.0

def get_orderbook_imbalance(symbol):
    """
    Déséquilibre carnet d'ordres — si 3x plus d'achats que de ventes
    dans le top 5 niveaux = pression d'achat forte
    """
    r = GET('/api/v2/mix/market/merge-depth', {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'precision': 'scale0', 'limit': '10'
    })
    try:
        d    = r.get('data', {})
        bids = d.get('bids', [])  # achats
        asks = d.get('asks', [])  # ventes
        if not bids or not asks:
            return 0.0
        bid_vol = sum(float(b[1]) for b in bids[:5])
        ask_vol = sum(float(a[1]) for a in asks[:5])
        if ask_vol == 0:
            return 3.0
        return bid_vol / ask_vol  # >1.5 = pression achat, <0.7 = pression vente
    except:
        return 1.0

def get_liquidation_data(symbol):
    """
    Données de liquidation — si beaucoup de shorts liquidés = squeeze haussier
    Retourne: (longs_liquidés, shorts_liquidés) en USDT
    """
    r = GET('/api/v2/mix/market/liquidation-order', {
        'symbol': symbol, 'productType': 'USDT-FUTURES'
    })
    try:
        data = r.get('data', {}).get('liquidationOrderList', [])
        longs_liq  = sum(float(x.get('size', 0)) * float(x.get('price', 0))
                        for x in data if x.get('side') == 'buy')
        shorts_liq = sum(float(x.get('size', 0)) * float(x.get('price', 0))
                        for x in data if x.get('side') == 'sell')
        return longs_liq, shorts_liq
    except:
        return 0.0, 0.0

def get_btc_momentum():
    """
    Momentum BTC sur 5 minutes — si BTC pompe fort,
    les alts suivent avec 5-15 min de retard
    """
    r = GET('/api/v2/mix/market/candles', {
        'symbol': 'BTCUSDT', 'productType': 'USDT-FUTURES',
        'granularity': '1m', 'limit': '10'
    })
    try:
        candles = r.get('data', [])
        if len(candles) < 5:
            return 0.0
        opens  = [float(c[1]) for c in candles[-5:]]
        closes = [float(c[4]) for c in candles[-5:]]
        chg    = (closes[-1] - opens[0]) / opens[0] * 100
        return chg
    except:
        return 0.0

def advanced_score_boost(symbol, direction, base_score):
    """
    Boost le score avec les signaux avancés.
    Retourne (boost_pts, advanced_reasons)
    """
    boost = 0
    reasons = []

    try:
        # 1. ORDER BOOK IMBALANCE
        imbalance = get_orderbook_imbalance(symbol)
        if direction == 'long' and imbalance > 2.0:
            boost += 15
            reasons.append(f"Carnet d'ordres: {imbalance:.1f}x plus d'acheteurs que vendeurs")
        elif direction == 'long' and imbalance > 1.5:
            boost += 8
        elif direction == 'short' and imbalance < 0.5:
            boost += 15
            reasons.append("Carnet d'ordres: pression vendeuse massive")
        elif direction == 'long' and imbalance < 0.6:
            boost -= 10  # contre-signal
        time.sleep(0.05)

        # 2. LIQUIDATION CASCADE DETECTION
        longs_liq, shorts_liq = get_liquidation_data(symbol)
        if direction == 'long' and shorts_liq > longs_liq * 2 and shorts_liq > 50000:
            boost += 18
            reasons.append(f'Cascade de liquidations shorts — squeeze haussier probable')
        elif direction == 'short' and longs_liq > shorts_liq * 2 and longs_liq > 50000:
            boost += 18
            reasons.append('Cascade de liquidations longs — dump probable')
        time.sleep(0.05)

        # 3. OPEN INTEREST CONFIRMATION
        # On compare avec 30 min avant via les candles OI
        # Si prix monte ET OI monte = vrais acheteurs
        # Si prix monte ET OI baisse = fermeture de shorts seulement (moins fiable)
        # Simplifié: on check juste la valeur absolue
        oi = get_open_interest(symbol)
        if oi > 5_000_000 and direction == 'long':
            boost += 8
            reasons.append(f'Open interest élevé — liquidité confirmée')
        elif oi > 1_000_000:
            boost += 4
        time.sleep(0.05)

        # 4. BTC CORRELATION PLAY
        btc_mom = get_btc_momentum()
        if abs(btc_mom) > 0.8:  # BTC bouge de +0.8% en 5min
            if btc_mom > 0 and direction == 'long':
                boost += 12
                reasons.append(f'BTC +{btc_mom:.2f}% sur 5min — vague haussière en cours')
            elif btc_mom < 0 and direction == 'short':
                boost += 12
                reasons.append(f'BTC {btc_mom:.2f}% sur 5min — vague baissière en cours')
            elif btc_mom > 1.5 and direction == 'long':
                boost += 5  # bonus supplémentaire si BTC très fort

    except Exception as e:
        log.warning(f'Advanced signals error {symbol}: {e}')

    return boost, reasons[:3]


# ══ INDICATORS ════════════════════════════════════════════════════════
def rsi(closes, n=14):
    if len(closes) < n + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-n:]) / n
    al = sum(losses[-n:]) / n
    if al == 0: return 100.0
    return 100.0 - (100.0 / (1 + ag / al))

def ema(data, n):
    if not data: return []
    k = 2 / (n + 1)
    e = [data[0]]
    for v in data[1:]:
        e.append(v * k + e[-1] * (1 - k))
    return e

def macd_hist(closes):
    if len(closes) < 35: return 0.0
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    ml   = [f - s for f, s in zip(fast, slow)]
    sig  = ema(ml, 9)
    return ml[-1] - sig[-1]

def atr(candles, n=14):
    if len(candles) < n + 1: return 0.0
    trs = []
    for i in range(1, len(candles)):
        h  = float(candles[i][2])
        l  = float(candles[i][3])
        pc = float(candles[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n

def vol_spike(candles, n=20):
    if len(candles) < n + 1: return 1.0
    vols = [float(c[5]) for c in candles]
    avg  = sum(vols[-n-1:-1]) / n
    return vols[-1] / avg if avg > 0 else 1.0

# ══ SCORING ════════════════════════════════════════════════════════════
def score_token(ticker, c1m, c5m, c15m, c1h, weights, c4h=None):
    try:
        price  = float(ticker.get('lastPr', 0))
        vol24  = float(ticker.get('usdtVolume', 0))
        chg24  = float(ticker.get('change24h', 0)) * 100
        high24 = float(ticker.get('high24h', price))
        low24  = float(ticker.get('low24h', price))

        if price <= 0 or vol24 < MIN_VOL_24H:
            return None

        def cl(c): return [float(x[4]) for x in c] if c else []
        cl1m  = cl(c1m);  cl5m = cl(c5m)
        cl15m = cl(c15m); cl1h = cl(c1h)
        cl4h  = cl(c4h) if c4h else cl1h

        score = 0
        direction = 'long'
        reasons = []

        # ── RSI ──────────────────────────────────── w=1.0
        w_rsi = weights.get('rsi', 1.0)
        r1m  = rsi(cl1m)
        r5m  = rsi(cl5m)
        r15m = rsi(cl15m)

        if r1m < 28 and r5m < 32:
            score += 28 * w_rsi; direction = 'long'
            reasons.append(f'RSI très survendu 1m:{r1m:.0f} 5m:{r5m:.0f}')
        elif r1m < 35 and r5m < 40:
            score += 18 * w_rsi; direction = 'long'
            reasons.append(f'RSI survendu 1m:{r1m:.0f}')
        elif r1m > 72 and r5m > 68:
            score += 28 * w_rsi; direction = 'short'
            reasons.append(f'RSI surachat 1m:{r1m:.0f} 5m:{r5m:.0f}')
        elif r1m > 65 and r5m > 62:
            score += 18 * w_rsi; direction = 'short'
            reasons.append(f'RSI surachat 1m:{r1m:.0f}')
        elif 42 <= r5m <= 58:
            score += 8 * w_rsi

        # Alignement 15m
        if direction == 'long'  and r15m < 48: score += 8 * w_rsi
        elif direction == 'short' and r15m > 52: score += 8 * w_rsi

        # ── CONFIRMATION TENDANCE 1H et 4H — filtre anti-contre-tendance ──
        r1h = rsi(cl1h)
        if direction == 'long'  and r1h > 65:
            score -= 25
            reasons.append(f'DANGER: RSI 1h={r1h:.0f} surachat — contre-tendance')
        elif direction == 'short' and r1h < 35:
            score -= 25
            reasons.append(f'DANGER: RSI 1h={r1h:.0f} survente — contre-tendance')
        elif direction == 'long'  and r1h < 50:
            score += 12
            reasons.append(f'RSI 1h={r1h:.0f} confirme long')
        elif direction == 'short' and r1h > 50:
            score += 12
            reasons.append(f'RSI 1h={r1h:.0f} confirme short')

        # ── FILTRE 4H — tendance majeure obligatoire ─────────────────
        r4h = rsi(cl4h)
        if direction == 'long' and r4h > 70:
            score -= 30  # Marché surachat sur 4h — short squeeze probable
            reasons.append(f'BLOQUE: RSI 4h={r4h:.0f} surachat majeur')
        elif direction == 'short' and r4h < 30:
            score -= 30  # Marché survente sur 4h — rebond probable
            reasons.append(f'BLOQUE: RSI 4h={r4h:.0f} survente majeure')
        elif direction == 'long' and r4h < 60:
            score += 8   # Tendance 4h favorable au long
        elif direction == 'short' and r4h > 40:
            score += 8   # Tendance 4h favorable au short

        # ── MACD ─────────────────────────────────── w=1.0
        w_macd = weights.get('macd', 1.0)
        mh5m  = macd_hist(cl5m)
        mh15m = macd_hist(cl15m)
        if direction == 'long':
            if mh5m > 0 and mh15m > 0:
                score += 20 * w_macd
                reasons.append('MACD haussier aligné 5m+15m')
            elif mh5m > 0: score += 10 * w_macd
        else:
            if mh5m < 0 and mh15m < 0:
                score += 20 * w_macd
                reasons.append('MACD baissier aligné 5m+15m')
            elif mh5m < 0: score += 10 * w_macd

        # ── VOLUME SPIKE ─────────────────────────── w=1.0
        w_vol = weights.get('volume', 1.0)
        vs = vol_spike(c5m)
        if vs > 5:
            score += 18 * w_vol
            reasons.append(f'Volume x{vs:.1f} — activité massive')
        elif vs > 3: score += 12 * w_vol; reasons.append(f'Volume x{vs:.1f}')
        elif vs > 2: score += 7 * w_vol
        elif vs < 0.5: score -= 8

        # ── BREAKOUT ─────────────────────────────── w=1.0
        w_bo = weights.get('breakout', 1.0)
        if len(cl15m) >= 20:
            rec_hi = max(cl15m[-20:-1])
            rec_lo = min(cl15m[-20:-1])
            rng    = (rec_hi - rec_lo) / rec_lo * 100
            cur    = cl15m[-1]
            if cur > rec_hi * 1.002 and rng < 8 and direction == 'long':
                score += 20 * w_bo
                reasons.append('Breakout haussier confirmé')
            elif cur < rec_lo * 0.998 and rng < 8 and direction == 'short':
                score += 20 * w_bo
                reasons.append('Breakout baissier confirmé')

        # ── RANGE 24H ────────────────────────────── w=1.0
        w_rng = weights.get('range', 1.0)
        if high24 > low24:
            pos_r = (price - low24) / (high24 - low24)
            if direction == 'long'  and pos_r < 0.25:
                score += 12 * w_rng
                reasons.append('Prix en bas du range journalier')
            elif direction == 'short' and pos_r > 0.75:
                score += 12 * w_rng
                reasons.append('Prix en haut du range journalier')
            elif 0.3 <= pos_r <= 0.7: score += 5 * w_rng

        # ── FUNDING ──────────────────────────────── w=1.0
        w_fund = weights.get('funding', 1.0)
        fund = get_funding(ticker.get('symbol', ''))
        if direction == 'long'  and fund < -0.02:
            score += 10 * w_fund
            reasons.append(f'Funding {fund:.3f}% favorable')
        elif direction == 'short' and fund > 0.02:
            score += 10 * w_fund
            reasons.append(f'Funding {fund:.3f}% favorable')
        elif abs(fund) > 0.08: score -= 15

        # ── MALUS ────────────────────────────────────────────────────
        # Déjà trop pumpé
        if chg24 > 20 and direction == 'long':  score -= 15
        if chg24 < -20 and direction == 'short': score -= 15
        # ATR — volatilité
        a = atr(c5m)
        atr_pct = (a / price * 100) if price > 0 else 0
        if atr_pct > 4: score -= 12  # trop volatile

        # ── ADVANCED SIGNALS BOOST ──────────────────────────────
        adv_boost, adv_reasons = advanced_score_boost(
            ticker.get('symbol',''), direction, score
        )
        score += adv_boost
        reasons = (adv_reasons + reasons)[:4]

        final = min(100, max(0, round(score)))
        return {
            'score':     final,
            'direction': direction,
            'reasons':   reasons[:4],
            'atr_pct':   round(atr_pct, 3),
            'vol_spike': round(vs, 2),
            'rsi_1m':    round(r1m, 1),
            'rsi_5m':    round(r5m, 1),
            'funding':   round(fund, 4),
            'chg24':     round(chg24, 2),
            'adv_boost': adv_boost,
        }
    except Exception as e:
        log.warning(f'Score error: {e}')
        return None

# ══ ADAPTIVE LEARNING ═════════════════════════════════════════════════
def update_weights(state):
    """Ajuste les poids des indicateurs selon les performances passées"""
    hist = state.get('history', [])
    if len(hist) < 5:
        return state  # pas assez de données

    # Analyser les 10 derniers trades
    recent = hist[:10]
    wins   = [h for h in recent if h.get('pnl', 0) > 0]
    losses = [h for h in recent if h.get('pnl', 0) <= 0]

    if not wins and not losses:
        return state

    weights = state.get('score_weights', {
        'rsi': 1.0, 'macd': 1.0, 'volume': 1.0,
        'breakout': 1.0, 'range': 1.0, 'funding': 1.0
    })

    # Si on perd beaucoup → réduire légèrement les poids
    # Si on gagne → augmenter légèrement
    win_rate = len(wins) / len(recent)

    if win_rate >= 0.65:
        # Bonne performance — légèrement plus agressif
        for k in weights:
            weights[k] = min(1.5, weights[k] * 1.05)
        state['mode'] = 'Aggressive' if win_rate >= 0.8 else 'Normal'
        log.info(f'Learning: win_rate={win_rate:.0%} → mode Aggressive')
    elif win_rate <= 0.35:
        # Mauvaise performance — plus conservateur
        for k in weights:
            weights[k] = max(0.5, weights[k] * 0.92)
        state['mode'] = 'Protective'
        log.info(f'Learning: win_rate={win_rate:.0%} → mode Protective')
    else:
        state['mode'] = 'Normal'

    state['score_weights'] = weights
    return state

# ══ LEVERAGE ══════════════════════════════════════════════════════════
def set_leverage(symbol, lev, side):
    r = POST('/api/v2/mix/account/set-leverage', {
        'symbol':      symbol,
        'productType': 'USDT-FUTURES',
        'marginCoin':  'USDT',
        'leverage':    str(lev),
        'holdSide':    side,
    })
    log.info(f'SetLev {symbol} x{lev} {side}: {r.get("code")}')
    return r.get('code') == '00000'

# ══ PLACE ORDER ═══════════════════════════════════════════════════════
def place_order(symbol, direction, balance, scored, state_balance_info=None):
    try:
        # Levier selon capital
        lev = 20 if balance >= 500 else 15

        # Levier dynamique basé sur le score
        score = scored.get('score', 62)
        consensus = scored.get('consensus_boost', False)

        if score >= 90:   base_lev = 25
        elif score >= 80: base_lev = 20
        elif score >= 70: base_lev = 15
        else:             base_lev = 10

        # Bonus consensus marché
        if consensus:
            base_lev = min(25, int(base_lev * 1.25))
            log.info(f'Consensus boost: levier -> x{base_lev}')

        # Marge dynamique selon pertes consécutives
        consec_losses = state_balance_info.get('consecutive_losses', 0) if state_balance_info else 0
        if consec_losses >= 2:
            risk_pct = 0.25
            log.info(f'Mode survie: 2 pertes consecutives -> marge 25%')
        elif consec_losses == 1:
            risk_pct = 0.40
            log.info(f'Mode prudent: 1 perte -> marge 40%')
        else:
            risk_pct = RISK_PCT  # 60%

        lev = base_lev
        log.info(f'Score={score} -> levier x{lev} marge={risk_pct*100:.0f}%')

        # Set leverage — essayer le levier demandé, puis descendre si refus
        def set_lev_safe(sym, target_lev, side):
            for try_lev in [target_lev, int(target_lev*0.75), 10, 5]:
                r = POST('/api/v2/mix/account/set-leverage', {
                    'symbol':      sym,
                    'productType': 'USDT-FUTURES',
                    'marginCoin':  'USDT',
                    'leverage':    str(try_lev),
                    'holdSide':    side,
                })
                code = r.get('code', '')
                log.info(f'SetLev {sym} x{try_lev} {side}: {code}')
                if code == '00000':
                    return try_lev
                time.sleep(0.15)
            return 5

        actual_lev_l = set_lev_safe(symbol, lev, 'long')
        time.sleep(0.2)
        actual_lev_s = set_lev_safe(symbol, lev, 'short')
        time.sleep(0.2)
        lev = min(actual_lev_l, actual_lev_s)
        log.info(f'Effective leverage: x{lev}')

        # Prix actuel
        tk = GET('/api/v2/mix/market/ticker', {
            'symbol': symbol, 'productType': 'USDT-FUTURES'
        })
        if tk.get('code') != '00000':
            log.error(f'Ticker failed: {tk}')
            return None
        price = float(tk['data'][0]['lastPr'])
        log.info(f'Entry price: {price}')

        # Infos contrat
        ci = GET('/api/v2/mix/market/contracts', {
            'productType': 'USDT-FUTURES', 'symbol': symbol
        })
        size_dec = 1
        min_size = 0.01
        price_dec = 6
        if ci.get('code') == '00000' and ci.get('data'):
            d = ci['data'][0]
            size_dec  = int(d.get('volumePlace', 1))
            min_size  = float(d.get('minTradeNum', 0.01))
            price_dec = int(d.get('pricePlace', 6))

        # Taille: 40% du solde comme marge
        # Bitget isolated exige beaucoup de buffer (fees, liquidation reserve)
        target_margin = balance * 0.40
        notional      = target_margin * lev
        size          = notional / price
        if size_dec > 0:
            size = math.floor(size * (10**size_dec)) / (10**size_dec)
        else:
            size = math.floor(size)
        size = max(size, min_size)
        actual_margin = (size * price) / lev
        log.info(f'Size calc: balance={balance:.2f} margin={actual_margin:.2f} ({actual_margin/balance*100:.1f}%) size={size}')

        # TP et SL — prix absolus arrondis correctement
        def rnd(p):
            return round(p, price_dec)

        if direction == 'long':
            tp_price = rnd(price * (1 + TP_PCT))
            sl_price = rnd(price * (1 - SL_PCT))
            side     = 'buy'
        else:
            tp_price = rnd(price * (1 - TP_PCT))
            sl_price = rnd(price * (1 + SL_PCT))
            side     = 'sell'

        # Vérification: SL ne doit pas être plus proche que 2× le SL_PCT
        # On s'assure d'un minimum de distance
        min_sl_dist = price * SL_PCT * 0.5
        if direction == 'long':
            sl_price = min(sl_price, rnd(price - min_sl_dist))
        else:
            sl_price = max(sl_price, rnd(price + min_sl_dist))

        log.info(f'Order: {symbol} {side} size={size} price={price} TP={tp_price} SL={sl_price} lev=x{lev}')

        # ÉTAPE 1 — Ordre market (sans TP/SL dans l'ordre)
        order_body = {
            'symbol':      symbol,
            'productType': 'USDT-FUTURES',
            'marginMode':  'isolated',
            'marginCoin':  'USDT',
            'size':        str(size),
            'side':        side,
            'tradeSide':   'open',
            'orderType':   'market',
        }
        r = POST('/api/v2/mix/order/place-order', order_body)
        log.info(f'Order response: {r}')

        if r.get('code') != '00000':
            log.error(f'Order failed: {r}')
            return None

        order_id = r['data']['orderId']
        time.sleep(2.0)  # Attendre que la position soit ouverte

        # Récupérer le vrai prix de liquidation depuis Bitget
        real_liq = 0.0
        tp_price_final = tp_price
        sl_price_final = sl_price
        try:
            pos_data = GET('/api/v2/mix/position/all-position', {
                'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'
            })
            for p in (pos_data.get('data') or []):
                if p.get('symbol') == symbol and float(p.get('total', 0)) > 0:
                    real_liq = float(p.get('liquidationPrice', 0))
                    log.info(f'Real liq price: {real_liq}')
                    break
            if real_liq > 0:
                if direction == 'long':
                    safe_sl = round(real_liq * 1.035, price_dec)
                    sl_price_final = max(sl_price, safe_sl)
                    if sl_price_final != sl_price:
                        log.info(f'SL moved above liq: {sl_price} -> {sl_price_final}')
                else:
                    safe_sl = round(real_liq * 0.965, price_dec)
                    sl_price_final = min(sl_price, safe_sl)
                    if sl_price_final != sl_price:
                        log.info(f'SL moved below liq: {sl_price} -> {sl_price_final}')
        except Exception as e:
            log.warning(f'Liq fetch failed: {e}')

        # ÉTAPE 2 — Poser TP/SL séparément via l'endpoint dédié
        hold_side = 'long' if direction == 'long' else 'short'

        # ── PLACEMENT TP ──────────────────────────────────────────────
        def place_tpsl(plan_type, trigger_price, label):
            """Place un ordre TP ou SL — essaie les deux formats Bitget"""
            # Format 1: avec size (requis par certaines versions API)
            body = {
                'symbol':       symbol,
                'productType':  'USDT-FUTURES',
                'marginCoin':   'USDT',
                'planType':     plan_type,
                'triggerPrice': str(round(trigger_price, price_dec)),
                'triggerType':  'mark_price',
                'holdSide':     hold_side,
                'size':         str(size),
            }
            r = POST('/api/v2/mix/order/place-tpsl-order', body)
            log.info(f'{label} at {trigger_price}: code={r.get("code")} msg={r.get("msg","")}')
            if r.get('code') == '00000':
                return r
            log.error(f'{label} format1 FAILED: {r}')

            # Format 2: sans size
            time.sleep(0.3)
            body2 = {k: v for k, v in body.items() if k != 'size'}
            r2 = POST('/api/v2/mix/order/place-tpsl-order', body2)
            log.info(f'{label} format2: code={r2.get("code")} msg={r2.get("msg","")}')
            if r2.get('code') == '00000':
                return r2
            log.error(f'{label} format2 FAILED: {r2}')

            # Format 3: endpoint alternatif place-plan-order
            time.sleep(0.3)
            body3 = {
                'symbol':       symbol,
                'productType':  'USDT-FUTURES',
                'marginCoin':   'USDT',
                'planType':     plan_type,
                'triggerPrice': str(round(trigger_price, price_dec)),
                'triggerType':  'mark_price',
                'side':         'buy' if (plan_type == 'loss_plan' and hold_side == 'short') or (plan_type == 'profit_plan' and hold_side == 'long') else 'sell',
                'tradeSide':    'close',
                'size':         str(size),
                'orderType':    'market',
            }
            r3 = POST('/api/v2/mix/order/place-plan-order', body3)
            log.info(f'{label} format3 plan-order: code={r3.get("code")} msg={r3.get("msg","")}')
            return r3

        tp_r = place_tpsl('profit_plan', tp_price_final, 'TP')
        time.sleep(0.5)
        sl_r = place_tpsl('loss_plan', sl_price_final, 'SL')
        time.sleep(0.5)

        # Retry TP/SL si echec — avec log complet pour debug
        if tp_r.get('code') != '00000':
            log.error(f'TP FAILED full response: {tp_r}')
            time.sleep(1.5)
            tp_r2 = place_tpsl('profit_plan', tp_price_final, 'TP-retry')
            log.info(f'TP retry: code={tp_r2.get("code")} msg={tp_r2.get("msg","")}')
        if sl_r.get('code') != '00000':
            log.error(f'SL FAILED full response: {sl_r}')
            # Essayer sans size
            time.sleep(1.5)
            r_nosize = POST('/api/v2/mix/order/place-tpsl-order', {
                'symbol':       symbol,
                'productType':  'USDT-FUTURES',
                'marginCoin':   'USDT',
                'planType':     'loss_plan',
                'triggerPrice': str(round(sl_price_final, price_dec)),
                'triggerType':  'mark_price',
                'holdSide':     hold_side,
            })
            log.info(f'SL retry without size: code={r_nosize.get("code")} msg={r_nosize.get("msg","")}')
            if r_nosize.get('code') != '00000':
                log.error(f'SL COMPLETELY FAILED — position sans SL!')

        return {
            'orderId':       order_id,
            'symbol':        symbol,
            'direction':     direction,
            'entryPrice':    price,
            'currentPrice':  price,
            'size':          size,
            'leverage':      lev,
            'tp':            tp_price_final,
            'sl':            sl_price_final,
            'tp_pct':        round(TP_PCT * 100, 2),
            'sl_pct':        round(SL_PCT * 100, 2),
            'margin':        round(target_margin, 4),
            'openTime':      datetime.now(timezone.utc).isoformat(),
            'scoreAtEntry':  scored['score'],
            'reasons':       scored['reasons'],
            'unrealizedPnl': 0.0,
            'liqPrice':      real_liq,
            'totalSize':     size,
            'trailing_active': False,
            'trailing_high':   price,
        }

    except Exception as e:
        log.error(f'Place order exception: {e}')
        return None

# ══ CHECK POSITION ════════════════════════════════════════════════════
def check_position(state):
    if not state['position']:
        return state

    log.info(f'Checking position: {state["position"]["symbol"]}')
    positions = get_positions()
    sym  = state['position']['symbol']
    pos  = next((p for p in positions if p['symbol'] == sym), None)

    if not pos:
        # Position fermée — on compare avec le solde précédent
        time.sleep(1.0)
        bal_new = get_balance()
        old_bal = state['balance']
        pnl     = round(bal_new - old_bal, 6)

        log.info(f'Position closed. Old bal={old_bal} New bal={bal_new} PNL={pnl}')

        record = {
            **state['position'],
            'closeTime':    datetime.now(timezone.utc).isoformat(),
            'pnl':          pnl,
            'pnlPct':       round((pnl / max(state['position'].get('margin', 1), 0.01)) * 100, 2),
            'closeBalance': round(bal_new, 6),
            'exitReason':   'TP/SL auto',
        }
        state['history'].insert(0, record)
        if len(state['history']) > 200:
            state['history'] = state['history'][:200]

        # Update daily PNL
        today = str(datetime.now(timezone.utc).date())
        if state.get('today_date') != today:
            state['today_pnl'] = 0.0
            state['today_date'] = today
        state['today_pnl']   = round(state.get('today_pnl', 0) + pnl, 6)
        state['total_pnl']   = round(state.get('total_pnl', 0) + pnl, 6)
        state['total_trades']= state.get('total_trades', 0) + 1

        if pnl > 0:
            state['win_trades']         = state.get('win_trades', 0) + 1
            state['consecutive_wins']   = state.get('consecutive_wins', 0) + 1
            state['consecutive_losses'] = 0
        else:
            state['consecutive_losses'] = state.get('consecutive_losses', 0) + 1
            state['consecutive_wins']   = 0

        state['position']      = None
        state['balance']       = round(bal_new, 6)
        state['balance_total'] = round(bal_new, 6)
        state['status']        = f'{"Gain" if pnl>0 else "Perte"}: {pnl:+.4f} USDT'

        # Apprentissage après chaque trade fermé
        state = update_weights(state)

    else:
        # Sync depuis Bitget (source de vérité)
        unr   = float(pos.get('unrealizedPL', 0))
        cp    = float(pos.get('markPrice',    state['position']['entryPrice']))
        entry = float(pos.get('openPriceAvg', state['position']['entryPrice']))
        marg  = float(pos.get('marginSize',   state['position'].get('margin', 0)))
        lev   = int(float(pos.get('leverage', state['position'].get('leverage', 15))))
        liq   = float(pos.get('liquidationPrice', 0))
        tot   = float(pos.get('total', 0))

        state['position']['unrealizedPnl'] = round(unr, 6)
        state['position']['currentPrice']  = cp
        state['position']['entryPrice']    = entry
        state['position']['margin']        = round(marg, 4)
        state['position']['leverage']      = lev
        state['position']['liqPrice']      = liq
        state['position']['totalSize']     = tot

        # Solde total = disponible + marge + PNL live
        avail = get_balance()
        state['balance']       = round(avail, 6)
        state['balance_total'] = round(avail + marg + unr, 6)

        # ── TRAILING STOP INTELLIGENT ──────────────────────────────────
        ep   = entry
        dirp = state['position']['direction']
        liq  = state['position'].get('liqPrice', 0)

        gain_pct = (cp - ep) / ep if dirp == 'long' else (ep - cp) / ep
        sl_updated = False
        new_sl = None
        log.info(f'Position monitor: {sym} ep={ep} cp={cp} gain={gain_pct*100:.2f}% liq={liq}')

        if gain_pct >= TRAIL_STEP4:       # +35%+ → lock +25%
            lock = 0.25
        elif gain_pct >= TRAIL_STEP3:      # +20%+ → lock +12%
            lock = 0.12
        elif gain_pct >= TRAIL_STEP2:      # +10%+ → lock +4%
            lock = 0.04
        elif gain_pct >= TRAIL_STEP1:      # +5%+  → lock breakeven (+0.5%)
            lock = 0.005
        else:
            lock = None  # pas encore de trailing

        if lock is not None:
            state['position']['trailing_active'] = True
            if dirp == 'long':
                candidate = round(ep * (1 + lock), 8)
                # SÉCURITÉ CRITIQUE — jamais sous la liquidation
                if liq > 0:
                    candidate = max(candidate, round(liq * 1.02, 8))
                # Seulement monter le SL, jamais le descendre
                if candidate > state['position']['sl']:
                    new_sl = candidate
                    sl_updated = True
            else:  # short
                candidate = round(ep * (1 - lock), 8)
                # SÉCURITÉ CRITIQUE — jamais au-dessus de la liquidation
                if liq > 0:
                    candidate = min(candidate, round(liq * 0.98, 8))
                # Seulement descendre le SL, jamais le monter
                if candidate < state['position']['sl']:
                    new_sl = candidate
                    sl_updated = True

        if sl_updated and new_sl:
            state['position']['sl'] = new_sl
            log.info(f'Trailing SL → {new_sl} (gain: {gain_pct*100:.1f}%)')
            # Annuler TOUS les anciens ordres SL puis poser le nouveau
            try:
                hold_side = 'long' if dirp == 'long' else 'short'

                # Étape 1 — Récupérer tous les ordres TPSL en cours
                existing = GET('/api/v2/mix/order/plan-delegateList', {
                    'symbol':      sym,
                    'productType': 'USDT-FUTURES',
                    'planType':    'loss_plan',
                    'status':      'live',
                })
                log.info(f'Existing SL orders: {existing.get("code")} count={len(existing.get("data",{}).get("entrustedList",[]))}')

                # Étape 2 — Annuler chaque ordre SL existant
                orders = existing.get('data', {}).get('entrustedList', [])
                for order in orders:
                    oid = order.get('orderId', '')
                    if oid:
                        cancel_r = POST('/api/v2/mix/order/cancel-plan-order', {
                            'symbol':      sym,
                            'productType': 'USDT-FUTURES',
                            'orderId':     oid,
                        })
                        log.info(f'Cancelled SL order {oid}: {cancel_r.get("code")}')
                        time.sleep(0.15)

                # Étape 3 — Poser le nouveau SL (sans executePrice ni size inutile)
                sl_r = POST('/api/v2/mix/order/place-tpsl-order', {
                    'symbol':       sym,
                    'productType':  'USDT-FUTURES',
                    'marginCoin':   'USDT',
                    'planType':     'loss_plan',
                    'triggerPrice': str(new_sl),
                    'triggerType':  'mark_price',
                    'holdSide':     hold_side,
                })
                log.info(f'New SL placed at {new_sl}: code={sl_r.get("code")} msg={sl_r.get("msg","")}')
                if sl_r.get('code') != '00000':
                    log.error(f'Trailing SL update FAILED: {sl_r}')

            except Exception as e:
                log.warning(f'SL update failed: {e}')

        # ── SORTIE FORCÉE à MAX_GAIN ────────────────────────────────────
        if gain_pct >= MAX_GAIN_PCT:
            log.info(f'Max gain reached ({gain_pct*100:.1f}%) — forcing close')
            state['position']['force_close'] = True
            state['status'] = f'Sortie forcée — gain max {gain_pct*100:.1f}% atteint'

        # ── SIGNAL DÉGRADÉ — sortir si position plus valide ────────────
        if not state['position'].get('force_close'):
            # Re-score la position toutes les 3 scans
            state['position']['scan_count'] = state['position'].get('scan_count', 0) + 1
            if state['position']['scan_count'] % 3 == 0:
                # Si en perte ET momentum inverse → sortir
                if gain_pct < -0.01 and gain_pct <= -SL_PCT * 0.8:
                    log.info(f'SL about to trigger — monitoring closely')

    return state

# ══ DETECT MANUAL CLOSE ═══════════════════════════════════════════════
def detect_manual_close(state):
    """Détecte si l'utilisateur a fermé la position manuellement sur Bitget"""
    if not state['position']:
        return state

    positions = get_positions()
    sym = state['position']['symbol']
    still_open = any(p['symbol'] == sym for p in positions)

    if not still_open:
        log.info('Manual close detected')
        bal_new = get_balance()
        pnl     = round(bal_new - state['balance'], 6)

        record = {
            **state['position'],
            'closeTime':    datetime.now(timezone.utc).isoformat(),
            'pnl':          pnl,
            'pnlPct':       round((pnl / max(state['position'].get('margin', 1), 0.01)) * 100, 2),
            'closeBalance': round(bal_new, 6),
            'exitReason':   'Fermé manuellement',
        }
        state['history'].insert(0, record)
        if len(state['history']) > 200:
            state['history'] = state['history'][:200]

        today = str(datetime.now(timezone.utc).date())
        if state.get('today_date') != today:
            state['today_pnl'] = 0.0
            state['today_date'] = today

        state['today_pnl']    = round(state.get('today_pnl', 0) + pnl, 6)
        state['total_pnl']    = round(state.get('total_pnl', 0) + pnl, 6)
        state['total_trades'] = state.get('total_trades', 0) + 1
        if pnl > 0:
            state['win_trades'] = state.get('win_trades', 0) + 1
            state['consecutive_wins']   = state.get('consecutive_wins', 0) + 1
            state['consecutive_losses'] = 0
        else:
            state['consecutive_losses'] = state.get('consecutive_losses', 0) + 1
            state['consecutive_wins']   = 0

        state['position']      = None
        state['balance']       = round(bal_new, 6)
        state['balance_total'] = round(bal_new, 6)
        state['status']        = f'Ferme manuellement — PNL: {pnl:+.4f} USDT'
        emoji = 'GAIN' if pnl > 0 else 'PERTE'
        send_sms(
            f'PORTAL DAVID - Trade ferme\n'
            f'{emoji}: {pnl:+.4f} USDT (manuel)\n'
            f'Nouveau solde: ${bal_new:.2f}'
        )
        state = update_weights(state)

    return state

# ══ MAIN SCAN ══════════════════════════════════════════════════════════
def close_position_on_bitget(symbol, direction, size):
    """Ferme la position sur Bitget via ordre market"""
    side      = 'sell' if direction == 'long' else 'buy'
    hold_side = 'long' if direction == 'long' else 'short'
    r = POST('/api/v2/mix/order/place-order', {
        'symbol':      symbol,
        'productType': 'USDT-FUTURES',
        'marginMode':  'isolated',
        'marginCoin':  'USDT',
        'size':        str(size),
        'side':        side,
        'tradeSide':   'close',
        'orderType':   'market',
    })
    log.info(f'Force close {symbol}: {r}')
    return r.get('code') == '00000'

def scan(state):
    # Si bot en pause — ne rien faire
    if state.get('paused'):
        state['status'] = 'Bot en pause — réactivation requise'
        return state

    state['last_scan'] = datetime.now(timezone.utc).isoformat()
    # Refresh CAD rate every ~1h (120 scans × 30s)
    state['_scan_count'] = state.get('_scan_count', 0) + 1
    if state['_scan_count'] % 120 == 0:
        state['cad_rate'] = fetch_cad_rate()

    # Warmup — attendre 6 scans (3 min) avant de trader après un redémarrage
    # Évite d'ouvrir un trade immédiatement sur données insuffisantes
    if state['_scan_count'] < 6:
        state['status'] = f'Warmup… ({state["_scan_count"]}/6 scans)'
        log.info(f'Warmup scan {state["_scan_count"]}/6 — pas de trade encore')
        return state
    today = str(datetime.now(timezone.utc).date())
    if state.get('today_date') != today:
        state['today_pnl'] = 0.0
        state['today_date'] = today

    # Vérifier fermeture manuelle en premier
    state = detect_manual_close(state)

    # Si position ouverte — juste surveiller
    if state['position']:
        state = check_position(state)
        # Si la position vient de se fermer → continuer vers le scan
        if not state['position']:
            log.info('Position fermée — reprise du scan')
        else:
            pos = state['position']
            unr = pos.get('unrealizedPnl', 0)
            pct = (unr / pos.get('margin', 1) * 100) if pos.get('margin') else 0
            trail = ' | Trailing actif' if pos.get('trailing_active') else ''
        if state['position'] and state['position'].get('force_close'):
            sym  = state['position']['symbol']
            dirp = state['position']['direction']
            sz   = state['position'].get('totalSize', 0)
            if sz > 0:
                ok = close_position_on_bitget(sym, dirp, sz)
                if ok:
                    log.info('Force close executed successfully')
                    time.sleep(1.5)
                    state = check_position(state)
                    return state

        state['status'] = (
                f'{pos["symbol"]} {pos["direction"].upper()} x{pos["leverage"]} — '
                f'{unr:+.4f} USDT ({pct:+.1f}%){trail}'
            )
        return state

    # Scan du marché
    state['status'] = 'Analyse du marché…'
    bal = get_balance()
    if bal > 0:
        state['balance'] = round(bal, 6)
        state['balance_total'] = round(bal, 6)

    # Si position dans l'état — surveiller et ajuster SL/trailing
    if state.get('position'):
        state = check_position(state)
        return state

    # Vérifier si position ouverte sur Bitget mais pas dans l'état
    existing_pos = get_positions()
    if existing_pos:
        sym = existing_pos[0].get('symbol', '')
        log.info(f'Position Bitget détectée ({sym}) sans état — sync')
        p   = existing_pos[0]
        ep  = float(p.get('openPriceAvg', 0))
        lev = int(float(p.get('leverage', 15)))
        mg  = float(p.get('marginSize', 0))
        sz  = float(p.get('total', 0))
        hld = p.get('holdSide', 'long')
        dirp = 'long' if hld == 'long' else 'short'
        state['position'] = {
            'symbol': sym, 'direction': dirp,
            'entryPrice': ep, 'currentPrice': ep,
            'tp': round(ep*(1+TP_PCT) if dirp=='long' else ep*(1-TP_PCT), 8),
            'sl': round(ep*(1-SL_PCT) if dirp=='long' else ep*(1+SL_PCT), 8),
            'leverage': lev, 'margin': mg, 'liqPrice': float(p.get('liquidationPrice',0)),
            'totalSize': sz, 'unrealizedPnl': float(p.get('unrealizedPL',0)),
            'scoreAtEntry': 60, 'reasons': ['Sync Bitget'],
            'openTime': '', 'trailing_active': False, 'trailing_high': ep,
            'tp_pct': TP_PCT*100, 'sl_pct': SL_PCT*100,
        }
        state = check_position(state)
        return state

    tickers = get_tickers()
    if not tickers:
        state['status'] = 'Erreur API marché'
        return state

    # Top 30 par volume
    top = sorted(tickers, key=lambda x: float(x.get('usdtVolume', 0)), reverse=True)[:30]
    candidates = []
    weights = state.get('score_weights', {
        'rsi':1.0,'macd':1.0,'volume':1.0,'breakout':1.0,'range':1.0,'funding':1.0
    })

    for tk in top:
        sym = tk.get('symbol', '')
        if not sym.endswith('USDT'): continue
        if sym in ['USDCUSDT','TUSDUSDT','BUSDUSDT','FDUSDUSDT']: continue

        state['signals_checked'] = state.get('signals_checked', 0) + 1

        c1m  = get_candles(sym, '1m', 100); time.sleep(0.07)
        c5m  = get_candles(sym, '5m', 100); time.sleep(0.07)
        c15m = get_candles(sym, '15m', 60); time.sleep(0.07)
        c1h  = get_candles(sym, '1H', 50);  time.sleep(0.07)
        c4h  = get_candles(sym, '4H', 30);  time.sleep(0.07)
        res = score_token(tk, c1m, c5m, c15m, c1h, weights, c4h)
        if res and res['score'] >= MIN_SCORE:
            candidates.append({'symbol': sym, **res})
            log.info(f'Candidate: {sym} score={res["score"]} dir={res["direction"]}')

    state['status'] = f'Scan terminé — {len(candidates)} signaux sur {len(top)} paires'

    if not candidates:
        state['status'] = 'Aucun signal fort — surveillance continue'
        return state

    best = sorted(candidates, key=lambda x: x['score'], reverse=True)[0]

    # Détection consensus — combien de cryptos vont dans la même direction?
    same_dir = [c for c in candidates if c['direction'] == best['direction']]
    consensus = len(same_dir) >= 3
    best['consensus_boost'] = consensus
    if consensus:
        log.info(f'Consensus détecté: {len(same_dir)} cryptos en {best["direction"]} — boost levier')

    log.info(f'Best: {best["symbol"]} score={best["score"]} dir={best["direction"]} consensus={consensus}')
    state['status'] = f'Signal: {best["symbol"]} {best["direction"].upper()} score={best["score"]} — Ouverture…'

    pos = place_order(best['symbol'], best['direction'], state['balance'], best,
                      state_balance_info={'consecutive_losses': state.get('consecutive_losses', 0)})
    if pos:
        state['position'] = pos
        state['status'] = (
            f'{pos["symbol"]} {pos["direction"].upper()} x{pos["leverage"]} ouvert — '
            f'TP: +{pos["tp_pct"]}% | SL: -{pos["sl_pct"]}%'
        )
        log.info(f'Position opened: {pos["symbol"]} {pos["direction"]} x{pos["leverage"]}')
        # SMS ouverture
        send_sms(
            f'PORTAL DAVID - Trade ouvert\n'
            f'{pos["symbol"]} {pos["direction"].upper()} x{pos["leverage"]}\n'
            f'Entree: ${pos["entryPrice"]}\n'
            f'TP: ${pos["tp"]} (+{pos["tp_pct"]}%)\n'
            f'SL: ${pos["sl"]}\n'
            f'Score: {pos["scoreAtEntry"]}/100'
        )
    else:
        state['status'] = f'Échec ordre {best["symbol"]} — prochaine tentative'

    return state

# ══ FLASK ═══════════════════════════════════════════════════════════════
app = Flask(__name__)

def cors_json(data):
    r = Response(json.dumps(data, default=str), mimetype='application/json')
    r.headers['Access-Control-Allow-Origin'] = '*'
    return r

@app.route('/')
def root():
    return cors_json({'status': 'Portal David Bot v3', 'ok': True})

@app.route('/api/state')
def api_state():
    return cors_json(load_state())

@app.route('/api/health')
def api_health():
    return cors_json({'ok': True, 'ts': datetime.now(timezone.utc).isoformat()})

@app.route('/api/chat', methods=['POST', 'OPTIONS'])
def api_chat():
    """Chat avec le bot — explique ses décisions en français simple"""
    from flask import request
    if request.method == 'OPTIONS':
        r = Response('', 204)
        r.headers['Access-Control-Allow-Origin']  = '*'
        r.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return r

    try:
        body     = request.get_json(force=True)
        question = body.get('question', '').strip()[:500]
        if not question:
            return cors_json({'answer': 'Pose-moi une question sur mes trades.'})

        state = load_state()
        pos   = state.get('position')
        hist  = state.get('history', [])[:5]
        bal   = state.get('balance_total', state.get('balance', 0))

        # Contexte du bot pour l'IA
        context = f"""Tu es le bot de trading de David. Tu parles en français simple, sans jargon financier.
Voici ton état actuel:
- Solde: ${bal:.2f} USDT
- Mode: {state.get('mode', 'Normal')}
- Trades fermés: {state.get('total_trades', 0)} ({state.get('win_trades', 0)} gagnants)
- PNL aujourd'hui: {state.get('today_pnl', 0):+.4f} USDT
- PNL total: {state.get('total_pnl', 0):+.4f} USDT
"""
        if pos:
            unr = pos.get('unrealizedPnl', 0)
            ep  = pos.get('entryPrice', 0)
            cp  = pos.get('currentPrice', ep)
            pct = ((cp - ep) / ep * 100) if ep > 0 else 0
            context += f"""
Position ouverte:
- Coin: {pos.get('symbol')}
- Direction: {pos.get('direction', '').upper()}
- Levier: x{pos.get('leverage')}
- Prix d'entrée: ${ep}
- Prix actuel: ${cp}
- P&L: {unr:+.4f} USDT ({pct:+.2f}%)
- Take Profit: ${pos.get('tp')}
- Stop Loss: ${pos.get('sl')}
- Score de confiance: {pos.get('scoreAtEntry', 0)}/100
- Raisons: {', '.join(pos.get('reasons', []))}
"""
        if hist:
            context += "\n5 derniers trades:"
            for h in hist:
                context += f"\n- {h.get('symbol')} {h.get('direction','').upper()}: {h.get('pnl', 0):+.4f} USDT ({h.get('exitReason', 'auto')})"

        context += f"""\n\nStratégie:
- Tu analyses RSI multi-TF, MACD, volume, breakouts, carnet d'ordres, liquidations, momentum BTC
- Levier x15-20, TP à +5%, SL à -2.5%
- Tu ajustes tes poids selon les performances passées (apprentissage adaptatif)
- Objectif: 94.36$ → 100 000$
"""
        # Appel API Claude
        import urllib.request
        payload = json.dumps({
            'model': 'claude-sonnet-4-6',
            'max_tokens': 400,
            'system': context,
            'messages': [{'role': 'user', 'content': question}]
        }).encode()

        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return cors_json({'answer': 'Clé API Anthropic manquante. Ajoute ANTHROPIC_API_KEY dans les variables Render.'})

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload,
            headers={
                'Content-Type':      'application/json',
                'anthropic-version': '2023-06-01',
                'x-api-key':         api_key,
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            answer = data['content'][0]['text']

        return cors_json({'answer': answer})

    except Exception as e:
        log.error(f'Chat error: {e}')
        return cors_json({'answer': f'Erreur: {str(e)[:100]}'})

@app.route('/api/pause', methods=['POST','OPTIONS'])
def api_pause():
    from flask import request
    if request.method == 'OPTIONS':
        r = Response('', 204)
        r.headers['Access-Control-Allow-Origin']  = '*'
        r.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return r
    try:
        body   = request.get_json(force=True)
        action = body.get('action', 'pause')  # pause or resume
        state  = load_state()
        state['paused'] = (action == 'pause')
        state['status'] = 'Bot en pause' if state['paused'] else 'Bot réactivé — reprise du scan'
        save_state(state)
        log.info(f'Bot {"paused" if state["paused"] else "resumed"}')
        return cors_json({'ok': True, 'paused': state['paused']})
    except Exception as e:
        return cors_json({'ok': False, 'error': str(e)})

@app.route('/api/close-trade', methods=['POST','OPTIONS'])
def api_close_trade():
    from flask import request
    if request.method == 'OPTIONS':
        r = Response('', 204)
        r.headers['Access-Control-Allow-Origin']  = '*'
        r.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return r
    try:
        state = load_state()
        pos   = state.get('position')
        if not pos:
            return cors_json({'ok': False, 'error': 'Aucune position ouverte'})

        sym  = pos['symbol']
        dirp = pos['direction']
        sz   = pos.get('totalSize', 0)
        log.info(f'Manual close request: {sym} {dirp} size={sz}')

        ok = close_position_on_bitget(sym, dirp, sz) if sz > 0 else False
        time.sleep(2)

        # Recalculate PNL from balance
        bal_new = get_balance()
        pnl     = round(bal_new - state['balance'], 6)

        record = {
            **pos,
            'closeTime':    datetime.now(timezone.utc).isoformat(),
            'pnl':          pnl,
            'pnlPct':       round((pnl / max(pos.get('margin',1),0.01))*100, 2),
            'closeBalance': round(bal_new, 6),
            'exitReason':   'Fermé manuellement via dashboard',
        }
        state['history'].insert(0, record)
        if len(state['history']) > 200: state['history'] = state['history'][:200]

        today = str(datetime.now(timezone.utc).date())
        if state.get('today_date') != today:
            state['today_pnl'] = 0.0
            state['today_date'] = today
        state['today_pnl']    = round(state.get('today_pnl',0) + pnl, 6)
        state['total_pnl']    = round(state.get('total_pnl',0) + pnl, 6)
        state['total_trades'] = state.get('total_trades',0) + 1
        if pnl > 0:
            state['win_trades']         = state.get('win_trades',0) + 1
            state['consecutive_wins']   = state.get('consecutive_wins',0) + 1
            state['consecutive_losses'] = 0
        else:
            state['consecutive_losses'] = state.get('consecutive_losses',0) + 1
            state['consecutive_wins']   = 0

        state['position']      = None
        state['balance']       = round(bal_new, 6)
        state['balance_total'] = round(bal_new, 6)
        state['status']        = f'Ferme manuellement — PNL: {pnl:+.4f} USDT'
        emoji = 'GAIN' if pnl > 0 else 'PERTE'
        send_sms(
            f'PORTAL DAVID - Trade ferme\n'
            f'{emoji}: {pnl:+.4f} USDT (manuel)\n'
            f'Nouveau solde: ${bal_new:.2f}'
        )
        save_state(state)
        return cors_json({'ok': True, 'pnl': pnl, 'balance': bal_new})
    except Exception as e:
        log.error(f'Close trade error: {e}')
        return cors_json({'ok': False, 'error': str(e)})

@app.route('/api/fix-history', methods=['POST','OPTIONS'])
def api_fix_history():
    """Corrige ou ajoute un trade dans l historique"""
    from flask import request
    if request.method == 'OPTIONS':
        r = Response('', 204)
        r.headers['Access-Control-Allow-Origin']  = '*'
        r.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return r
    try:
        body  = request.get_json(force=True)
        state = load_state()
        record = body.get('record', {})
        if record:
            state['history'].insert(0, record)
            state['today_pnl']  = round(state.get('today_pnl',0) + record.get('pnl',0), 6)
            state['total_pnl']  = round(state.get('total_pnl',0) + record.get('pnl',0), 6)
            state['total_trades'] = state.get('total_trades',0) + 1
            if record.get('pnl',0) > 0:
                state['win_trades'] = state.get('win_trades',0) + 1
            bal = body.get('balance')
            if bal: state['balance'] = bal; state['balance_total'] = bal
            save_state(state)
        return cors_json({'ok': True})
    except Exception as e:
        return cors_json({'ok': False, 'error': str(e)})

@app.route('/api/candles')
def api_candles():
    """Retourne les chandeliers 1m pour le graphique live"""
    from flask import request
    symbol = request.args.get('symbol', 'BTCUSDT')
    try:
        r = GET('/api/v2/mix/market/candles', {
            'symbol': symbol, 'productType': 'USDT-FUTURES',
            'granularity': '1m', 'limit': '60'
        })
        candles = []
        if r.get('code') == '00000':
            for c in r.get('data', []):
                candles.append({
                    'ts': int(c[0]),
                    'o':  float(c[1]),
                    'h':  float(c[2]),
                    'l':  float(c[3]),
                    'c':  float(c[4]),
                    'v':  float(c[5]),
                })
        return cors_json({'candles': candles, 'symbol': symbol})
    except Exception as e:
        return cors_json({'candles': [], 'error': str(e)})

# ══ BOT LOOP ════════════════════════════════════════════════════════════
def bot_loop():
    global S
    log.info('Portal David Bot v3 starting…')
    time.sleep(6)
    while True:
        try:
            S = scan(S)
            save_state(S)
        except Exception as e:
            log.error(f'Loop error: {e}')
            S['status'] = f'Erreur: {str(e)[:80]}'
            save_state(S)
        time.sleep(SCAN_SEC)

Thread(target=bot_loop, daemon=True).start()
log.info('Bot thread launched')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
