"""Network failure categories and provider codes never expose response secrets."""
import json
import unittest
from unittest.mock import patch
import httpx
from account_auth import renewal_error
from oauth_network import OAuthError, request


class RenewalDiagnosticsTests(unittest.TestCase):
    def network_failure(self, result):
        def handle(req):
            if isinstance(result, Exception):
                raise result
            return result
        client = httpx.Client(transport=httpx.MockTransport(handle))
        with patch('oauth_network.httpx.Client',return_value=client):
            with self.assertRaises(OAuthError) as raised:
                request('xai','https://auth.x.ai/oauth2/token','http://proxy.example',
                        data={'refresh_token':'PRIVATE_TOKEN'})
        return raised.exception

    def test_connection_timeout_response_timeout_and_interruption_are_distinct(self):
        for error, code in [
            (httpx.ConnectTimeout('PRIVATE_PROXY'),'renewal_connection_timeout'),
            (httpx.ConnectError('PRIVATE_PROXY'),'renewal_connection_failed'),
            (httpx.ReadTimeout('PRIVATE_TOKEN'),'renewal_response_timeout'),
            (httpx.ReadError('PRIVATE_TOKEN'),'renewal_response_interrupted'),
            (httpx.WriteTimeout('PRIVATE_TOKEN'),'renewal_request_interrupted')]:
            with self.subTest(code=code):
                value=self.network_failure(error)
                kind,message=renewal_error(value)
                self.assertEqual(kind,code)
                self.assertNotIn('PRIVATE',message)
                self.assertNotIn('重新授权',message)

    def test_status_and_known_oauth_code_are_retained_but_raw_body_is_not(self):
        for status, error in [(400,'invalid_grant'),(401,{'code':'invalid_client'}),(429,'rate_limit_exceeded'),(503,'server_error')]:
            value=self.network_failure(httpx.Response(status,json={'error':error,'error_description':'PRIVATE_TOKEN PRIVATE_PROXY'}))
            kind,message=renewal_error(value)
            self.assertEqual(kind,'renewal_http_error')
            self.assertIn(f'HTTP {status}',message)
            self.assertIn(error if isinstance(error,str) else error['code'],message)
            self.assertNotIn('PRIVATE',message)
        value=self.network_failure(httpx.Response(400,json={'error':'PRIVATE_TOKEN'}))
        self.assertEqual(value.provider_code,'')
        self.assertNotIn('PRIVATE',renewal_error(value)[1])

    def test_bad_response_and_unknown_exception_details_are_redacted(self):
        for response in [httpx.Response(200,text='PRIVATE_TOKEN'),httpx.Response(200,json={'error':'PRIVATE_TOKEN'})]:
            self.assertEqual(renewal_error(self.network_failure(response))[0],'renewal_invalid_response')
        self.assertNotIn('PRIVATE',json.dumps(renewal_error(RuntimeError('PRIVATE_TOKEN'))))


if __name__ == '__main__': unittest.main()
