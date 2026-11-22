#!/usr/bin/env python3
"""
Виджет мероприятий qtickets для Telegram Mini App.

Сервер держит API-ключ qtickets у себя и отдаёт браузеру только
готовый список мероприятий. Ключ в клиент не попадает.

Запуск:
    export QTICKETS_KEY="ваш_ключ"
    python3 server.py
"""

import hashlib
import hmac
import re
import sqlite3
import json
import os
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote

try:                       # сжатие постеров, если установлен Pillow
    from PIL import Image
    import io
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, 'static')
IMGCACHE = os.path.join(HERE, 'cache', 'img')

API_KEY = os.environ.get('QTICKETS_KEY', '')
API_BASE = 'https://qtickets.ru/api/rest/v1'
PORT = int(os.environ.get('PORT', '8000'))
CACHE_TTL = 300          # секунд; за это время к qtickets не ходим
MAX_PAGES = 4            # сколько страниц по 50 тянуть за раз
PER_PAGE = 50
GRACE = timedelta(hours=2)   # событие считаем актуальным ещё 2 часа после начала

MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']

_cache = {'at': 0, 'payload': None}
_lock = threading.Lock()


# ---------------------------------------------------------------- qtickets

def qtickets(path, body=None):
    req = urllib.request.Request(
        f'{API_BASE}/{path}',
        data=json.dumps(body).encode('utf-8') if body is not None else None,
        headers={
            'Authorization': f'Bearer {API_KEY}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        },
        method='GET',
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


def effective_price(price_obj, now):
    """Цена с учётом временных надбавок (modifiers)."""
    if not price_obj:
        return None
    base = price_obj.get('default_price')
    for m in price_obj.get('modifiers') or []:
        if m.get('type') != 'date':
            continue
        start, end = parse_dt(m.get('active_from')), parse_dt(m.get('active_to'))
        if start and end and start <= now <= end:
            try:
                return int(m.get('value'))
            except (TypeError, ValueError):
                pass
    try:
        return int(base) if base is not None else None
    except (TypeError, ValueError):
        return None


def human_date(dt):
    """'29 ноября, сб · 18:30' с пометкой сегодня/завтра."""
    if not dt:
        return ''
    now = datetime.now(dt.tzinfo)
    delta = (dt.date() - now.date()).days
    if delta == 0:
        label = 'Сегодня'
    elif delta == 1:
        label = 'Завтра'
    else:
        label = f'{dt.day} {MONTHS[dt.month - 1]}, {WEEKDAYS[dt.weekday()]}'
    return f'{label} · {dt.strftime("%H:%M")}'


def normalize(event, now):
    """Оставляем только то, что нужно виджету."""
    shows = [s for s in (event.get('shows') or [])
             if s.get('is_active') and not s.get('deleted_at')]
    upcoming = []
    for s in shows:
        start = parse_dt(s.get('start_date'))
        finish_sale = parse_dt(s.get('sale_finish_date'))
        if not start:
            continue
        if start + GRACE < now:
            continue
        if finish_sale and finish_sale < now:
            continue
        upcoming.append((start, s))
    if not upcoming:
        return None

    start, show = min(upcoming, key=lambda x: x[0])
    prices = [p for p in (
        effective_price(show.get('min_price'), now),
        effective_price(show.get('max_price'), now),
    ) if p]
    low = min(prices) if prices else None
    high = max(prices) if prices else None

    poster = event.get('poster') or {}
    city = event.get('city') or {}
    desc = (event.get('description') or '').replace('\r\n', '\n').strip()

    return {
        'id': event.get('id'),
        'name': (event.get('name') or '').strip(),
        'slug': event.get('slug') or '',
        'date': start.isoformat(),
        'date_human': human_date(start),
        'ts': int(start.timestamp()),
        'place': event.get('place_name') or '',
        'address': event.get('place_address') or '',
        'city': city.get('name') or '',
        'age': event.get('age') or '',
        'poster': poster.get('url') or '',
        'poster_thumb': thumb_url(poster.get('url'), 320),
        'poster_big': thumb_url(poster.get('url'), 640),
        'description': desc,
        'price_from': low,
        'price_to': high,
        'url': event.get('url') or '',
        'shows_total': len(upcoming),
    }


ALLOWED_IMG_HOSTS = ('qtickets.ru', 'cdn.qtickets.tech', 'qtickets.events')


def thumb_url(url, width):
    """Ссылка на постер через наш прокси (сжатие + кеш)."""
    if not url:
        return ''
    return f'/api/img?w={int(width)}&src={quote(url, safe="")}'


def fetch_image(url, width):
    """Возвращает (bytes, content_type). Сжимает, если доступен Pillow."""
    os.makedirs(IMGCACHE, exist_ok=True)
    key = hashlib.sha1(f'{url}|{width}'.encode('utf-8')).hexdigest()
    path = os.path.join(IMGCACHE, key + '.bin')
    meta = path + '.type'

    if os.path.exists(path) and os.path.exists(meta):
        with open(path, 'rb') as f:
            data = f.read()
        with open(meta, encoding='utf-8') as f:
            return data, f.read().strip()

    req = urllib.request.Request(url, headers={'User-Agent': 'heckle-widget/1.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        ctype = r.headers.get('Content-Type', 'image/jpeg')

    out, out_type = raw, ctype
    if HAVE_PIL:
        try:
            im = Image.open(io.BytesIO(raw))
            im = im.convert('RGB')
            if im.width > width:
                h = round(im.height * width / im.width)
                im = im.resize((width, h), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=82, optimize=True)
            out, out_type = buf.getvalue(), 'image/jpeg'
        except Exception:
            out, out_type = raw, ctype

    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(out)
    os.replace(tmp, path)
    with open(meta, 'w', encoding='utf-8') as f:
        f.write(out_type)
    return out, out_type


def build_feed():
    """Собираем ближайшие мероприятия. Результат кешируется."""
    now = datetime.now(timezone.utc)
    # фильтр по дате сеанса — один запрос вместо перебора страниц
    since = (now - GRACE).astimezone(timezone(timedelta(hours=3)))
    since_str = since.strftime('%Y-%m-%d %H:%M:%S')

    items, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        data = qtickets('events', {
            'where': [
                {'column': 'is_active', 'operator': '=', 'value': 1},
                {'column': 'shows.start_date', 'operator': '>=', 'value': since_str},
            ],
            'orderBy': {'id': 'desc'},
            'page': page,
            'perPage': PER_PAGE,
        })
        batch = data.get('data') or []
        for ev in batch:
            if not ev.get('is_active') or ev.get('deleted_at'):
                continue
            norm = normalize(ev, now)
            if norm and norm['id'] not in seen:
                seen.add(norm['id'])
                items.append(norm)
        total = (data.get('paging') or {}).get('total') or 0
        if len(batch) < PER_PAGE or page * PER_PAGE >= total:
            break

    items.sort(key=lambda x: x['ts'])
    return items



def get_feed(force=False):
    with _lock:
        fresh = time.time() - _cache['at'] < CACHE_TTL
        if _cache['payload'] is not None and fresh and not force:
            return _cache['payload'], True
    try:
        items = build_feed()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        with _lock:
            if _cache['payload'] is not None:
                return _cache['payload'], True      # отдаём устаревшее, но рабочее
        raise RuntimeError(f'qtickets недоступен: {e}')
    with _lock:
        _cache['at'] = time.time()
        _cache['payload'] = items
    return items, False


# ---------------------------------------------------------------- HTTP

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
TG_API = os.environ.get('TELEGRAM_API', 'https://api.telegram.org')
DB_PATH = os.path.join(HERE, 'data.sqlite3')
REMIND_FROM_H = 20
REMIND_TO_H = 28
REMIND_EVERY = 1800
ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW = 900
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

_db_lock = threading.Lock()
_attempts = {}
_event_cache = {}


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS bindings ('
                 'tg_id INTEGER NOT NULL, order_id INTEGER NOT NULL, bound_at INTEGER NOT NULL, '
                 'PRIMARY KEY (tg_id, order_id))')
    conn.execute('CREATE TABLE IF NOT EXISTS reminders ('
                 'basket_id INTEGER PRIMARY KEY, tg_id INTEGER NOT NULL, sent_at INTEGER NOT NULL)')
    return conn


def verify_init_data(init_data):
    """Telegram ID пользователя, если подпись initData верна. Иначе None."""
    if not BOT_TOKEN or not init_data:
        return None
    pairs = {}
    for part in init_data.split('&'):
        if '=' in part:
            k, v = part.split('=', 1)
            pairs[k] = v
    their = pairs.pop('hash', '')
    check = '\n'.join(f'{k}={unquote(v)}' for k, v in sorted(pairs.items()))
    secret = hmac.new(b'WebAppData', BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, their):
        return None
    try:
        if time.time() - int(pairs.get('auth_date', '0')) > 86400:
            return None
        return int(json.loads(unquote(pairs.get('user', '{}')))['id'])
    except (ValueError, KeyError, TypeError):
        return None


def norm_phone(value):
    d = re.sub(r'\D', '', value or '')
    if len(d) == 10:
        d = '7' + d
    elif len(d) == 11 and d[0] in '78':
        d = '7' + d[1:]
    return d


def too_many(key):
    with _db_lock:
        now = time.time()
        hits = [t for t in _attempts.get(key, []) if now - t < ATTEMPT_WINDOW]
        _attempts[key] = hits
        return len(hits) >= ATTEMPT_LIMIT


def note_fail(*keys):
    with _db_lock:
        now = time.time()
        for k in keys:
            _attempts.setdefault(k, []).append(now)


def event_info(event_id):
    hit = _event_cache.get(event_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    ev = qtickets(f'events/{event_id}').get('data') or {}
    _event_cache[event_id] = (time.time(), ev)
    return ev


def find_orders(email, phone):
    """Оплаченные заказы, где совпадают и email, и телефон покупателя."""
    data = qtickets('orders', {
        'where': [
            {'column': 'client.email', 'operator': '=', 'value': email},
            {'column': 'payed', 'operator': '=', 'value': 1},
        ],
        'page': 1,
        'perPage': 50,
    })
    out = []
    for o in data.get('data') or []:
        client = o.get('client') or {}
        details = client.get('details') or {}
        if (client.get('email') or '').strip().lower() != email:
            continue
        if norm_phone(details.get('phone')) != phone:
            continue
        out.append(o)
    return out


def tickets_of(order, now):
    """Предстоящие билеты заказа."""
    try:
        ev = event_info(order['event_id'])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError):
        return []
    shows = {s.get('id'): s for s in ev.get('shows') or []}
    result = []
    for b in order.get('baskets') or []:
        if b.get('deleted_at') or b.get('refunded_at'):
            continue
        show = shows.get(b.get('show_id'))
        start = parse_dt(show.get('start_date')) if show else None
        if not start or start + GRACE < now:
            continue
        result.append({
            'basket_id': b.get('id'),
            'order_id': order.get('id'),
            'event': (ev.get('name') or '').strip(),
            'place': ev.get('place_name') or '',
            'address': ev.get('place_address') or '',
            'date_human': human_date(start),
            'ts': int(start.timestamp()),
            'seat': b.get('seat_name') or '',
            'price': b.get('price'),
            'barcode': b.get('barcode') or '',
            'pdf_url': b.get('pdf_url') or '',
        })
    return result


def bind(tg_id, order_ids):
    with _db_lock:
        conn = db()
        now = int(time.time())
        for oid in order_ids:
            conn.execute('INSERT OR IGNORE INTO bindings VALUES (?, ?, ?)', (tg_id, oid, now))
        conn.commit()
        conn.close()


def bound_orders(tg_id):
    with _db_lock:
        conn = db()
        rows = conn.execute('SELECT order_id FROM bindings WHERE tg_id = ?', (tg_id,)).fetchall()
        conn.close()
    return [r['order_id'] for r in rows]


def send_message(chat_id, text):
    body = json.dumps({'chat_id': chat_id, 'text': text}).encode('utf-8')
    req = urllib.request.Request(f'{TG_API}/bot{BOT_TOKEN}/sendMessage', data=body,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return bool(json.loads(r.read().decode('utf-8')).get('ok'))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print(f'отправка напоминания не удалась: {e.__class__.__name__}', flush=True)
        return False


def remind_once():
    if not BOT_TOKEN:
        return 0
    now = datetime.now(timezone.utc)
    with _db_lock:
        conn = db()
        pairs = conn.execute('SELECT DISTINCT tg_id, order_id FROM bindings').fetchall()
        conn.close()
    sent = 0
    for row in pairs:
        try:
            order = qtickets(f'orders/{row["order_id"]}').get('data') or {}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            continue
        if not order.get('payed') or order.get('deleted_at'):
            continue
        for t in tickets_of(order, now):
            hours = (t['ts'] - now.timestamp()) / 3600
            if not REMIND_FROM_H <= hours <= REMIND_TO_H:
                continue
            with _db_lock:
                conn = db()
                done = conn.execute('SELECT 1 FROM reminders WHERE basket_id = ?',
                                    (t['basket_id'],)).fetchone()
                if not done:
                    text = (f'Напоминание: {t["date_human"]} — {t["event"]}.\n'
                            f'{t["place"]}, {t["address"]}.\n'
                            f'Билет: {t["seat"]}. Штрихкод — в разделе «Мои билеты».')
                    if send_message(row['tg_id'], text):
                        conn.execute('INSERT INTO reminders VALUES (?, ?, ?)',
                                     (t['basket_id'], row['tg_id'], int(time.time())))
                        conn.commit()
                        sent += 1
                conn.close()
    return sent


def remind_loop():
    while True:
        try:
            remind_once()
        except Exception as e:
            print(f'цикл напоминаний: {e.__class__.__name__}', flush=True)
        time.sleep(REMIND_EVERY)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC, **kwargs)

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args), flush=True)

    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)

        if url.path == '/api/events':
            if not API_KEY:
                return self.send_json(500, {
                    'error': 'no_key',
                    'message': 'Не задан QTICKETS_KEY. Укажите ключ в переменной окружения.',
                })
            q = parse_qs(url.query)
            try:
                limit = max(1, min(60, int(q.get('limit', ['20'])[0])))
            except ValueError:
                limit = 20
            force = q.get('refresh', ['0'])[0] in ('1', 'true')
            try:
                items, cached = get_feed(force=force)
            except RuntimeError as e:
                return self.send_json(502, {'error': 'upstream', 'message': str(e)})
            return self.send_json(200, {
                'items': items[:limit],
                'total': len(items),
                'cached': cached,
                'updated': int(_cache['at']),
            })

        if url.path == '/api/img':
            q = parse_qs(url.query)
            src = (q.get('src') or [''])[0]
            try:
                width = max(80, min(1200, int(q.get('w', ['400'])[0])))
            except ValueError:
                width = 400
            host = urlparse(src).netloc.lower()
            if not src or not any(host == h or host.endswith('.' + h) for h in ALLOWED_IMG_HOSTS):
                return self.send_json(400, {'error': 'bad src'})
            try:
                data, ctype = fetch_image(src, width)
            except Exception:
                return self.send_json(502, {'error': 'image failed'})
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            return self.wfile.write(data)

        if url.path == '/api/my/tickets':
            tg_id = verify_init_data(self.headers.get('X-Telegram-Init-Data', ''))
            if not tg_id:
                return self.send_json(200, {'tickets': [], 'bound': False})
            now = datetime.now(timezone.utc)
            tickets = []
            for oid in bound_orders(tg_id):
                try:
                    order = qtickets(f'orders/{oid}').get('data') or {}
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
                    continue
                if order.get('payed') and not order.get('deleted_at'):
                    tickets += tickets_of(order, now)
            tickets.sort(key=lambda t: t['ts'])
            return self.send_json(200, {'tickets': tickets, 'bound': True})

        if url.path == '/api/health':
            return self.send_json(200, {
                'ok': not API_KEY is None and bool(API_KEY),
                'cached_items': len(_cache['payload'] or []),
            })

        if url.path == '/':
            self.path = '/index.html'
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != '/api/my/lookup':
            return self.send_json(404, {'error': 'not found'})
        length = int(self.headers.get('Content-Length', '0') or 0)
        if length <= 0 or length > 2048:
            return self.send_json(400, {'error': 'bad body'})
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.send_json(400, {'error': 'bad json'})
        if not isinstance(data, dict):
            return self.send_json(400, {'error': 'bad json'})

        email = str(data.get('email', '')).strip().lower()
        phone = norm_phone(str(data.get('phone', '')))
        if not EMAIL_RE.match(email) or len(phone) != 11:
            return self.send_json(400, {
                'error': 'bad_fields',
                'message': 'Введите email и телефон, которые указывали при покупке.'})

        ip = self.client_address[0]
        if too_many(f'ip:{ip}') or too_many(f'mail:{email}'):
            return self.send_json(429, {
                'error': 'too_many',
                'message': 'Слишком много попыток. Попробуйте через 15 минут.'})

        try:
            orders = find_orders(email, phone)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            return self.send_json(502, {'error': 'upstream',
                                        'message': 'Сервис билетов сейчас недоступен.'})
        if not orders:
            note_fail(f'ip:{ip}', f'mail:{email}')
            return self.send_json(404, {
                'error': 'not_found',
                'message': 'Заказ с такими данными не найден. Проверьте email и телефон из покупки.'})

        now = datetime.now(timezone.utc)
        tickets = []
        for o in orders:
            tickets += tickets_of(o, now)
        tickets.sort(key=lambda t: t['ts'])

        tg_id = verify_init_data(self.headers.get('X-Telegram-Init-Data', ''))
        if tg_id:
            bind(tg_id, [o['id'] for o in orders])
        return self.send_json(200, {'tickets': tickets, 'bound': bool(tg_id)})


def warm_cache():
    """Прогреваем кеш при старте, чтобы первый игрок не ждал."""
    if not API_KEY:
        return
    try:
        get_feed(force=True)
        print('Кеш афиши прогрет', flush=True)
    except Exception as e:
        print(f'Не удалось прогреть кеш: {e}', flush=True)


if __name__ == '__main__':
    os.makedirs(STATIC, exist_ok=True)
    if not API_KEY:
        print('ВНИМАНИЕ: QTICKETS_KEY не задан. Задайте его перед запуском.', flush=True)
    threading.Thread(target=warm_cache, daemon=True).start()
    if BOT_TOKEN:
        threading.Thread(target=remind_loop, daemon=True).start()
    else:
        print('Напоминания выключены: не задан BOT_TOKEN', flush=True)
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Виджет qtickets: http://localhost:{PORT}/', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
