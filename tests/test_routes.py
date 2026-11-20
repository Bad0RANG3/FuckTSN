"""路由与 URL 拼接的单测：这里固化「丢斜杠」这个历史致命 bug。"""
import unittest

import tsn_routes as R


class JoinUrl(unittest.TestCase):
    def test_host_without_trailing_slash_keeps_separator(self):
        self.assertEqual(
            R.join_url('http://a.example.com', 'upms/basUser/getLoginUserInfo'),
            'http://a.example.com/upms/basUser/getLoginUserInfo',
        )

    def test_host_with_trailing_slash_has_no_double_slash(self):
        self.assertEqual(
            R.join_url('http://a.example.com/', '/upms/x'),
            'http://a.example.com/upms/x',
        )

    def test_absolute_path_wins(self):
        self.assertEqual(R.join_url('http://a.example.com', 'https://b.example.com/y'), 'https://b.example.com/y')


class NormalizeBaseUrl(unittest.TestCase):
    def test_bare_host_gets_scheme_not_substitution(self):
        # 历史实现会把裸域名替换成默认网关，导致请求发到别的学校
        self.assertEqual(R.normalize_base_url('a.example.com'), 'https://a.example.com/')

    def test_empty_falls_back_to_default(self):
        self.assertEqual(R.normalize_base_url(''), R.DEFAULT_BASE_URL)
        self.assertEqual(R.normalize_base_url(None), R.DEFAULT_BASE_URL)

    def test_keeps_existing_scheme(self):
        self.assertEqual(R.normalize_base_url('http://x.cn'), 'http://x.cn/')


class Routes(unittest.TestCase):
    def test_known_route(self):
        self.assertEqual(R.route('getLoginUserInfo'), 'upms/basUser/getLoginUserInfo')

    def test_locally_registered_route_exists(self):
        self.assertEqual(R.route('listBasUserImageFace'), 'upms/basUserImage/listBasUserImageFace')

    def test_every_name_resolves(self):
        for name in R.ROUTE_NAMES:
            self.assertTrue(R.route(name), name)

    def test_unknown_route_raises(self):
        with self.assertRaises(R.UnknownRouteError):
            R.route('no_such_route')


if __name__ == '__main__':
    unittest.main()
