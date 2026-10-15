#!/usr/bin/env python3
"""Tests for sb_retry. No network and no Supabase credentials required.

Run: python test_sb_retry.py
"""

import unittest
from unittest import mock

import httpx
from postgrest._sync import request_builder as rb
from postgrest.exceptions import APIError

import sb_retry

# The APIError postgrest actually raised on 2026-10-15, trimmed.
GATEWAY_502 = {'message': 'JSON could not be generated', 'code': 502,
               'hint': 'Refer to full message for details',
               'details': '<!DOCTYPE html> ... 502: Bad gateway ...'}


class Transient(unittest.TestCase):
    def test_the_real_failure_is_transient(self):
        self.assertTrue(sb_retry.transient(APIError(GATEWAY_502)))

    def test_gateway_statuses(self):
        for code in (502, 503, 504, 520, 522, 524, 429, 408):
            self.assertTrue(sb_retry.transient(
                APIError({'message': 'x', 'code': code})), code)

    def test_httpx_transport_errors(self):
        for exc in (httpx.ConnectError('x'), httpx.ReadTimeout('x'),
                    httpx.RemoteProtocolError('x'), httpx.PoolTimeout('x')):
            self.assertTrue(sb_retry.transient(exc), type(exc).__name__)

    def test_client_errors_are_not_retried(self):
        for code in (400, 401, 403, 404, 409):
            self.assertFalse(sb_retry.transient(
                APIError({'message': 'x', 'code': code})), code)

    def test_statement_timeout_is_not_retried(self):
        # 57014 means the query is too big; repeating it just fails slower.
        self.assertFalse(sb_retry.transient(
            APIError({'message': 'canceling statement due to statement timeout',
                      'code': '57014'})))

    def test_postgrest_error_codes_are_not_retried(self):
        self.assertFalse(sb_retry.transient(
            APIError({'message': 'no rows', 'code': 'PGRST116'})))

    def test_unrelated_exception_is_not_retried(self):
        self.assertFalse(sb_retry.transient(ValueError('nope')))


class Patch(unittest.TestCase):
    def test_it_patched_something(self):
        self.assertTrue(sb_retry.PATCHED)
        self.assertIn('SyncQueryRequestBuilder', sb_retry.PATCHED)

    def test_patch_is_idempotent(self):
        before = rb.SyncQueryRequestBuilder.execute
        sb_retry._install()
        self.assertIs(before, rb.SyncQueryRequestBuilder.execute)

    def test_inheriting_builders_get_it_too(self):
        # SyncSelectRequestBuilder does not define execute; it must resolve to
        # the patched one, otherwise ordinary .select() calls go unprotected.
        self.assertTrue(hasattr(rb.SyncSelectRequestBuilder.execute, '__wrapped__'))


class Behaviour(unittest.TestCase):
    """Drive the wrapper directly, with sleep stubbed so the suite stays fast."""

    def setUp(self):
        self.slept = []
        p = mock.patch('sb_retry.time.sleep', self.slept.append)
        p.start()
        self.addCleanup(p.stop)

    def _wrapped(self, side_effect):
        inner = mock.Mock(side_effect=side_effect)
        return sb_retry._wrap(inner), inner

    def test_succeeds_after_two_gateway_failures(self):
        fn, inner = self._wrapped(
            [APIError(GATEWAY_502), APIError(GATEWAY_502), 'ok'])
        self.assertEqual(fn(None), 'ok')
        self.assertEqual(inner.call_count, 3)
        self.assertEqual(len(self.slept), 2)

    def test_gives_up_after_attempts_and_reraises(self):
        fn, inner = self._wrapped([APIError(GATEWAY_502)] * 10)
        with self.assertRaises(APIError):
            fn(None)
        self.assertEqual(inner.call_count, sb_retry.ATTEMPTS)
        self.assertEqual(len(self.slept), sb_retry.ATTEMPTS - 1)

    def test_deterministic_error_fails_immediately(self):
        fn, inner = self._wrapped([APIError({'message': 'bad', 'code': 400})])
        with self.assertRaises(APIError):
            fn(None)
        self.assertEqual(inner.call_count, 1)
        self.assertEqual(self.slept, [])

    def test_success_does_not_sleep(self):
        fn, inner = self._wrapped(['ok'])
        self.assertEqual(fn(None), 'ok')
        self.assertEqual(self.slept, [])

    def test_backoff_grows_and_is_capped(self):
        fn, _ = self._wrapped([APIError(GATEWAY_502)] * 10)
        with self.assertRaises(APIError):
            fn(None)
        # Jittered, so compare against the envelope rather than exact values.
        for i, d in enumerate(self.slept):
            ceiling = min(sb_retry.BASE_DELAY * 2 ** i, sb_retry.MAX_DELAY)
            self.assertGreater(d, 0)
            self.assertLessEqual(d, ceiling * 1.3 + 1e-9)
        self.assertGreater(self.slept[-1], self.slept[0])

    def test_arguments_and_self_are_passed_through(self):
        inner = mock.Mock(return_value='ok')
        fn = sb_retry._wrap(inner)
        sentinel = object()
        fn(sentinel, 1, key='v')
        inner.assert_called_once_with(sentinel, 1, key='v')


if __name__ == '__main__':
    unittest.main(verbosity=2)
