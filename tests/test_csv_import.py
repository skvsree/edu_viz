from __future__ import annotations

import io
import unittest

from app.services.csv_import import CsvImportError, parse_cards_csv


class CsvImportTests(unittest.TestCase):
    def test_parses_quoted_commas_and_escaped_quotes(self) -> None:
        payload = (
            'question,answer\n'
            '"What is 2, 3, and 4?","It is a list with a ""quoted"" note"\n'
        ).encode("utf-8")

        rows = parse_cards_csv(io.BytesIO(payload))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].front, "What is 2, 3, and 4?")
        self.assertEqual(rows[0].back, 'It is a list with a "quoted" note')

    def test_parses_multiline_fields(self) -> None:
        payload = 'front,back\n"Line 1\nLine 2","Answer"\n'.encode("utf-8")

        rows = parse_cards_csv(io.BytesIO(payload))

        self.assertEqual(rows[0].front, "Line 1\nLine 2")
        self.assertEqual(rows[0].back, "Answer")

    def test_rejects_misaligned_rows(self) -> None:
        payload = 'front,back\n"Question","Answer",oops\n'.encode("utf-8")

        with self.assertRaises(CsvImportError):
            parse_cards_csv(io.BytesIO(payload))


if __name__ == "__main__":
    unittest.main()
