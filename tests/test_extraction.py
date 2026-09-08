"""End-to-end extraction and CLI behaviour against a stubbed yt-dlp."""

import io
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import video_metadata_extractor as vme  # noqa: E402
from test_captions import ROLLING_VTT  # noqa: E402
from test_incidents import RecordingLogger  # noqa: E402

VIDEO_URL = "https://www.example.com/watch?v=demo123"

INFO = {
    "id": "demo123",
    "title": "How Rate Limiting Works",
    "uploader": "Guilty Trex",
    "channel": "Guilty Trex",
    "channel_url": "https://www.example.com/@guiltytrex",
    "upload_date": "20240307",
    "duration": 612.4,
    "tags": ["rate limiting", "http", "agents"],
    "categories": ["Science & Technology"],
    "license": "Creative Commons Attribution license (reuse allowed)",
    "description": "A short talk about 429s.",
    "view_count": 12345,
    "like_count": 678,
    "thumbnail": "https://www.example.com/thumb.jpg",
    "language": "en",
    "extractor_key": "Example",
    "webpage_url": VIDEO_URL,
    "chapters": [{"start_time": 0.0, "end_time": 60.0, "title": "Intro"}],
    "subtitles": {"en-US": [{"ext": "vtt", "url": "https://subs.example.com/en.vtt"}]},
    "automatic_captions": {"en": [{"ext": "vtt", "url": "https://subs.example.com/auto.vtt"}]},
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload


class FakeYDL:
    """A stand-in for yt_dlp.YoutubeDL: no network, scripted responses."""

    def __init__(self, options, infos=None, subtitles=None, errors=None):
        self.options = options
        self.infos = infos or {}
        self.subtitles = subtitles or {}
        self.errors = errors or {}
        self.requested = []
        self.closed = False

    def extract_info(self, url, download=False):
        self.requested.append(url)
        if url in self.errors:
            raise self.errors[url]
        if url not in self.infos:
            raise RuntimeError("ERROR: [example] %s: Video unavailable" % url)
        return self.infos[url]

    def urlopen(self, url):
        if url not in self.subtitles:
            raise RuntimeError("HTTP Error 404: Not Found")
        return FakeResponse(self.subtitles[url])

    def close(self):
        self.closed = True


def factory(infos=None, subtitles=None, errors=None, box=None):
    def build(options):
        ydl = FakeYDL(options, infos, subtitles, errors)
        if box is not None:
            box.append(ydl)
        return ydl
    return build


DEFAULT_FACTORY_ARGS = dict(
    infos={VIDEO_URL: INFO},
    subtitles={"https://subs.example.com/en.vtt": ROLLING_VTT.encode("utf-8")},
)


class ExtractionTests(unittest.TestCase):
    def extract(self, **kwargs):
        config = vme.Config(incident_log=None, write_incidents=False, **kwargs)
        with vme.Extractor(config, RecordingLogger(),
                           factory(**DEFAULT_FACTORY_ARGS)) as extractor:
            return extractor.extract(VIDEO_URL)

    def test_core_schema_fields(self):
        record = self.extract()
        self.assertEqual(record["title"], "How Rate Limiting Works")
        self.assertEqual(record["source_url"], VIDEO_URL)
        self.assertEqual(record["uploader"], "Guilty Trex")
        self.assertEqual(record["upload_date"], "2024-03-07")
        self.assertEqual(record["duration_seconds"], 612)
        self.assertEqual(record["tags"], ["rate limiting", "http", "agents"])
        self.assertEqual(record["license"],
                         "Creative Commons Attribution license (reuse allowed)")
        self.assertEqual(record["tool_version"], vme.TOOL_VERSION)
        self.assertRegex(record["extracted_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIsNone(record["error"])

    def test_additive_agent_context(self):
        record = self.extract()
        self.assertEqual(record["video_id"], "demo123")
        self.assertEqual(record["webpage_url"], VIDEO_URL)
        self.assertEqual(record["channel"], "Guilty Trex")
        self.assertEqual(record["channel_url"], "https://www.example.com/@guiltytrex")
        self.assertEqual(record["categories"], ["Science & Technology"])
        self.assertEqual(record["view_count"], 12345)
        self.assertEqual(record["like_count"], 678)
        self.assertEqual(record["language"], "en")
        self.assertEqual(record["extractor"], "Example")
        self.assertEqual(record["chapters"],
                         [{"start": 0.0, "end": 60.0, "title": "Intro"}])

    def test_every_documented_key_is_present(self):
        expected = {
            "title", "source_url", "uploader", "upload_date", "duration_seconds",
            "tags", "timestamps", "transcript", "license", "extracted_at",
            "tool_version", "video_id", "webpage_url", "description", "channel",
            "channel_url", "categories", "view_count", "like_count", "thumbnail",
            "language", "extractor", "chapters", "transcript_source",
            "transcript_language", "error",
        }
        self.assertEqual(set(self.extract()), expected)
        self.assertEqual(set(vme.build_error_record(VIDEO_URL, RuntimeError("x"))), expected)

    def test_transcript_is_merged_and_labelled(self):
        record = self.extract()
        self.assertEqual(record["transcript_source"], "manual")
        self.assertEqual(record["transcript_language"], "en-US")   # en matched en-US
        self.assertEqual(record["transcript"],
                         "the quick brown fox jumps over the lazy dog and keeps running")
        self.assertEqual([segment["text"] for segment in record["timestamps"]],
                         ["the quick brown fox", "jumps over the lazy dog",
                          "and keeps running"])

    def test_subtitles_can_be_skipped(self):
        record = self.extract(subtitles=False)
        self.assertEqual(record["transcript"], "")
        self.assertEqual(record["timestamps"], [])
        self.assertIsNone(record["transcript_source"])

    def test_missing_transcript_does_not_lose_the_metadata(self):
        config = vme.Config(incident_log=None, write_incidents=False)
        build = factory(infos={VIDEO_URL: INFO}, subtitles={})   # caption fetch 404s
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            with vme.Extractor(config, RecordingLogger(), build) as extractor:
                record = extractor.extract(VIDEO_URL)
        finally:
            sys.stderr = stderr
        self.assertEqual(record["title"], "How Rate Limiting Works")
        self.assertEqual(record["transcript"], "")
        self.assertIsNone(record["error"])

    def test_json_serialisable(self):
        json.dumps(self.extract())

    def test_playlist_expansion(self):
        flat = {"_type": "playlist", "entries": [
            {"url": "https://www.example.com/watch?v=a"},
            {"id": "b", "ie_key": "Youtube"},
            {"_type": "playlist", "entries": [{"url": "https://www.example.com/watch?v=c"}]},
        ]}
        playlist_url = "https://www.example.com/playlist?list=PL1"
        config = vme.Config(playlist=True, incident_log=None, write_incidents=False)
        with vme.Extractor(config, RecordingLogger(),
                           factory(infos={playlist_url: flat})) as extractor:
            urls = extractor.expand(playlist_url)
        self.assertEqual(urls, ["https://www.example.com/watch?v=a",
                                "https://www.youtube.com/watch?v=b",
                                "https://www.example.com/watch?v=c"])

    def test_max_entries_caps_a_playlist(self):
        flat = {"_type": "playlist", "entries": [
            {"url": "https://www.example.com/watch?v=%d" % n} for n in range(10)]}
        playlist_url = "https://www.example.com/playlist?list=PL2"
        config = vme.Config(playlist=True, max_entries=3, incident_log=None,
                            write_incidents=False)
        with vme.Extractor(config, RecordingLogger(),
                           factory(infos={playlist_url: flat})) as extractor:
            self.assertEqual(len(extractor.expand(playlist_url)), 3)


class BatchTests(unittest.TestCase):
    def test_failures_are_inline_records_not_aborts(self):
        good = VIDEO_URL
        bad = "https://www.example.com/watch?v=gone"
        build = factory(infos={good: INFO},
                        subtitles={"https://subs.example.com/en.vtt": ROLLING_VTT},
                        errors={bad: RuntimeError("ERROR: [example] gone: Private video")})
        config = vme.Config(incident_log=None, write_incidents=False)
        records, failures = vme.extract_videos([bad, good, bad], config,
                                               RecordingLogger(), ydl_factory=build)
        self.assertEqual(failures, 2)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["error"]["category"], vme.Category.FATAL)
        self.assertEqual(records[0]["source_url"], bad)
        self.assertIsNone(records[1]["error"])
        self.assertEqual(records[1]["title"], "How Rate Limiting Works")

    def test_error_record_carries_the_incident_id(self):
        bad = "https://www.example.com/watch?v=throttled"
        build = factory(errors={bad: RuntimeError("HTTP Error 429: Too Many Requests")})
        incidents = RecordingLogger()
        config = vme.Config(retries=1, base_backoff=0.0, max_backoff=0.0,
                            incident_log=None, write_incidents=False)
        with vme.Extractor(config, incidents, build, sleeper=lambda seconds: None) as extractor:
            with self.assertRaises(vme.ExtractionError) as caught:
                extractor.extract(bad)
        record = vme.build_error_record(bad, caught.exception)
        self.assertEqual(record["error"]["incident_id"],
                         incidents.lines()[0]["incident_id"])
        self.assertEqual(record["error"]["attempts"], 2)
        self.assertEqual(record["error"]["stage"], "metadata")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.output = os.path.join(self.directory, "out.json")
        self.incidents = os.path.join(self.directory, "incidents.jsonl")

    def run_cli(self, argv, **factory_args):
        args = dict(DEFAULT_FACTORY_ARGS)
        args.update(factory_args)
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            code = vme.main(argv, ydl_factory=factory(**args))
        finally:
            captured, sys.stderr = sys.stderr.getvalue(), stderr
        return code, captured

    def test_single_url_writes_one_object(self):
        code, _ = self.run_cli([VIDEO_URL, "-o", self.output, "--no-incident-log"])
        self.assertEqual(code, 0)
        with open(self.output, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["title"], "How Rate Limiting Works")

    def test_multiple_urls_write_an_array(self):
        code, _ = self.run_cli([VIDEO_URL, VIDEO_URL, "-o", self.output, "--no-incident-log"])
        self.assertEqual(code, 0)
        with open(self.output, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(len(payload), 2)

    def test_jsonl_output(self):
        code, _ = self.run_cli([VIDEO_URL, VIDEO_URL, "--jsonl", "-o", self.output,
                                "--no-incident-log"])
        self.assertEqual(code, 0)
        with open(self.output, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["video_id"], "demo123")

    def test_failure_exit_code_and_inline_error(self):
        bad = "https://www.example.com/watch?v=gone"
        code, _ = self.run_cli([VIDEO_URL, bad, "--jsonl", "-o", self.output,
                                "--no-incident-log"],
                               errors={bad: RuntimeError("HTTP Error 404: Not Found")})
        self.assertEqual(code, 1)
        with open(self.output, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)                       # the run did not abort
        self.assertIsNone(lines[0]["error"])
        self.assertEqual(lines[1]["error"]["category"], vme.Category.FATAL)
        self.assertEqual(lines[1]["error"]["http_status"], 404)

    def test_incident_log_is_written_for_retried_failures(self):
        bad = "https://www.example.com/watch?v=throttled"
        code, _ = self.run_cli([bad, "-o", self.output, "--incident-log", self.incidents,
                                "--retries", "1", "--base-backoff", "0",
                                "--max-backoff", "0"],
                               errors={bad: RuntimeError("HTTP Error 429: Too Many Requests")})
        self.assertEqual(code, 1)
        with open(self.incidents, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line["category"] == vme.Category.RATE_LIMIT for line in lines))
        self.assertTrue(all(not line["resolved"] for line in lines))
        self.assertEqual(len({line["incident_id"] for line in lines}), 1)

    def test_batch_file(self):
        batch = os.path.join(self.directory, "urls.txt")
        with open(batch, "w", encoding="utf-8") as handle:
            handle.write("# a comment\n\n%s\n%s\n" % (VIDEO_URL, VIDEO_URL))
        self.assertEqual(vme.read_batch_file(batch), [VIDEO_URL, VIDEO_URL])
        code, _ = self.run_cli(["--batch-file", batch, "--jsonl", "-o", self.output,
                                "--no-incident-log"])
        self.assertEqual(code, 0)
        with open(self.output, encoding="utf-8") as handle:
            self.assertEqual(len([line for line in handle if line.strip()]), 2)

    def test_quiet_suppresses_progress(self):
        _, captured = self.run_cli([VIDEO_URL, "-o", self.output, "--no-incident-log", "-q"])
        self.assertEqual(captured, "")

    def test_progress_goes_to_stderr(self):
        _, captured = self.run_cli([VIDEO_URL, "-o", self.output, "--no-incident-log"])
        self.assertIn("How Rate Limiting Works", captured)

    def test_no_urls_is_a_usage_error(self):
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            with self.assertRaises(SystemExit) as caught:
                vme.main(["--no-incident-log"])
        finally:
            sys.stderr = stderr
        self.assertEqual(caught.exception.code, 2)

    def test_version_flag(self):
        stdout, sys.stdout = sys.stdout, io.StringIO()
        try:
            with self.assertRaises(SystemExit) as caught:
                vme.main(["--version"])
            printed = sys.stdout.getvalue()
        finally:
            sys.stdout = stdout
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(vme.TOOL_VERSION, printed)

    def test_cookie_options_reach_yt_dlp(self):
        box = []
        args = dict(DEFAULT_FACTORY_ARGS)
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            vme.main([VIDEO_URL, "-o", self.output, "--no-incident-log",
                      "--cookies", "cookies.txt", "--sleep-interval", "0",
                      "--proxy", "socks5://127.0.0.1:9050"],
                     ydl_factory=factory(box=box, **args))
        finally:
            sys.stderr = stderr
        self.assertEqual(box[0].options["cookiefile"], "cookies.txt")
        self.assertEqual(box[0].options["proxy"], "socks5://127.0.0.1:9050")
        self.assertEqual(box[0].options["retries"], 0)

    def test_metadata_only_extraction_ignores_format_selection(self):
        # A video whose formats cannot be resolved is still a good metadata
        # result: this tool downloads nothing.
        box = []
        args = dict(DEFAULT_FACTORY_ARGS)
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            vme.main([VIDEO_URL, "-o", self.output, "--no-incident-log"],
                     ydl_factory=factory(box=box, **args))
        finally:
            sys.stderr = stderr
        self.assertTrue(box[0].options["ignore_no_formats_error"])
        self.assertTrue(box[0].options["skip_download"])

    def test_extractor_args_reach_yt_dlp(self):
        box = []
        args = dict(DEFAULT_FACTORY_ARGS)
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            vme.main([VIDEO_URL, "-o", self.output, "--no-incident-log",
                      "--extractor-args", "youtube:player_client=web_safari,mweb"],
                     ydl_factory=factory(box=box, **args))
        finally:
            sys.stderr = stderr
        self.assertEqual(box[0].options["extractor_args"],
                         {"youtube": {"player_client": ["web_safari", "mweb"]}})

    def test_no_extractor_args_means_no_key(self):
        box = []
        args = dict(DEFAULT_FACTORY_ARGS)
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            vme.main([VIDEO_URL, "-o", self.output, "--no-incident-log"],
                     ydl_factory=factory(box=box, **args))
        finally:
            sys.stderr = stderr
        self.assertNotIn("extractor_args", box[0].options)

    def test_cookies_from_browser_is_translated(self):
        box = []
        args = dict(DEFAULT_FACTORY_ARGS)
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            vme.main([VIDEO_URL, "-o", self.output, "--no-incident-log",
                      "--cookies-from-browser", "firefox:default"],
                     ydl_factory=factory(box=box, **args))
        finally:
            sys.stderr = stderr
        self.assertEqual(box[0].options["cookiesfrombrowser"],
                         ("firefox", "default", None, None))


class HelpTests(unittest.TestCase):
    def test_every_flag_has_help_text(self):
        parser = vme.build_parser()
        for action in parser._actions:
            self.assertTrue(action.help, "%s has no help" % action.dest)

    def test_readme_documents_every_flag(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        readme_path = os.path.join(root, "README.md")
        if not os.path.exists(readme_path):
            self.skipTest("README.md not present")
        with open(readme_path, encoding="utf-8") as handle:
            readme = handle.read()
        parser = vme.build_parser()
        for action in parser._actions:
            for flag in action.option_strings:
                if flag in ("-h", "--help"):
                    continue
                self.assertIn(flag, readme, "%s is undocumented in README.md" % flag)
        self.assertTrue(re.search(r"`URL`|positional", readme))


if __name__ == "__main__":
    unittest.main()
