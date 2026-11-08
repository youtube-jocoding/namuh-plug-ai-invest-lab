import os
import unittest
from unittest.mock import patch
from namuh_lab.providers import OpenRouter, InfrastructureError


class ProviderTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'offline-test-placeholder'}):
            self.provider = OpenRouter()
        self.p = {'name': 'Example', 'model': 'example/model', 'provider': 'example', 'provider_name': 'Example'}
        self.cfg = {'max_output_tokens': 100, 'timeout_seconds': 1, 'credit_reserve_usd': '1', 'max_api_budget_usd': '5'}

    def test_credit_shortage_fails_preflight(self):
        def request(path):
            if path == '/models':
                return {'data': [{'id': 'example/model', 'context_length': 100000,
                                  'pricing': {'prompt': '0.000001', 'completion': '0.000001'}}]}
            if path == '/credits':
                return {'data': {'total_credits': 100, 'total_usage': 0}}
            if path == '/key':
                return {'data': {'limit_remaining': .01}}
            self.fail('No inference allowed in preflight')
        with patch.object(self.provider, 'request', side_effect=request):
            with self.assertRaisesRegex(InfrastructureError, 'Insufficient credits'):
                self.provider.preflight([self.p], {'Example': {}}, self.cfg, 0)

    def test_no_fallback_and_pinned_identity(self):
        captured = {}
        def request(path, body, timeout):
            captured.update(body)
            return {'model': 'another/model', 'provider': 'Example', 'usage': {'cost': .1},
                    'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}
        with patch.object(self.provider, 'request', side_effect=request):
            result = self.provider.decide(self.p, {}, self.cfg)
        self.assertFalse(captured['provider']['allow_fallbacks'])
        self.assertEqual(captured['provider']['only'], ['example'])
        self.assertEqual(result.status, 'infrastructure')

    def test_missing_billing_is_not_silently_free(self):
        response = {'model': 'example/model', 'provider': 'Example',
                    'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}
        with patch.object(self.provider, 'request', return_value=response):
            result = self.provider.decide(self.p, {}, self.cfg)
        self.assertEqual(result.status, 'infrastructure')
        self.assertIsNone(result.cost)

    def test_error_message_does_not_copy_provider_body(self):
        with patch.object(self.provider, 'request', return_value={'error': {'message': 'private-provider-body'}}):
            result = self.provider.decide(self.p, {}, self.cfg)
        self.assertEqual(result.status, 'infrastructure')
        self.assertNotIn('private-provider-body', result.error)


if __name__ == '__main__':
    unittest.main()
