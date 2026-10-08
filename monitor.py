"""Personal, read-only stock monitoring. Secrets come only from the environment."""
import argparse
import base64
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
import json
import math
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
SYMBOLS = ['MLP', 'RCMT', 'IHT', 'AWX', 'ANGH']
END = date(2026, 10, 16)
MAX_AGE = timedelta(minutes=30)


def session(at):
    t = at.time().replace(tzinfo=None)
    if time(4) <= t < time(9, 30):
        return 'pre-market'
    if time(9, 30) <= t < time(16):
        return 'regular'
    if time(16) <= t < time(20):
        return 'after-hours'
    return 'closed'


def phase(now):
    if now.date() > END:
        return 'expired'
    if now.weekday() >= 5:
        return 'idle'
    if time(20) <= now.time().replace(tzinfo=None):
        return 'summary'
    return 'monitor' if session(now) != 'closed' else 'idle'


def number(value, positive=False):
    try:
        value = float(value)
        return value if math.isfinite(value) and (not positive or value > 0) else None
    except (TypeError, ValueError):
        return None


def thresholds(move):
    if move is None:
        return None, 0
    direction = 'up' if move >= 0 else 'down'
    level = int((abs(Decimal(str(move))) / Decimal(5)).to_integral_value(rounding=ROUND_FLOOR))
    return direction, level


def request_json(url, method='GET', payload=None, headers=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data=body, method=method,
                      headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urlopen(request, timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except HTTPError as exc:
        # Do not print URLs: Telegram's URL contains the secret token.
        raise RuntimeError(f'HTTP {exc.code}') from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError('Network failure; request outcome may be unknown') from None


def telegram(text):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret')
    result = request_json(f'https://api.telegram.org/bot{token}/sendMessage', 'POST',
                          {'chat_id': chat, 'text': text, 'disable_web_page_preview': True})
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected message')


class State:
    """Durable repository state, with SHA compare-and-swap for every reservation."""
    def __init__(self):
        repo = os.environ['GITHUB_REPOSITORY']
        self.base = f'https://api.github.com/repos/{repo}'
        self.headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                        'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2022-11-28'}
        self.branch = os.environ.get('STATE_BRANCH', 'main')
        self.url = self.base + '/contents/monitor-state.json'
        result = request_json(self.url + '?ref=' + self.branch, headers=self.headers)
        self.sha = result['sha']
        self.data = json.loads(base64.b64decode(result['content']))
        if not isinstance(self.data.get('days'), dict):
            raise RuntimeError('Invalid monitor-state.json; stopping to prevent duplicates')

    def save(self):
        content = base64.b64encode(json.dumps(self.data, indent=2).encode()).decode()
        result = request_json(self.url, 'PUT',
            {'message': 'Save monitor notification state [skip ci]', 'content': content,
             'sha': self.sha, 'branch': self.branch}, self.headers)
        self.sha = result['content']['sha']

    def once(self, day, key, message):
        sent = self.data['days'].setdefault(day, {})
        if key in sent:
            return False
        # Reserve before sending. This prioritizes no duplicates over retries when
        # a timeout leaves delivery uncertain. Never delete state to retry blindly.
        sent[key] = {'reserved_at': datetime.now(NY).isoformat()}
        self.save()
        telegram(message)
        return True


def disable():
    repo = os.environ['GITHUB_REPOSITORY']
    request_json(f'https://api.github.com/repos/{repo}/actions/workflows/monitor.yml/disable',
                 'PUT', headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                                 'Accept': 'application/vnd.github+json'})
    print('Workflow disabled: monitoring period has ended.')


def fresh_stamp(stamp, now, summary=False):
    stamp = stamp.astimezone(NY)
    if stamp.date() != now.date() or session(stamp) == 'closed':
        return False
    anchor = now.replace(hour=20, minute=0, second=0, microsecond=0) if summary else now
    return timedelta(0) <= anchor - stamp <= MAX_AGE


def fetch(symbol, now, summary=False):
    import yfinance as yf
    result = dict(symbol=symbol, price=None, change=None, volume=None,
                  bid=None, ask=None, stamp=None, session='n/a')
    ticker = yf.Ticker(symbol)
    try:
        bars = ticker.history(period='5d', interval='5m', prepost=True,
                              auto_adjust=False, actions=False, timeout=20)
        if not bars.empty:
            bars = bars.tz_convert('America/New_York')
            cutoff = now.replace(hour=20, minute=0, second=0, microsecond=0) if summary else now
            candidates = bars[(bars.index.date == now.date()) & (bars.index <= cutoff)]
            candidates = candidates.dropna(subset=['Close'])
            candidates = candidates[candidates['Close'] > 0]
            if not candidates.empty:
                stamp = candidates.index[-1].to_pydatetime()
                if fresh_stamp(stamp, now, summary):
                    result.update(price=number(candidates.iloc[-1]['Close'], True),
                                  stamp=stamp, session=session(stamp))
    except Exception:
        print(f'{symbol}: intraday data unavailable')
    try:
        daily = ticker.history(period='1mo', interval='1d', prepost=False,
                               auto_adjust=False, actions=False, timeout=20)
        if not daily.empty:
            daily = daily.tz_convert('America/New_York')
            previous = daily[daily.index.date < now.date()].dropna(subset=['Close'])
            if not previous.empty and result['price'] is not None:
                prior = number(previous.iloc[-1]['Close'], True)
                # Reject a suspiciously old reference instead of inventing a close.
                if prior and (now.date() - previous.index[-1].date()).days <= 7:
                    result['change'] = float((Decimal(str(result['price'])) /
                                              Decimal(str(prior)) - 1) * 100)
    except Exception:
        print(f'{symbol}: prior close unavailable')
    try:
        info = ticker.get_info()
        epoch = number(info.get('regularMarketTime'), True)
        stamp = datetime.fromtimestamp(epoch, NY) if epoch else None
        if stamp and stamp.date() == now.date():
            volume = number(info.get('regularMarketVolume'))
            result['volume'] = volume if volume is not None and volume >= 0 else None
        # Yahoo does not reliably timestamp bid/ask separately. Retrieve them,
        # but display n/a unless freshness can be established independently.
        for field in ('bid', 'ask'):
            raw = number(info.get(field), True)
            ts = number(info.get(field + 'Time'), True)
            if raw and ts:
                at = datetime.fromtimestamp(ts, NY)
                if fresh_stamp(at, now, summary):
                    result[field] = raw
    except Exception:
        print(f'{symbol}: volume/bid/ask unavailable')
    return result


def fmt(value, spec='.2f'):
    return 'n/a' if value is None else format(value, spec)


def row(q):
    stamp = q['stamp'].strftime('%Y-%m-%d %H:%M %Z') if q['stamp'] else 'n/a'
    return (f"{q['symbol']} | {fmt(q['change'], '+.2f')}% | ${fmt(q['price'], '.4f')}\n"
            f"Volume (regular): {fmt(q['volume'], ',.0f')} | {q['session']}\n"
            f"Bid: {fmt(q['bid'], '.4f')} | Ask: {fmt(q['ask'], '.4f')}\n"
            f"Quote NY: {stamp}")


def run(mode, now=None):
    now = now or datetime.now(NY)
    if mode == 'test':
        telegram('✅ اختبار الاتصال نجح. مراقب الأسهم جاهز. هذه رسالة تجريبية بلا بيانات أسعار.\n'
                 + now.strftime('NY: %Y-%m-%d %H:%M %Z'))
        return
    status = phase(now)
    if status == 'expired':
        disable()
        return
    if status == 'idle':
        print('Outside monitoring hours; no data requests.')
        return
    state = State()
    day = now.date().isoformat()
    if status == 'summary' and 'summary' in state.data['days'].get(day, {}):
        if now.date() == END:
            disable()
        return
    quotes = [fetch(s, now, status == 'summary') for s in SYMBOLS]
    for q in quotes:
        print(row(q))
    all_failed = all(q['price'] is None or q['change'] is None for q in quotes)
    if all_failed:
        state.once(day, 'data-failed', '⚠️ فشل جلب بيانات صالحة للأسهم الخمسة: n/a\n'
                   + now.strftime('NY: %Y-%m-%d %H:%M %Z'))
    if status == 'summary':
        text = 'ملخص المراقبة — ' + day + '\n\n' + '\n\n'.join(row(q) for q in quotes)
        text += '\n\nVolume = حجم الجلسة العادية المتاح فقط؛ حجم الساعات الممتدة n/a.'
        text += '\nالسعر = آخر إغلاق شمعة 5 دقائق متاح؛ ليس سعرًا لحظيًا مضمونًا.'
        state.once(day, 'summary', text)
        if now.date() == END:
            disable()
        return
    for q in quotes:
        direction, level = thresholds(q['change'])
        if not level:
            continue
        sent = state.data['days'].setdefault(day, {})
        key = f"{q['symbol']}:{direction}:{level}"
        # Mark skipped lower thresholds too: +12% means one alert, not +5/+10 spam.
        if key in sent:
            continue
        for lower in range(1, level):
            sent.setdefault(f"{q['symbol']}:{direction}:{lower}", {'covered': True})
        text = 'تنبيه حركة للمراقبة\n' + row(q)
        text += '\n' + now.strftime('Checked NY: %Y-%m-%d %H:%M %Z')
        text += '\nالسعر: آخر شمعة 5 دقائق متاحة. Volume: الجلسة العادية فقط.'
        state.once(day, key, text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['monitor', 'test'], default='monitor')
    args = parser.parse_args()
    try:
        run(args.mode)
    except Exception as exc:
        # Never print exception text from third-party libraries that may contain secrets.
        print(f'Monitor failed ({type(exc).__name__}). Check secrets, permissions and connectivity.',
              file=sys.stderr)
        sys.exit(1)
