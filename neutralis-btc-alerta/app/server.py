import datetime as dt
import html
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import read_seed, fetch_market, market_snapshot, event_scan, sale_lines

SYMBOLS = ('BTC', 'ETH', 'SOL')
DATA = Path(os.environ.get('APP_DATA_DIR', '/data'))
DATA.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA / 'state.json'
DEFAULT = {
    'locations': [
        {'symbol': 'BTC', 'network': 'Base', 'asset': 'cbBTC · Aave', 'quantity': 0.2631},
        {'symbol': 'BTC', 'network': 'Ethereum', 'asset': 'cbBTC · Aave', 'quantity': 0.0256},
        {'symbol': 'BTC', 'network': 'BNB Chain', 'asset': 'BTCB · Aave', 'quantity': 0.0439},
        {'symbol': 'BTC', 'network': 'Solana', 'asset': 'BTC · Kamino', 'quantity': None},
        {'symbol': 'ETH', 'network': 'Ethereum', 'asset': 'ETH', 'quantity': None},
        {'symbol': 'SOL', 'network': 'Solana', 'asset': 'SOL', 'quantity': None},
    ],
    'top_overrides': {}, 'telegram_token': '', 'telegram_chat_id': '',
    'memory': {}, 'alerts': [],
}


def load_state():
    if not STATE_FILE.exists():
        return json.loads(json.dumps(DEFAULT))
    old = json.loads(STATE_FILE.read_text())
    state = {**DEFAULT, **old}
    for p in state['locations']:
        p.setdefault('symbol', 'BTC')
    for symbol in ('ETH', 'SOL'):
        if not any(p['symbol'] == symbol for p in state['locations']):
            state['locations'].extend(p for p in DEFAULT['locations'] if p['symbol'] == symbol)
    if 'top_override' in old and old['top_override']:
        state.setdefault('top_overrides', {})['BTC'] = old['top_override']
    if 'last_week' in state['memory']:
        state['memory'] = {'BTC': state['memory']}
    return state


def save_state():
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temp.replace(STATE_FILE)


state = load_state()
daily = {}
for symbol in SYMBOLS:
    history_file = DATA / f'history-{symbol.lower()}.json'
    legacy_file = DATA / 'history.json' if symbol == 'BTC' else history_file
    historic = json.loads(legacy_file.read_text()) if legacy_file.exists() else {}
    daily[symbol] = {**read_seed(symbol), **historic}
latest = {}
errors = {symbol: 'Aguardando primeira atualização.' for symbol in SYMBOLS}
lock = threading.RLock()


def telegram(message):
    token, chat_id = state.get('telegram_token'), state.get('telegram_chat_id')
    if not token or not chat_id:
        return
    url = 'https://api.telegram.org/bot' + token + '/sendMessage'
    body = urllib.parse.urlencode({'chat_id': chat_id, 'text': message}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, body), timeout=15).read()
    except Exception as exc:
        state['alerts'].insert(0, {'symbol': 'ALL', 'time': dt.datetime.now(dt.timezone.utc).isoformat(),
                                   'title': 'Falha ao enviar Telegram', 'detail': str(exc)})


def refresh_symbol(symbol):
    try:
        new_rows, price, quote_time = fetch_market(symbol, full_history=len(daily[symbol]) < 3000)
        with lock:
            daily[symbol].update(new_rows)
            if new_rows:
                (DATA / f'history-{symbol.lower()}.json').write_text(json.dumps(daily[symbol]))
            snap = market_snapshot(daily[symbol], price, quote_time,
                                   state['top_overrides'].get(symbol), symbol)
            latest[symbol] = snap
            memory = state['memory'].setdefault(symbol, {})
            alerts = event_scan(snap, memory)
            for item in alerts:
                item['symbol'] = symbol
                item['time'] = dt.datetime.now(dt.timezone.utc).isoformat()
                if item['kind'] == 'sell':
                    lines = sale_lines(state['locations'], symbol)
                    item['lines'] = lines
                    msg = symbol + ': ' + item['title'] + '\n' + item['detail'] + '\n' + '\n'.join(
                        f"{p['network']}: {p['sell_70']:.8f} {p['asset']}" for p in lines)
                else:
                    msg = symbol + ': ' + item['title'] + '\n' + item['detail']
                state['alerts'].insert(0, item)
                telegram(msg)
            state['alerts'] = state['alerts'][:100]
            save_state()
            errors[symbol] = None
    except Exception as exc:
        with lock:
            errors[symbol] = str(exc)


def background():
    while True:
        for symbol in SYMBOLS:
            refresh_symbol(symbol)
        time.sleep(300)


def chart_svg(weeks, symbol):
    points = weeks[-104:]
    w, h, pad = 920, 300, 45
    vals = [v for p in points for v in (p['close'], p['ema'])]
    lo, hi = min(vals) * .95, max(vals) * 1.05
    def xy(i, value):
        return f'{pad + i*(w-2*pad)/(len(points)-1):.1f},{h-pad-(value-lo)*(h-2*pad)/(hi-lo):.1f}'
    price = ' '.join(xy(i, p['close']) for i, p in enumerate(points))
    ema = ' '.join(xy(i, p['ema']) for i, p in enumerate(points))
    guides = ''.join(f'<line x1="{pad}" y1="{pad+j*(h-2*pad)/4}" x2="{w-pad}" y2="{pad+j*(h-2*pad)/4}" stroke="#27354c"/>' for j in range(5))
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Preço semanal {symbol} em laranja e EMA50 em azul">{guides}<polyline points="{ema}" fill="none" stroke="#75baff" stroke-width="3"/><polyline points="{price}" fill="none" stroke="#f6b65d" stroke-width="3"/></svg>'


def page(symbol='BTC'):
    with lock:
        saved = json.loads(json.dumps(state))
        snap = json.loads(json.dumps(latest.get(symbol))) if symbol in latest else None
        error = errors[symbol]
    esc = html.escape
    positions = [p for p in saved['locations'] if p['symbol'] == symbol]
    if snap:
        price, ema = snap['price'], snap['week']['ema']
        gap = price - ema
        relation = 'acima' if gap >= 0 else 'abaixo'
        status = 'Acima da EMA50' if snap['week']['above'] else 'Abaixo da EMA50'
        summary = f'''<section class="cards"><div class="card"><small>{symbol}/USD · Bitstamp</small><strong>US$ {price:,.2f}</strong><span>Cotação: {esc(snap['quote_time'][:16])} UTC</span></div><div class="card"><small>EMA50 semanal</small><strong>US$ {ema:,.2f}</strong><span>Último fechamento: {snap['week']['date']}</span></div><div class="card"><small>Distância até EMA50</small><strong>US$ {abs(gap):,.2f}</strong><span>{abs(gap)/ema*100:.2f}% {relation} da média</span></div><div class="card"><small>Regime</small><strong>{status}</strong><span>Faixa de compra: US$ {snap['zone_low']:,.2f}–{snap['zone_high']:,.2f}</span></div></section>'''
        chart = chart_svg(snap['weekly'], symbol)
        top = snap['previous_top']
    else:
        price, top = None, None
        summary, chart = '<p>Aguardando cotação atual.</p>', ''
    rows = ''.join(f'''<tr><td>{esc(p['network'])}</td><td>{esc(p['asset'])}</td><td>{'—' if p['quantity'] is None else f"{p['quantity']:.8f}"}</td><td>{'—' if p['quantity'] is None or price is None else f"US$ {p['quantity']*price:,.2f}"}</td><td>{'—' if p['quantity'] is None else f"{p['quantity']*.7:.8f}"}</td><td>{'—' if p['quantity'] is None or price is None else f"US$ {p['quantity']*.7*price:,.2f}"}</td></tr>''' for p in positions)
    total = sum(p['quantity'] for p in positions if p['quantity'] is not None)
    missing = sum(p['quantity'] is None for p in positions)
    rows += f'''<tr class="total"><td>Total informado</td><td>{missing} posição(ões) sem quantidade</td><td>{total:.8f}</td><td>{'—' if price is None else f"US$ {total*price:,.2f}"}</td><td>{total*.7:.8f}</td><td>{'—' if price is None else f"US$ {total*.7*price:,.2f}"}</td></tr>'''
    selected_alerts = [a for a in saved['alerts'] if a.get('symbol', 'BTC') in (symbol, 'ALL')]
    alert_html = ''.join(f'''<article class="alert"><small>{esc(a.get('time','')[:16])} UTC</small><strong>{esc(a['title'])}</strong><p>{esc(a.get('detail',''))}</p>{''.join(f"<div>{esc(x['network'])}: {x['sell_70']:.8f} {esc(x['asset'])}</div>" for x in a.get('lines',[]))}</article>''' for a in selected_alerts[:20]) or '<p>Nenhum alerta novo desde a instalação.</p>'
    config_rows = ''.join(f'''<div class="position"><input name="network_{i}" value="{esc(p['network'], quote=True)}" aria-label="Rede {i+1}"><input name="asset_{i}" value="{esc(p['asset'], quote=True)}" aria-label="Ativo {i+1}"><input name="quantity_{i}" value="{'' if p['quantity'] is None else p['quantity']}" type="number" min="0" step="any" placeholder="{symbol}" aria-label="Quantidade {i+1}"></div>''' for i,p in enumerate(positions))
    tabs = ''.join(f'<a class="tab {"active" if x == symbol else ""}" href="/?asset={x}">{x}</a>' for x in SYMBOLS)
    default_top = {'BTC': 69000, 'ETH': 4868.79, 'SOL': 260}[symbol]
    top_text = f'US$ {top:,.2f}' if top is not None else f'US$ {default_top:,.2f} (inicial)'
    top_override = saved['top_overrides'].get(symbol) or ''
    return f'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{symbol} · Sinais semanais</title><style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif;background:#0a1220;color:#e8eef7}}body{{max-width:1100px;margin:0 auto;padding:30px 20px}}h1{{font-size:2rem;margin-bottom:4px}}p,small,span{{color:#aab9ce}}.tabs{{display:flex;gap:8px;margin:22px 0}}.tab{{padding:10px 20px;border-radius:9px;background:#18283e;color:#c6d8eb;text-decoration:none}}.tab.active{{background:#70aff4;color:#081321;font-weight:700}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:24px 0}}.card,.panel,.alert{{background:#121f31;border:1px solid #27354c;border-radius:16px;padding:20px}}.card strong{{display:block;font-size:1.6rem;margin:8px 0}}.card span{{font-size:.85rem}}.panel{{margin:20px 0}}svg{{width:100%;height:auto}}.legend{{display:flex;gap:24px}}.legend b:first-child{{color:#f6b65d}}.legend b:last-child{{color:#75baff}}.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:760px}}td,th{{padding:11px;border-bottom:1px solid #27354c;text-align:left}}.total{{font-weight:700;background:#1a2a40}}input{{background:#0a1220;color:#e8eef7;border:1px solid #43536b;border-radius:8px;padding:10px;min-width:0}}.position{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin:8px 0}}button{{background:#70aff4;color:#081321;border:0;border-radius:9px;padding:11px 18px;font-weight:bold;cursor:pointer}}.alert{{margin:10px 0}}.alert strong{{display:block;margin:5px 0}}.warning{{color:#f4be70}}@media(max-width:600px){{.position{{grid-template-columns:1fr}}}}</style><body><header><h1>{symbol} · Sinais semanais</h1><p>EMA50 · venda de 70% por rede · recompra na faixa do ciclo ou no cruzamento de alta</p><nav class="tabs">{tabs}</nav></header>{f'<p class="warning">Fonte de preço indisponível: {esc(error)}</p>' if error else ''}{summary}<section class="panel"><h2>Preço e EMA50</h2><div class="legend"><b>● {symbol}/USD</b><b>● EMA50</b></div>{chart}<p>Últimos 104 fechamentos semanais. Preço atual e distância aparecem acima.</p></section><section class="panel"><h2>Quantidades por rede</h2><div class="table-wrap"><table><thead><tr><th>Rede</th><th>Ativo</th><th>{symbol} informado</th><th>Valor estimado</th><th>70% para venda</th><th>Valor dos 70%</th></tr></thead><tbody>{rows}</tbody></table></div><p>Valores em USD estimados por {symbol}/USD na Bitstamp. O total soma apenas posições com quantidade informada; atualize-as manualmente quando mudarem.</p></section><section class="panel"><h2>Alertas</h2>{alert_html}</section><section class="panel"><h2>Configurar {symbol}</h2><form method="post" action="/settings"><input type="hidden" name="symbol" value="{symbol}"><h3>Posições</h3>{config_rows}<p>Para incluir outra rede, preencha a linha abaixo.</p><div class="position"><input name="new_network" placeholder="Nova rede"><input name="new_asset" placeholder="Ativo/protocolo"><input name="new_quantity" type="number" min="0" step="any" placeholder="{symbol}"></div><h3>Ciclo</h3><p>Topo anterior usado: {top_text} · faixa de recompra: 80% a 95%. Atualize o topo se iniciar outro ciclo.</p><input name="top_override" type="number" min="1" step="any" placeholder="Topo anterior automático" value="{top_override}"><h3>Telegram (opcional, comum aos três ativos)</h3><input name="telegram_token" type="password" placeholder="Token do bot" value="{esc(saved.get('telegram_token',''),quote=True)}"><input name="telegram_chat_id" placeholder="Chat ID" value="{esc(saved.get('telegram_chat_id',''),quote=True)}"><p><button type="submit">Salvar</button></p></form></section><footer><p>Sinais informativos. O app não movimenta fundos. Fonte: Bitstamp; vela semanal fecha domingo 23:59:59 UTC.</p></footer></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body = b'ok'
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        query = urllib.parse.urlparse(self.path)
        if query.path != '/':
            self.send_error(404); return
        symbol = urllib.parse.parse_qs(query.query).get('asset', ['BTC'])[0].upper()
        if symbol not in SYMBOLS:
            self.send_error(400); return
        body = page(symbol).encode()
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if self.path != '/settings':
            self.send_error(404); return
        size = int(self.headers.get('Content-Length', '0'))
        if size > 100_000:
            self.send_error(413); return
        form = {k: v[0] for k,v in urllib.parse.parse_qs(self.rfile.read(size).decode(), keep_blank_values=True).items()}
        symbol = form.get('symbol', 'BTC').upper()
        if symbol not in SYMBOLS:
            self.send_error(400); return
        try:
            with lock:
                old_positions = [p for p in state['locations'] if p['symbol'] == symbol]
                positions = []
                for i in range(len(old_positions)):
                    network, asset, raw = (form[f'{k}_{i}'].strip() for k in ('network', 'asset', 'quantity'))
                    if network and asset:
                        qty = float(raw) if raw else None
                        if qty is not None and (qty < 0 or qty > 1_000_000): raise ValueError('Quantidade inválida')
                        positions.append({'symbol': symbol, 'network': network[:60], 'asset': asset[:100], 'quantity': qty})
                if form.get('new_network','').strip() and form.get('new_asset','').strip():
                    raw = form.get('new_quantity','').strip()
                    qty = float(raw) if raw else None
                    if qty is not None and (qty < 0 or qty > 1_000_000): raise ValueError('Quantidade inválida')
                    positions.append({'symbol': symbol, 'network': form['new_network'].strip()[:60],
                                      'asset': form['new_asset'].strip()[:100], 'quantity': qty})
                top = form.get('top_override','').strip()
                top = float(top) if top else None
                if top is not None and (top < 1 or top > 100_000_000): raise ValueError('Topo inválido')
                state['locations'] = [p for p in state['locations'] if p['symbol'] != symbol] + positions
                if top is None:
                    state['top_overrides'].pop(symbol, None)
                else:
                    state['top_overrides'][symbol] = top
                state['telegram_token'] = form.get('telegram_token','').strip()
                state['telegram_chat_id'] = form.get('telegram_chat_id','').strip()
                save_state()
            self.send_response(303); self.send_header('Location', '/?asset=' + symbol); self.end_headers()
        except (ValueError, KeyError) as exc:
            self.send_error(400, str(exc))


if __name__ == '__main__':
    threading.Thread(target=background, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
