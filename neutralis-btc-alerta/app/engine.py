import csv
import datetime as dt
import json
import math
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
SEED = BASE / 'btc_usd_daily.csv'
BITSTAMP = 'https://www.bitstamp.net/api/v2'


def request_json(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': 'UmbrelBTCAlerta/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def read_seed():
    if not SEED.exists():
        return {}
    with SEED.open(newline='') as f:
        return {r['date_utc']: {'date': r['date_utc'], 'open': float(r['open']),
                'high': float(r['high']), 'low': float(r['low']),
                'close': float(r['close'])} for r in csv.DictReader(f)}


def fetch_market(full_history=False):
    today = dt.datetime.now(dt.timezone.utc).date()
    # Fetch completed daily candles only. The ticker is used for intraday region alerts.
    rows = {}
    end = None
    while True:
        url = f'{BITSTAMP}/ohlc/btcusd/?step=86400&limit=1000&exclude_current_candle=true'
        if end:
            url += f'&end={end}'
        payload = request_json(url)
        candles = payload['data']['ohlc']
        if not candles:
            break
        for r in candles:
            day = dt.datetime.fromtimestamp(int(r['timestamp']), dt.timezone.utc).date()
            if day < today:
                rows[day.isoformat()] = {'date': day.isoformat(), **{
                    k: float(r[k]) for k in ('open', 'high', 'low', 'close')}}
        oldest = min(int(r['timestamp']) for r in candles)
        if not full_history or oldest <= int(dt.datetime(2011, 8, 18, tzinfo=dt.timezone.utc).timestamp()):
            break
        end = oldest - 1
    ticker = request_json(f'{BITSTAMP}/ticker/btcusd/')
    ticker_time = dt.datetime.fromtimestamp(int(ticker['timestamp']), dt.timezone.utc)
    if (dt.datetime.now(dt.timezone.utc) - ticker_time).total_seconds() > 900:
        raise ValueError('Cotação da Bitstamp está atrasada mais de 15 minutos')
    return rows, float(ticker['last']), ticker_time.isoformat()


def weekly_ema(daily, period=50):
    # Sunday UTC close represents a weekly candle ending Monday 00:00 UTC.
    Sundays = [r for r in sorted(daily.values(), key=lambda x: x['date'])
               if dt.date.fromisoformat(r['date']).weekday() == 6]
    if len(Sundays) < period + 2:
        raise ValueError('Histórico insuficiente para EMA50 semanal')
    k = 2 / (period + 1)
    ema = sum(r['close'] for r in Sundays[:period]) / period
    result = []
    for r in Sundays[period - 1:]:
        if r is not Sundays[period - 1]:
            ema = r['close'] * k + ema * (1 - k)
        result.append({'date': r['date'], 'close': r['close'], 'ema': ema,
                       'above': r['close'] > ema})
    return result


def previous_cycle_top(daily):
    # Use the 2020-2021 bull peak, excluding the pre-halving 2024 rally.
    vals = [r['high'] for d, r in daily.items() if '2020-05-11' <= d < '2023-01-01']
    return max(vals) if vals else 69000.0


def market_snapshot(daily, price, quote_time, top_override=None):
    weeks = weekly_ema(daily)
    top = float(top_override) if top_override else previous_cycle_top(daily)
    return {'price': price, 'quote_time': quote_time, 'week': weeks[-1],
            'prev_week': weeks[-2], 'previous_top': top,
            'zone_low': top * .80, 'zone_high': top * .95,
            'weekly': weeks[-156:]}


def event_scan(snapshot, memory):
    week, prev = snapshot['week'], snapshot['prev_week']
    events = []
    last_week = memory.get('last_week')
    # Never issue a backdated alert on first launch. Show the current regime in UI.
    if last_week is not None and week['date'] != last_week:
        if prev['above'] and not week['above']:
            memory['waiting_to_buy'] = True
            events.append({'key': f"sell:{week['date']}", 'kind': 'sell',
                           'title': 'Sinal de venda: 70% por rede',
                           'detail': 'Fechamento semanal cruzou abaixo da EMA50.'})
        elif not prev['above'] and week['above'] and memory.get('waiting_to_buy'):
            memory['waiting_to_buy'] = False
            events.append({'key': f"buy-ema:{week['date']}", 'kind': 'buy',
                           'title': 'Sinal de recompra',
                           'detail': 'Fechamento semanal cruzou acima da EMA50.'})
    memory['last_week'] = week['date']
    in_zone = snapshot['zone_low'] <= snapshot['price'] <= snapshot['zone_high']
    if memory.get('waiting_to_buy') and in_zone and not memory.get('was_in_zone', False):
        memory['waiting_to_buy'] = False
        events.append({'key': f"buy-zone:{snapshot['quote_time']}", 'kind': 'buy',
                       'title': 'Preço entrou na região de recompra',
                       'detail': 'BTC/USD entrou entre 80% e 95% do topo anterior.'})
    memory['was_in_zone'] = in_zone
    return events


def sale_lines(locations):
    return [{'network': p['network'], 'asset': p['asset'],
             'quantity': p['quantity'], 'sell_70': round(p['quantity'] * .7, 8)}
            for p in locations if p.get('quantity') is not None]
