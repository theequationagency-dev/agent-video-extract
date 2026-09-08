"""Failure classification, Retry-After parsing and backoff math."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import video_metadata_extractor as vme  # noqa: E402


class FakeHTTPError(Exception):
    """Stands in for urllib/yt-dlp HTTP errors: a status plus headers."""

    def __init__(self, status, message="", headers=None):
        super().__init__(message or "HTTP Error %s" % status)
        self.status = status
        self.headers = headers or {}


class ClassificationTests(unittest.TestCase):
    def assert_category(self, message, expected):
        category, _ = vme.classify_error(message)
        self.assertEqual(category, expected, message)

    def test_rate_limit(self):
        for message in (
            "ERROR: [youtube] abc: HTTP Error 429: Too Many Requests",
            "HTTP Error 429",
            "We have detected unusual traffic, please slow down",
            "You are being rate-limited by the server",
            "The uploader is throttling requests",
        ):
            self.assert_category(message, vme.Category.RATE_LIMIT)

    def test_blocked(self):
        for message in (
            "ERROR: [youtube] aqz-KE-bpKQ: Sign in to confirm you're not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication.",
            "Please solve the captcha to continue",
            "This video is available to Music Premium members only",
            "HTTP Error 403: Forbidden",
            "Sign in to confirm your age",
            "This video is members-only content",
        ):
            self.assert_category(message, vme.Category.BLOCKED)

    def test_transient(self):
        for message in (
            "HTTP Error 503: Service Unavailable",
            "HTTP Error 500: Internal Server Error",
            "Unable to download webpage: The read operation timed out",
            "Connection reset by peer",
            "Remote end closed connection without response",
            "[Errno -3] Temporary failure in name resolution",
        ):
            self.assert_category(message, vme.Category.TRANSIENT)

    def test_fatal(self):
        for message in (
            "ERROR: [youtube] xyz: Private video. Sign in if you've been granted "
            "access to this video",
            "Video unavailable. This video has been removed by the uploader",
            "HTTP Error 404: Not Found",
            "ERROR: Unsupported URL: https://example.invalid/nope",
            "The uploader has not made this video available in your country",
            "certificate verify failed: unable to get local issuer certificate",
        ):
            self.assert_category(message, vme.Category.FATAL)

    def test_private_video_is_fatal_not_a_login_wall(self):
        # It mentions "Sign in", but no cookie jar brings a private video back.
        category, _ = vme.classify_error("Private video. Sign in if you've been granted access")
        self.assertEqual(category, vme.Category.FATAL)

    def test_unknown_failures_get_a_cautious_retry(self):
        category, status = vme.classify_error("something nobody has seen before")
        self.assertEqual(category, vme.Category.TRANSIENT)
        self.assertIsNone(status)

    def test_status_from_exception_attribute(self):
        category, status = vme.classify_error(FakeHTTPError(429, "nope"))
        self.assertEqual((category, status), (vme.Category.RATE_LIMIT, 429))

    def test_status_parsed_from_message(self):
        self.assertEqual(vme.http_status_from("HTTP Error 503: Service Unavailable"), 503)
        self.assertIsNone(vme.http_status_from("no status here"))

    def test_status_only_classification(self):
        self.assertEqual(vme.classify_error(FakeHTTPError(410))[0], vme.Category.FATAL)
        self.assertEqual(vme.classify_error(FakeHTTPError(401))[0], vme.Category.BLOCKED)
        self.assertEqual(vme.classify_error(FakeHTTPError(502))[0], vme.Category.TRANSIENT)

    def test_wrapped_exception_is_inspected(self):
        try:
            try:
                raise FakeHTTPError(429, "Too Many Requests")
            except FakeHTTPError as inner:
                raise RuntimeError("Unable to download webpage") from inner
        except RuntimeError as outer:
            category, status = vme.classify_error(outer)
        self.assertEqual((category, status), (vme.Category.RATE_LIMIT, 429))

    def test_error_message_is_single_line_and_unprefixed(self):
        message = vme.error_message("ERROR: [youtube] id:\n  broke\tbadly")
        self.assertEqual(message, "[youtube] id: broke badly")


class RetryAfterTests(unittest.TestCase):
    def test_from_headers(self):
        error = FakeHTTPError(429, headers={"Retry-After": "120"})
        self.assertEqual(vme.extract_retry_after(error), 120.0)

    def test_lowercase_header(self):
        error = FakeHTTPError(429, headers={"retry-after": "7"})
        self.assertEqual(vme.extract_retry_after(error), 7.0)

    def test_from_message(self):
        self.assertEqual(vme.extract_retry_after("slow down, retry after: 45"), 45.0)

    def test_absent(self):
        self.assertIsNone(vme.extract_retry_after("no hint at all"))

    def test_http_date_form(self):
        value = vme._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT")
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 0.0)


class BackoffTests(unittest.TestCase):
    def test_doubles_each_attempt(self):
        full = lambda: 1.0  # noqa: E731 - jitter stub returning its maximum
        delays = [vme.compute_backoff(n, base=2.0, cap=1000.0, jitter=full)
                  for n in range(1, 6)]
        self.assertEqual(delays, [2.0, 4.0, 8.0, 16.0, 32.0])

    def test_full_jitter_scales_the_ceiling(self):
        self.assertEqual(vme.compute_backoff(3, base=2.0, cap=1000.0, jitter=lambda: 0.5), 4.0)
        self.assertEqual(vme.compute_backoff(3, base=2.0, cap=1000.0, jitter=lambda: 0.0), 0.0)

    def test_capped(self):
        self.assertEqual(vme.compute_backoff(20, base=2.0, cap=300.0, jitter=lambda: 1.0), 300.0)

    def test_retry_after_wins_and_is_capped(self):
        self.assertEqual(vme.compute_backoff(1, cap=300.0, retry_after=45, jitter=lambda: 0.1), 45.0)
        self.assertEqual(vme.compute_backoff(1, cap=300.0, retry_after=9999, jitter=lambda: 1.0), 300.0)
        self.assertEqual(vme.compute_backoff(1, cap=300.0, retry_after=-5, jitter=lambda: 1.0), 0.0)

    def test_real_jitter_stays_within_the_ceiling(self):
        random.seed(1234)
        for attempt in range(1, 8):
            ceiling = min(300.0, 2.0 * (2 ** (attempt - 1)))
            for _ in range(50):
                delay = vme.compute_backoff(attempt, base=2.0, cap=300.0)
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, ceiling)

    def test_attempt_zero_is_treated_as_first(self):
        self.assertEqual(vme.compute_backoff(0, base=3.0, cap=100.0, jitter=lambda: 1.0), 3.0)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = vme.RetryPolicy(retries=4, blocked_retries=1)

    def test_fatal_never_retries(self):
        self.assertEqual(self.policy.max_attempts(vme.Category.FATAL), 1)

    def test_blocked_budget_is_much_smaller_than_throttling(self):
        blocked = self.policy.max_attempts(vme.Category.BLOCKED)
        throttled = self.policy.max_attempts(vme.Category.RATE_LIMIT)
        self.assertEqual(blocked, 2)
        self.assertEqual(throttled, 5)
        self.assertLess(blocked, throttled)

    def test_transient_matches_rate_limit_budget(self):
        self.assertEqual(self.policy.max_attempts(vme.Category.TRANSIENT),
                         self.policy.max_attempts(vme.Category.RATE_LIMIT))


class ExtractorArgsTests(unittest.TestCase):
    def test_single_arg_with_multiple_values(self):
        self.assertEqual(vme.parse_extractor_args(["youtube:player_client=web_safari,mweb"]),
                         {"youtube": {"player_client": ["web_safari", "mweb"]}})

    def test_several_args_and_several_extractors(self):
        self.assertEqual(
            vme.parse_extractor_args(["youtube:player_client=tv;formats=incomplete",
                                      "generic:impersonate=chrome"]),
            {"youtube": {"player_client": ["tv"], "formats": ["incomplete"]},
             "generic": {"impersonate": ["chrome"]}})

    def test_key_is_lowercased_and_values_trimmed(self):
        self.assertEqual(vme.parse_extractor_args(["YouTube: player_client = tv , mweb "]),
                         {"youtube": {"player_client": ["tv", "mweb"]}})

    def test_empty_input(self):
        self.assertEqual(vme.parse_extractor_args(None), {})
        self.assertEqual(vme.parse_extractor_args([]), {})

    def test_malformed_values_are_rejected(self):
        for bad in ["youtube", "youtube:", ":player_client=tv", ""]:
            with self.assertRaises(ValueError, msg=bad):
                vme.parse_extractor_args([bad])


class CookieArgTests(unittest.TestCase):
    def test_browser_only(self):
        self.assertEqual(vme.parse_cookies_from_browser("firefox"),
                         ("firefox", None, None, None))

    def test_full_form(self):
        self.assertEqual(vme.parse_cookies_from_browser("chrome+GNOMEKEYRING:Default::work"),
                         ("chrome", "Default", "GNOMEKEYRING", "work"))

    def test_none(self):
        self.assertIsNone(vme.parse_cookies_from_browser(None))


if __name__ == "__main__":
    unittest.main()
