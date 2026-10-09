import unittest
import io
from urllib.error import HTTPError
from datetime import datetime
from unittest.mock import patch
import monitor as m


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=m.NY)


class MemoryState:
    data = {'days': {}}
    messages = []
    def __init__(self):
        self.data = type(self).data
    def save(self):
        pass
    def once(self, day, key, message):
        sent = self.data['days'].setdefault(day, {})
        if key in sent:
            return False
        sent[key] = True
        self.messages.append(message)
        return True


class Tests(unittest.TestCase):
    def setUp(self):
        # Unit tests must never consume the real API quota supplied by Actions.
        env = patch.dict('os.environ', {'TWELVE_DATA_API_KEY': '', 'ALPHA_VANTAGE_API_KEY': '', 'TELEGRAM_GROUP_CHAT_ID': ''})
        env.start()
        self.addCleanup(env.stop)
        MemoryState.data = {'days': {}}
        MemoryState.messages = []

    def test_session_boundaries(self):
        for t, s in [('03:59', 'closed'), ('04:00', 'pre-market'),
                     ('09:30', 'regular'), ('16:00', 'after-hours'), ('20:00', 'closed')]:
            self.assertEqual(m.session(at('2026-10-09T' + t)), s)

    def test_dst(self):
        self.assertEqual(at('2026-10-09T04:00').utcoffset().total_seconds(), -4*3600)
        self.assertEqual(at('2026-12-09T04:00').utcoffset().total_seconds(), -5*3600)

    def test_cutoff_weekend(self):
        self.assertEqual(m.phase(at('2026-10-10T10:00')), 'idle')
        self.assertEqual(m.phase(at('2026-10-16T20:07')), 'summary')
        self.assertEqual(m.phase(at('2026-10-17T00:07')), 'expired')

    def test_exact_levels(self):
        for value, want in [(4.999, ('up', 0)), (5, ('up', 1)),
                            (-5, ('down', 1)), (10, ('up', 2)), (None, (None, 0))]:
            self.assertEqual(m.thresholds(value), want)

    def test_stale_and_future(self):
        now = at('2026-10-09T10:00')
        self.assertTrue(m.fresh_stamp(at('2026-10-09T09:35'), now))
        self.assertFalse(m.fresh_stamp(at('2026-10-08T10:00'), now))
        self.assertFalse(m.fresh_stamp(at('2026-10-09T09:25'), now))
        self.assertFalse(m.fresh_stamp(at('2026-10-09T10:05'), now))
        self.assertTrue(m.fresh_stamp(at('2026-10-09T19:55'), at('2026-10-09T21:07'), True))

    def quote(self, symbol, now, summary=False):
        return dict(symbol=symbol, price=10, change=self.move, volume=None,
                    bid=None, ask=None, stamp=now, session=m.session(now))

    @patch('monitor.State', MemoryState)
    def test_dedup_direction_day_and_gaps(self):
        with patch('monitor.fetch', side_effect=self.quote), patch('builtins.print'):
            for self.move in [12, 12, 7, 15, -5, -5]:
                m.run('monitor', at('2026-10-09T10:00'))
            self.assertEqual(len(MemoryState.messages), 15)  # three per stock
            m.run('monitor', at('2026-10-12T10:00'))
            self.assertEqual(len(MemoryState.messages), 20)

    @patch('monitor.State', MemoryState)
    def test_failure_once_and_summary_once(self):
        self.move = None
        with patch('monitor.fetch', side_effect=self.quote), patch('builtins.print'):
            m.run('monitor', at('2026-10-09T10:00'))
            m.run('monitor', at('2026-10-09T10:15'))
            self.assertEqual(len(MemoryState.messages), 1)
            m.run('monitor', at('2026-10-09T20:07'))
            m.run('monitor', at('2026-10-09T20:22'))
            self.assertEqual(len(MemoryState.messages), 2)

    def test_test_mode_no_market_or_state(self):
        with patch('monitor.telegram') as tg, patch('monitor.State') as st, patch('monitor.fetch') as f:
            m.run('test', at('2026-10-09T02:00'))
            tg.assert_called_once()
            st.assert_not_called()
            f.assert_not_called()

    def test_expired_disables_no_quotes(self):
        with patch('monitor.disable') as disable, patch('monitor.fetch') as fetch:
            m.run('monitor', at('2026-10-17T08:00'))
            disable.assert_called_once()
            fetch.assert_not_called()

    def test_extended_quote_replaces_missing_chart(self):
        now = at('2026-10-09T19:45')
        q = dict(price=None, stamp=None)
        m.add_quote_snapshot(q, {'postMarketPrice': 10.5,
                             'postMarketTime': at('2026-10-09T19:40').timestamp()}, now)
        self.assertEqual(q['price'], 10.5)
        self.assertEqual(q['session'], 'after-hours')

    def test_old_or_untimestamped_snapshot_rejected(self):
        now = at('2026-10-09T19:45')
        for info in [{'postMarketPrice': 10.5},
                     {'postMarketPrice': 10.5, 'postMarketTime': at('2026-10-09T16:00').timestamp()},
                     {'postMarketPrice': 10.5, 'postMarketTime': at('2026-10-08T19:40').timestamp()}]:
            q = dict(price=None, stamp=None)
            m.add_quote_snapshot(q, info, now)
            self.assertIsNone(q['price'])

    def test_newer_chart_is_not_overwritten(self):
        now = at('2026-10-09T19:45')
        q = dict(price=11, stamp=at('2026-10-09T19:40'))
        m.add_quote_snapshot(q, {'postMarketPrice': 10.5,
                             'postMarketTime': at('2026-10-09T19:35').timestamp()}, now)
        self.assertEqual(q['price'], 11)

    def test_premarket_snapshot(self):
        now = at('2026-10-09T08:00')
        q = dict(price=None, stamp=None)
        m.add_quote_snapshot(q, {'preMarketPrice': 10.5,
                             'preMarketTime': at('2026-10-09T07:55').timestamp()}, now)
        self.assertEqual(q['session'], 'pre-market')

    def test_diagnose_has_no_external_side_effects(self):
        self.move = 10
        with patch('monitor.fetch', side_effect=self.quote), patch('monitor.State') as state, \
             patch('monitor.telegram') as tg, patch('monitor.disable') as disable, patch('builtins.print'):
            m.run('diagnose', at('2026-10-09T19:00'))
            state.assert_not_called()
            tg.assert_not_called()
            disable.assert_not_called()

    @patch.dict('os.environ', {'TWELVE_DATA_API_KEY': 'fake-test-key'})
    def test_twelve_success_uses_own_reference_and_skips_yahoo(self):
        now = at('2026-10-09T10:00')
        data = dict(symbol='MLP', currency='USD', close='10.5', previous_close='10',
                    timestamp=at('2026-10-09T09:59').timestamp(), volume='1234')
        with patch('monitor.twelve_request', side_effect=[data,
                 {'meta': {'symbol': 'MLP', 'currency': 'USD'},
                  'values': [{'datetime': '2026-10-09 09:55:00', 'close': '10.5'}]}]) as req, \
             patch('monitor.fetch_yahoo') as yahoo, patch('builtins.print'):
            q = m.fetch('MLP', now)
        self.assertEqual(q['change'], 5)
        self.assertEqual(q['volume'], 1234)
        self.assertIsNone(q['bid'])
        self.assertEqual(q['source'], 'Twelve Data 5-minute bar')
        self.assertEqual(req.call_count, 2)
        self.assertEqual(req.call_args_list[0].args[1]["interval"], "1day")
        yahoo.assert_not_called()

    @patch.dict('os.environ', {'TWELVE_DATA_API_KEY': 'fake-test-key'})
    def test_twelve_bad_quotes_fall_back(self):
        now = at('2026-10-09T10:00')
        good = dict(symbol='MLP', currency='USD', close='10.5', previous_close='10',
                    timestamp=at('2026-10-09T09:59').timestamp())
        bad = [{'status': 'error', 'code': 429},
               {**good, 'timestamp': at('2026-10-08T15:59').timestamp()},
               {**good, 'timestamp': at('2026-10-09T10:01').timestamp()},
               {**good, 'previous_close': None}, {**good, 'symbol': 'OTHER'},
               {**good, 'currency': 'EUR'}]
        for response in bad:
            with self.subTest(response=response), \
                 patch('monitor.twelve_request', return_value=response), \
                 patch('monitor.fetch_yahoo', return_value={'fallback': True}) as yahoo, \
                 patch('builtins.print'):
                self.assertEqual(m.fetch('MLP', now), {'fallback': True})
                yahoo.assert_called_once()

    @patch.dict('os.environ', {'TWELVE_DATA_API_KEY': 'fake-test-key'})
    def test_daily_reference_does_not_make_old_bar_fresh(self):
        reference = dict(symbol='MLP', currency='USD', previous_close='10',
                         timestamp=at('2026-10-09T09:30').timestamp())
        stale = {'meta': {'symbol': 'MLP', 'currency': 'USD'},
                 'values': [{'datetime': '2026-10-09 09:55:00', 'close': '11'}]}
        with patch('monitor.twelve_request', side_effect=[reference, stale]), \
             patch('builtins.print'):
            q = m.fetch_twelve('MLP', at('2026-10-09T14:00'))
            self.assertIsNone(q['price'])

    @patch.dict('os.environ', {'TWELVE_DATA_API_KEY': 'fake-test-key'})
    def test_free_twelve_no_requests_extended_or_summary(self):
        with patch('monitor.request_json') as req:
            for clock, summary in [('08:00', False), ('19:00', False), ('20:05', True)]:
                q = m.fetch_twelve('MLP', at('2026-10-09T' + clock), summary)
                self.assertIsNone(q['price'])
            req.assert_not_called()

    @patch.dict('os.environ', {'TWELVE_DATA_API_KEY': '',
                               'ALPHA_VANTAGE_API_KEY': 'fake-free-key'})
    def test_free_alpha_key_does_not_trigger_requests(self):
        with patch('monitor.request_json') as req, \
             patch('monitor.fetch_yahoo', return_value={'fallback': True}), patch('builtins.print'):
            m.fetch('MLP', at('2026-10-09T10:00'))
            req.assert_not_called()

    def test_twelve_minute_credit_limit(self):
        with patch('monitor.TWELVE_CALLS', []), \
             patch('monitor.clock.monotonic', side_effect=[0] * 9 + [61]), \
             patch('monitor.clock.sleep') as sleep, \
             patch('monitor.request_json', return_value={}):
            for _ in range(9):
                m.twelve_request('quote', {'symbol': 'MLP'})
            sleep.assert_called_once_with(61)

    @patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake-token',
                               'TELEGRAM_CHAT_ID': '123', 'TELEGRAM_GROUP_CHAT_ID': '-100456'})
    def test_private_and_group_receive_same_message(self):
        with patch('monitor.request_json', return_value={'ok': True}) as request:
            m.telegram('test')
            self.assertEqual([c.args[2]['chat_id'] for c in request.call_args_list], ['123', '-100456'])
            self.assertEqual([c.args[2]['text'] for c in request.call_args_list], ['test', 'test'])

    @patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake-token',
                               'TELEGRAM_CHAT_ID': '123', 'TELEGRAM_GROUP_CHAT_ID': ''})
    def test_private_only_without_group_secret(self):
        with patch('monitor.request_json', return_value={'ok': True}) as request:
            m.telegram('test')
            request.assert_called_once()

    def test_telegram_group_migration_retries_replacement_once(self):
        error = HTTPError('https://api.telegram.org/test', 400, 'Bad Request', {},
                          io.BytesIO(b'{"parameters":{"migrate_to_chat_id":-100456}}'))
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch('monitor.urlopen', side_effect=[error, response]) as opened, patch('builtins.print'):
            self.assertTrue(m.request_json('https://api.telegram.org/test', 'POST',
                            {'chat_id': '-123', 'text': 'test'})['ok'])
            retry = __import__('json').loads(opened.call_args_list[1].args[0].data)
            self.assertEqual(retry['chat_id'], '-100456')
            self.assertEqual(opened.call_count, 2)

    def test_reserve_before_send(self):
        state = object.__new__(m.State)
        state.data = {'days': {}}
        events = []
        state.save = lambda: events.append('save')
        with patch('monitor.telegram', side_effect=lambda _: events.append('send')):
            state.once('2026-10-09', 'test', 'hello')
            state.once('2026-10-09', 'test', 'hello')
        self.assertEqual(events, ['save', 'send'])


if __name__ == '__main__':
    unittest.main()
