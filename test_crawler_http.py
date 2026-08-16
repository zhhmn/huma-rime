import unittest
from unittest.mock import Mock, call, patch
import urllib.error
import urllib.request

import crawler_http


VALID_HOMEPAGE = b'''<script>
window.htxx = {"dlmc":"huma","qqdz":"https://c6.ysepan.com/api/"};
</script>'''


class OpenHomepageTest(unittest.TestCase):
    @patch("crawler_http.time.sleep")
    @patch("crawler_http.open_url", return_value=(200, {}, VALID_HOMEPAGE))
    @patch("crawler_http.urllib.request.build_opener")
    def test_returns_first_valid_session_without_waiting(
        self, build_opener, _open_url, sleep
    ):
        expected_opener = Mock()
        build_opener.return_value = expected_opener

        opener, _, site = crawler_http.open_homepage()

        self.assertIs(opener, expected_opener)
        self.assertEqual(site["qqdz"], "https://c6.ysepan.com/api/")
        build_opener.assert_called_once()
        sleep.assert_not_called()

    @patch("crawler_http.time.sleep")
    @patch("crawler_http.open_url")
    @patch("crawler_http.urllib.request.build_opener")
    def test_retries_with_a_new_session_when_config_is_missing(
        self, build_opener, open_url, sleep
    ):
        first_opener = Mock()
        second_opener = Mock()
        build_opener.side_effect = [first_opener, second_opener]
        open_url.side_effect = [
            (200, {}, b"<html></html>"),
            (200, {}, VALID_HOMEPAGE),
        ]

        opener, _, site = crawler_http.open_homepage()

        self.assertIs(opener, second_opener)
        self.assertEqual(site["dlmc"], "huma")
        self.assertEqual(build_opener.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("crawler_http.time.sleep")
    @patch("crawler_http.open_url", return_value=(200, {}, b"<html></html>"))
    @patch("crawler_http.urllib.request.build_opener")
    def test_fails_after_all_attempts(self, build_opener, _open_url, sleep):
        build_opener.side_effect = [Mock() for _ in range(3)]

        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            crawler_http.open_homepage()

        self.assertEqual(build_opener.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])


class TransportDiagnosticsTest(unittest.TestCase):
    @patch(
        "crawler_http.socket.getaddrinfo",
        return_value=[
            (2, 1, 6, "", ("61.147.124.28", 443)),
            (2, 1, 6, "", ("61.147.124.28", 443)),
        ],
    )
    @patch("crawler_http.time.monotonic", side_effect=[10.0, 40.0])
    @patch("crawler_http.log")
    def test_logs_dns_and_reason_for_url_error(self, log, _monotonic, _getaddrinfo):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(TimeoutError("timed out"))
        request = urllib.request.Request("https://ys-L.ysepan.com/file.txt")

        with self.assertRaises(urllib.error.URLError):
            crawler_http.open_url(opener, request, "changelog-download")

        diagnostic = log.call_args_list[-1].args[0]
        self.assertIn("after 30.000s", diagnostic)
        self.assertIn("addresses=['61.147.124.28']", diagnostic)
        self.assertIn("reason_type=TimeoutError", diagnostic)


if __name__ == "__main__":
    unittest.main()
