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

DATA = Path(os.environ.get('APP_DATA_DIR', '/data'))
DATA.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA / 'state.json'
DEFAULT = {
    'locations': [
        {'network': 'Base', 'asset': 'cbBTC · Aave', 'quantity': 0.2631},
        {'network': 'Ethereum', 'asset': 'cbBTC · Aave', 'quantity': 0.0256},
        {'network': 'BNB Chain', 'asset': 'BTCB · Aave', 'quantity': 0.0439},
        {'network': 'Solana', 'asset': 'BTC · Kamino', 'quantity': None},
    ],
    'top_override': None,
    'telegram_token': '', 'telegram_chat_id': '',
    'memory': {}, 'alerts': [],
}


def load_state():
    if STATE_FILE.exists():
        return {**DEFAULT, **json.loads(STATE_FILE.read_text())}
    return json.loads(json.dumps(DEFAULT))


def save_state():
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


state = load_state()
seed = read_seed()
HISTORY_FILE = DATA / 'history.json'
daily = {**seed, **(json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {})}
latest = None
error = 'Aguardando primeira atualização da Bitstamp.'
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
        with lock:
            state['alerts'].insert(0, {'time': dt.datetime.now(dt.timezone.utc).isoformat(),
                                       'title': 'Falha ao enviar Telegram', 'detail': str(exc)})
            save_state()


def refresh():
    global latest, error
    try:
        new_rows, price, quote_time = fetch_market(full_history=len(daily) < 3000)
        with lock:
            daily.update(new_rows)
            if new_rows:
                HISTORY_FILE.write_text(json.dumps(daily))
            latest = market_snapshot(daily, price, quote_time, state.get('top_override'))
            alerts = event_scan(latest, state['memory'])
            for item in alerts:
                item['time'] = dt.datetime.now(dt.timezone.utc).isoformat()
                if item['kind'] == 'sell':
                    lines = sale_lines(state['locations'])
                    item['lines'] = lines
                    msg = item['title'] + '\n' + item['detail'] + '\n' + '\n'.join(
                        f"{p['network']}: {p['sell_70']:.8f} {p['asset']}" for p in lines)
                else:
                    msg = item['title'] + '\n' + item['detail']
                state['alerts'].insert(0, item)
                telegram(msg)
            state['alerts'] = state['alerts'][:100]
            save_state()
            error = None
    except Exception as exc:
        with lock:
            error = str(exc)


def background():
    while True:
        refresh()
        time.sleep(300)


def chart_svg(weeks):
    points = weeks[-104:]
    w, h, pad = 920, 300, 45
    vals = [v for p in points for v in (p['close'], p['ema'])]
    lo, hi = min(vals) * .95, max(vals) * 1.05
    def xy(i, v):
        return f'{pad + i*(w-2*pad)/(len(points)-1):.1f},{h-pad-(v-lo)*(h-2*pad)/(hi-lo):.1f}'
    price = ' '.join(xy(i, p['close']) for i, p in enumerate(points))
    ema = ' '.join(xy(i, p['ema']) for i, p in enumerate(points))
    guides = ''.join(f'<line x1="{pad}" y1="{pad+j*(h-2*pad)/4}" x2="{w-pad}" y2="{pad+j*(h-2*pad)/4}" stroke="#27354c"/>' for j in range(5))
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Preço semanal do BTC em laranja e EMA50 em azul">{guides}<polyline points="{ema}" fill="none" stroke="#75baff" stroke-width="3"/><polyline points="{price}" fill="none" stroke="#f6b65d" stroke-width="3"/></svg>'


def page():
    with lock:
        s = json.loads(json.dumps(state))
        snap = json.loads(json.dumps(latest)) if latest else None
        err = error
    esc = html.escape
    if snap:
        price, ema = snap['price'], snap['week']['ema']
        gap = price - ema
        relation = 'acima' if gap >= 0 else 'abaixo'
        status = 'Acima da EMA50' if snap['week']['above'] else 'Abaixo da EMA50'
        summary = f'''<section class="cards"><div class="card"><small>BTC/USD · Bitstamp</small><strong>US$ {price:,.2f}</strong><span>Cotação: {esc(snap['quote_time'][:16])} UTC</span></div><div class="card"><small>EMA50 semanal</small><strong>US$ {ema:,.2f}</strong><span>Último fechamento: {snap['week']['date']}</span></div><div class="card"><small>Distância até EMA50</small><strong>{abs(gap):,.2f} USD</strong><span>{abs(gap)/ema*100:.2f}% {relation} da média</span></div><div class="card"><small>Regime</small><strong>{status}</strong><span>Faixa de compra: US$ {snap['zone_low']:,.0f}–{snap['zone_high']:,.0f}</span></div></section>'''
        chart = chart_svg(snap['weekly'])
        top = snap['previous_top']
    else:
        summary, chart, top = '<p>Aguardando cotação atual.</p>', '', None
    price_for_value = snap['price'] if snap else None
    rows = ''.join(f'''<tr><td>{esc(p['network'])}</td><td>{esc(p['asset'])}</td><td>{'—' if p['quantity'] is None else f"{p['quantity']:.8f}"}</td><td>{'—' if p['quantity'] is None or price_for_value is None else f"US$ {p['quantity']*price_for_value:,.2f}"}</td><td>{'—' if p['quantity'] is None else f"{p['quantity']*.7:.8f}"}</td><td>{'—' if p['quantity'] is None or price_for_value is None else f"US$ {p['quantity']*.7*price_for_value:,.2f}"}</td></tr>''' for p in s['locations'])
    known_total = sum(p['quantity'] for p in s['locations'] if p['quantity'] is not None)
    unknown_count = sum(p['quantity'] is None for p in s['locations'])
    rows += f'''<tr class="total"><td>Total informado</td><td>{unknown_count} posição(ões) sem quantidade</td><td>{known_total:.8f}</td><td>{'—' if price_for_value is None else f"US$ {known_total*price_for_value:,.2f}"}</td><td>{known_total*.7:.8f}</td><td>{'—' if price_for_value is None else f"US$ {known_total*.7*price_for_value:,.2f}"}</td></tr>'''
    alerts = ''.join(f'''<article class="alert"><small>{esc(a.get('time','')[:16])} UTC</small><strong>{esc(a['title'])}</strong><p>{esc(a.get('detail',''))}</p>{''.join(f"<div>{esc(x['network'])}: {x['sell_70']:.8f} {esc(x['asset'])}</div>" for x in a.get('lines',[]))}</article>''' for a in s['alerts'][:20]) or '<p>Nenhum alerta novo desde a instalação.</p>'
    config_rows = ''.join(f'''<div class="position"><input name="network_{i}" value="{esc(p['network'], quote=True)}" aria-label="Rede {i+1}"><input name="asset_{i}" value="{esc(p['asset'], quote=True)}" aria-label="Ativo {i+1}"><input name="quantity_{i}" value="{'' if p['quantity'] is None else p['quantity']}" type="number" min="0" step="any" placeholder="BTC" aria-label="Quantidade {i+1}"></div>''' for i,p in enumerate(s['locations']))
    return f'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BTC · Sinais semanais</title><style>
    :root{{color-scheme:dark;font-family:system-ui,sans-serif;background:#0a1220;color:#e8eef7}}body{{max-width:1100px;margin:0 auto;padding:30px 20px}}h1{{font-size:2rem;margin-bottom:4px}}p,small,span{{color:#aab9ce}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:24px 0}}.card,.panel,.alert{{background:#121f31;border:1px solid #27354c;border-radius:16px;padding:20px}}.card strong{{display:block;font-size:1.6rem;margin:8px 0}}.card span{{font-size:.85rem}}.panel{{margin:20px 0}}svg{{width:100%;height:auto}}.legend{{display:flex;gap:24px}}.legend b:first-child{{color:#f6b65d}}.legend b:last-child{{color:#75baff}}.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:760px}}td,th{{padding:11px;border-bottom:1px solid #27354c;text-align:left}}.total{{font-weight:700;background:#1a2a40}}input{{background:#0a1220;color:#e8eef7;border:1px solid #43536b;border-radius:8px;padding:10px;min-width:0}}.position{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin:8px 0}}button{{background:#70aff4;color:#081321;border:0;border-radius:9px;padding:11px 18px;font-weight:bold;cursor:pointer}}.alert{{margin:10px 0}}.alert strong{{display:block;margin:5px 0}}.warning{{color:#f4be70}}@media(max-width:600px){{.position{{grid-template-columns:1fr}}}} </style><body><header><h1>BTC · Sinais semanais</h1><p>EMA50 · venda de 70% por rede · recompra na faixa do ciclo ou no cruzamento de alta</p></header>{f'<p class="warning">Fonte de preço indisponível: {esc(err)}</p>' if err else ''}{summary}<section class="panel"><h2>Preço e EMA50</h2><div class="legend"><b>● BTC/USD</b><b>● EMA50</b></div>{chart}<p>Gráfico dos últimos 104 fechamentos semanais. O preço atual e a distância aparecem acima.</p></section><section class="panel"><h2>Quantidades por rede</h2><div class="table-wrap"><table><thead><tr><th>Rede</th><th>Ativo</th><th>BTC informado</th><th>Valor estimado</th><th>70% para venda</th><th>Valor dos 70%</th></tr></thead><tbody>{rows}</tbody></table></div><p>Valores em USD estimados pela cotação BTC/USD da Bitstamp, não pela cotação individual de cbBTC ou BTCB. O total soma somente as posições com quantidade informada. Valores iniciais consultados em 23/11/2026; atualize-os quando suas posições mudarem. A Solana está sem quantidade confirmada.</p></section><section class="panel"><h2>Alertas</h2>{alerts}</section><section class="panel"><h2>Configurar</h2><form method="post" action="/settings"><h3>Posições</h3>{config_rows}<p>Para incluir outra rede, preencha a linha abaixo.</p><div class="position"><input name="new_network" placeholder="Nova rede"><input name="new_asset" placeholder="Ativo/protocolo"><input name="new_quantity" type="number" min="0" step="any" placeholder="BTC"></div><h3>Ciclo</h3><p>Topo anterior usado: US$ {top:,.2f} · faixa de recompra: 80% a 95%. Se iniciar outro ciclo, informe o novo topo anterior.</p><input name="top_override" type="number" min="1" step="any" placeholder="Topo anterior automático" value="{s['top_override'] or ''}"><h3>Telegram (opcional)</h3><input name="telegram_token" type="password" placeholder="Token do bot" value="{esc(s.get('telegram_token',''),quote=True)}"><input name="telegram_chat_id" placeholder="Chat ID" value="{esc(s.get('telegram_chat_id',''),quote=True)}"><p><button type="submit">Salvar</button></p></form></section><footer><p>Sinais informativos. O app não movimenta fundos. Fonte: Bitstamp BTC/USD; vela semanal fecha domingo 23:59:59 UTC.</p></footer></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body = b'ok'
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if self.path != '/':
            self.send_error(404); return
        body = page().encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if self.path != '/settings':
            self.send_error(404); return
        size = int(self.headers.get('Content-Length', '0'))
        if size > 100_000:
            self.send_error(413); return
        form = {k: v[0] for k,v in urllib.parse.parse_qs(self.rfile.read(size).decode(), keep_blank_values=True).items()}
        try:
            with lock:
                locations = []
                for i in range(len(state['locations'])):
                    network, asset, raw = (form[f'{k}_{i}'].strip() for k in ('network','asset','quantity'))
                    if network and asset:
                        qty = float(raw) if raw else None
                        if qty is not None and (qty < 0 or qty > 1_000_000): raise ValueError('Quantidade inválida')
                        locations.append({'network': network[:60], 'asset': asset[:100], 'quantity': qty})
                if form.get('new_network','').strip() and form.get('new_asset','').strip():
                    raw = form.get('new_quantity','').strip()
                    qty = float(raw) if raw else None
                    if qty is not None and (qty < 0 or qty > 1_000_000): raise ValueError('Quantidade inválida')
                    locations.append({'network': form['new_network'].strip()[:60], 'asset': form['new_asset'].strip()[:100], 'quantity': qty})
                top = form.get('top_override','').strip()
                top = float(top) if top else None
                if top is not None and (top < 1 or top > 100_000_000): raise ValueError('Topo inválido')
                state.update(locations=locations, top_override=top,
                             telegram_token=form.get('telegram_token','').strip(),
                             telegram_chat_id=form.get('telegram_chat_id','').strip())
                save_state()
            self.send_response(303); self.send_header('Location','/'); self.end_headers()
        except (ValueError, KeyError) as exc:
            self.send_error(400, str(exc))


if __name__ == '__main__':
    threading.Thread(target=background, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
