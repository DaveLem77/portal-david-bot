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

LEVERAGE      = 15       # levier fixe — monte à 20 si capital > 500$
RISK_PCT      = 0.90     # 90% du capital par trade
TP_PCT        = 0.05     # take profit +5% du prix → +75% sur marge à x15
SL_PCT        = 0.025    # stop loss -2.5% → -37% sur marge (jamais liquidé)
MIN_SCORE     = 62       # score minimum pour entrer
SCAN_SEC      = 50       # scan toutes les 50 secondes
MIN_VOL_24H   = 8_000_000  # volume minimum USDT

STATE_FILE = '/tmp/pdv3.json'

# ══ STATE ══════════════════════════════════════════════════════════════
def empty_state():
    return {
        'status':            'Démarrage…',
        'balance':           0.0,
        'balance_total':     0.0,
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
def score_token(ticker, c1m, c5m, c15m, c1h, weights):
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
def place_order(symbol, direction, balance, scored):
    try:
        # Levier selon capital
        lev = 20 if balance >= 500 else 15

        # Set leverage pour les deux côtés
        set_leverage(symbol, lev, 'long')
        time.sleep(0.2)
        set_leverage(symbol, lev, 'short')
        time.sleep(0.3)

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

        # Taille position — 90% du capital × levier
        risk  = balance * RISK_PCT
        size  = (risk * lev) / price
        size  = max(size, min_size)
        size  = round(size, size_dec) if size_dec > 0 else max(1, math.floor(size))

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
        time.sleep(1.5)  # Attendre que la position soit ouverte

        # ÉTAPE 2 — Poser TP/SL séparément via l'endpoint dédié
        hold_side = 'long' if direction == 'long' else 'short'

        tp_body = {
            'symbol':             symbol,
            'productType':        'USDT-FUTURES',
            'marginCoin':         'USDT',
            'planType':           'profit_loss',
            'triggerPrice':       str(tp_price),
            'triggerType':        'mark_price',
            'executePrice':       '0',
            'holdSide':           hold_side,
            'size':               str(size),
            'rangeRate':          '',
        }
        tp_r = POST('/api/v2/mix/order/place-tpsl-order', tp_body)
        log.info(f'TP response: {tp_r}')

        sl_body = {
            'symbol':             symbol,
            'productType':        'USDT-FUTURES',
            'marginCoin':         'USDT',
            'planType':           'loss_plan',
            'triggerPrice':       str(sl_price),
            'triggerType':        'mark_price',
            'executePrice':       '0',
            'holdSide':           hold_side,
            'size':               str(size),
            'rangeRate':          '',
        }
        sl_r = POST('/api/v2/mix/order/place-tpsl-order', sl_body)
        log.info(f'SL response: {sl_r}')

        if tp_r.get('code') != '00000':
            log.error(f'TP failed: {tp_r}')
        if sl_r.get('code') != '00000':
            log.error(f'SL failed: {sl_r}')

        return {
            'orderId':      order_id,
            'symbol':       symbol,
            'direction':    direction,
            'entryPrice':   price,
            'currentPrice': price,
            'size':         size,
            'leverage':     lev,
            'tp':           tp_price,
            'sl':           sl_price,
            'tp_pct':       round(TP_PCT * 100, 2),
            'sl_pct':       round(SL_PCT * 100, 2),
            'margin':       round(risk, 4),
            'openTime':     datetime.now(timezone.utc).isoformat(),
            'scoreAtEntry': scored['score'],
            'reasons':      scored['reasons'],
            'direction':    direction,
            'unrealizedPnl':0.0,
            'liqPrice':     0.0,
            'totalSize':    size,
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

        # Trailing stop — après +3%
        ep  = entry
        dir = state['position']['direction']
        if dir == 'long':
            gain_pct = (cp - ep) / ep if ep > 0 else 0
            if gain_pct >= 0.03:
                state['position']['trailing_active'] = True
                if cp > state['position'].get('trailing_high', ep):
                    state['position']['trailing_high'] = cp
                    new_sl = round(cp * 0.985, 8)
                    if new_sl > state['position']['sl']:
                        state['position']['sl'] = new_sl
                        log.info(f'Trailing SL → {new_sl}')
        else:
            gain_pct = (ep - cp) / ep if ep > 0 else 0
            if gain_pct >= 0.03:
                state['position']['trailing_active'] = True
                if cp < state['position'].get('trailing_high', ep):
                    state['position']['trailing_high'] = cp
                    new_sl = round(cp * 1.015, 8)
                    if new_sl < state['position']['sl']:
                        state['position']['sl'] = new_sl
                        log.info(f'Trailing SL → {new_sl}')

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
        state['status']        = f'Fermé manuellement — PNL: {pnl:+.4f} USDT'
        state = update_weights(state)

    return state

# ══ MAIN SCAN ══════════════════════════════════════════════════════════
def scan(state):
    state['last_scan'] = datetime.now(timezone.utc).isoformat()
    today = str(datetime.now(timezone.utc).date())
    if state.get('today_date') != today:
        state['today_pnl'] = 0.0
        state['today_date'] = today

    # Vérifier fermeture manuelle en premier
    state = detect_manual_close(state)

    # Si position ouverte — juste surveiller
    if state['position']:
        state = check_position(state)
        pos = state['position']
        if pos:
            unr = pos.get('unrealizedPnl', 0)
            pct = (unr / pos.get('margin', 1) * 100) if pos.get('margin') else 0
            trail = ' | Trailing actif' if pos.get('trailing_active') else ''
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

        res = score_token(tk, c1m, c5m, c15m, c1h, weights)
        if res and res['score'] >= MIN_SCORE:
            candidates.append({'symbol': sym, **res})
            log.info(f'Candidate: {sym} score={res["score"]} dir={res["direction"]}')

    state['status'] = f'Scan terminé — {len(candidates)} signaux sur {len(top)} paires'

    if not candidates:
        state['status'] = 'Aucun signal fort — surveillance continue'
        return state

    best = sorted(candidates, key=lambda x: x['score'], reverse=True)[0]
    log.info(f'Best: {best["symbol"]} score={best["score"]} dir={best["direction"]}')

    state['status'] = f'Signal: {best["symbol"]} {best["direction"].upper()} score={best["score"]} — Ouverture…'

    pos = place_order(best['symbol'], best['direction'], state['balance'], best)
    if pos:
        state['position'] = pos
        state['status'] = (
            f'{pos["symbol"]} {pos["direction"].upper()} x{pos["leverage"]} ouvert — '
            f'TP: +{pos["tp_pct"]}% | SL: -{pos["sl_pct"]}%'
        )
        log.info(f'Position opened: {pos["symbol"]} {pos["direction"]} x{pos["leverage"]}')
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
