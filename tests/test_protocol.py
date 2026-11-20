"""报文分类的离线单测：所有判定都在这里固化，不再散落在各 SDK 里。"""
import json
import unittest

import httpx

from tsn.protocol import ResultKind, classify_response, redact, should_retry


def _resp(status=200, body=None, text=None, headers=None):
    if text is None:
        text = json.dumps(body, ensure_ascii=False) if body is not None else ''
    return httpx.Response(
        status,
        content=text.encode('utf-8'),
        headers=headers or {'content-type': 'application/json'},
        request=httpx.Request('GET', 'http://example.com/x'),
    )


class ClassifyOk(unittest.TestCase):
    def test_code_zero(self):
        r = classify_response(_resp(body={'code': 0, 'data': {'a': 1}}))
        self.assertIs(r.kind, ResultKind.OK)
        self.assertEqual(r.data, {'a': 1})

    def test_return_code_string(self):
        r = classify_response(_resp(body={'returnCode': '200', 'data': [1, 2]}))
        self.assertIs(r.kind, ResultKind.OK)
        self.assertEqual(r.data, [1, 2])

    def test_token_payload_without_code(self):
        r = classify_response(_resp(body={'access_token': 't', 'expires_in': 86399}), expect_token=True)
        self.assertIs(r.kind, ResultKind.OK)
        self.assertEqual(r.data['access_token'], 't')

    def test_bare_list(self):
        r = classify_response(_resp(body=[{'x': 1}]))
        self.assertIs(r.kind, ResultKind.OK)
        self.assertEqual(r.data, [{'x': 1}])

    def test_no_data_key_returns_payload(self):
        r = classify_response(_resp(body={'code': 200, 'other': 1}))
        self.assertIs(r.kind, ResultKind.OK)
        self.assertEqual(r.data, {'code': 200, 'other': 1})


class ClassifyFailure(unittest.TestCase):
    def test_http_999_is_anti_cheat_and_not_retryable(self):
        r = classify_response(_resp(999, body={'code': 999, 'msg': '已被拉入禁跑黑名单'}))
        self.assertIs(r.kind, ResultKind.ANTI_CHEAT)
        self.assertFalse(should_retry(r))

    def test_anti_cheat_detected_by_message(self):
        r = classify_response(_resp(body={'code': 1, 'msg': '系统检测到您有作弊嫌疑'}))
        self.assertIs(r.kind, ResultKind.ANTI_CHEAT)

    def test_401_is_auth_expired(self):
        r = classify_response(_resp(401, body={'msg': '登录已过期'}))
        self.assertIs(r.kind, ResultKind.AUTH_EXPIRED)
        self.assertTrue(r.is_auth_expired)
        self.assertFalse(should_retry(r))

    def test_428_is_rate_limited(self):
        self.assertIs(classify_response(_resp(428, body={})).kind, ResultKind.RATE_LIMITED)

    def test_5xx_is_server_degraded_and_retryable(self):
        r = classify_response(_resp(502, text='<html>502 Bad Gateway</html>'))
        self.assertIs(r.kind, ResultKind.SERVER_DEGRADED)
        self.assertTrue(should_retry(r))

    def test_server_error_body_is_degraded_not_password_error(self):
        r = classify_response(_resp(body={'code': 1, 'msg': 'Internal Server Error', 'data': 'server_error'}))
        self.assertIs(r.kind, ResultKind.SERVER_DEGRADED)

    def test_html_200_is_gateway_blocked(self):
        r = classify_response(_resp(200, text='<!DOCTYPE html><html><title>WAF</title></html>'))
        self.assertIs(r.kind, ResultKind.GATEWAY_BLOCKED)
        self.assertIn('WAF', r.snippet)

    def test_empty_body_is_gateway_blocked(self):
        self.assertIs(classify_response(_resp(200, text='')).kind, ResultKind.GATEWAY_BLOCKED)

    def test_biz_error_keeps_server_message(self):
        r = classify_response(_resp(body={'code': 1, 'msg': '密码错误'}))
        self.assertIs(r.kind, ResultKind.BIZ_ERROR)
        self.assertEqual(r.message, '密码错误')

    def test_malformed_dict(self):
        self.assertIs(classify_response(_resp(body={'foo': 1})).kind, ResultKind.MALFORMED)

    def test_none_response_is_transport(self):
        self.assertIs(classify_response(None).kind, ResultKind.TRANSPORT)

    def test_error_code_mapping(self):
        err = classify_response(_resp(999, body={'msg': '禁跑黑名单'})).to_error()
        self.assertEqual(err.code, 999)
        self.assertEqual(err.kind, 'anti_cheat')

    def test_raise_for_error_returns_data_on_success(self):
        self.assertEqual(classify_response(_resp(body={'code': 0, 'data': 7})).raise_for_error(), 7)


class Redaction(unittest.TestCase):
    def test_tokens_are_masked(self):
        text = '{"access_token":"abc","refresh_token":"def"}'
        self.assertNotIn('abc', redact(text))
        self.assertNotIn('def', redact(text))


if __name__ == '__main__':
    unittest.main()
