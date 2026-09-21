import unittest

from biocpkgconda import dcf


class ParseTest(unittest.TestCase):
    def test_records_and_continuation_lines(self):
        text = (
            "Package: a4Base\n"
            "Version: 1.58.0\n"
            "Imports: methods, graphics,\n"
            "        limma, glmnet\n"
            "\n"
            "\n"
            "Package: a4Core\r\n"
            "Version: 1.58.0\r\n"
        )
        records = dcf.parse(text)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["Imports"], "methods, graphics,\nlimma, glmnet")
        self.assertEqual(records[1], {"Package": "a4Core", "Version": "1.58.0"})

    def test_value_on_continuation_line_only(self):
        self.assertEqual(dcf.parse("Depends:\n  R (>= 4.0)\n")[0]["Depends"], "R (>= 4.0)")

    def test_colon_in_value(self):
        record = dcf.parse_one("URL: https://bioconductor.org\n")
        self.assertEqual(record["URL"], "https://bioconductor.org")

    def test_malformed_input(self):
        with self.assertRaises(ValueError):
            dcf.parse("  orphan continuation\n")
        with self.assertRaises(ValueError):
            dcf.parse("no field separator\n")
        with self.assertRaises(ValueError):
            dcf.parse_one("A: 1\n\nB: 2\n")
