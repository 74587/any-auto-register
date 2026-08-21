import unittest

from platforms.chatgpt.protocol.response_summary import compact, describe_error, describe_page


class CompactTests(unittest.TestCase):
    def test_folds_multiline_json_into_one_line(self):
        text = '{\n  "error": {\n    "message": "boom"\n  }\n}'
        self.assertEqual(compact(text), '{ "error": { "message": "boom" } }')

    def test_truncates_long_text(self):
        self.assertEqual(compact("x" * 500, limit=10), "x" * 10 + "…")

    def test_empty_input_stays_empty(self):
        self.assertEqual(compact(None), "")


class DescribeErrorTests(unittest.TestCase):
    def test_pulls_message_and_code_out_of_the_error_envelope(self):
        body = (
            '{\n  "error": {\n    "message": "Phone number already in use. Please try again.",'
            '\n    "type": "invalid_request_error",\n    "code": "phone_number_in_use"\n  }\n}'
        )
        self.assertEqual(
            describe_error(body),
            "Phone number already in use. Please try again. phone_number_in_use",
        )

    def test_falls_back_to_a_trimmed_snippet_for_html(self):
        summary = describe_error("<html>\n  <body>502 Bad Gateway</body>\n</html>")
        self.assertEqual(summary, "<html> <body>502 Bad Gateway</body> </html>")

    def test_reads_flat_message_fields(self):
        self.assertEqual(describe_error('{"message":"nope","code":"bad_state"}'), "nope bad_state")

    def test_json_without_anything_useful_degrades_to_the_raw_text(self):
        self.assertEqual(describe_error('{"foo":"bar"}'), '{"foo":"bar"}')


class DescribePageTests(unittest.TestCase):
    def test_keeps_only_the_next_step_fields(self):
        payload = {
            "continue_url": "https://auth.openai.com/contact-verification",
            "method": "GET",
            "page": {"type": "contact_verification", "backstack_behavior": "default"},
            "oai-client-auth-session": {"auth_session_logging_id": "6a2336f6"},
        }
        self.assertEqual(
            describe_page(payload),
            "page=contact_verification continue=https://auth.openai.com/contact-verification",
        )

    def test_missing_fields_produce_nothing_rather_than_noise(self):
        self.assertEqual(describe_page({}), "")
        self.assertEqual(describe_page("not a dict"), "")


if __name__ == "__main__":
    unittest.main()
