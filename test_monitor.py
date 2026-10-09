import unittest
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
