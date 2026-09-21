import json
import unittest
from pathlib import Path

from biocpkgconda import naming
from biocpkgconda.naming import Dependency

VECTORS = json.loads((Path(__file__).parent / "tag_vectors.json").read_text())


class NamesTest(unittest.TestCase):
    def test_package_names(self):
        self.assertEqual(naming.bioc_name("DESeq2"), "bioconductor-deseq2")
        self.assertEqual(naming.bioc_name("BSgenome.Hsapiens.UCSC.hg38"), "bioconductor-bsgenome.hsapiens.ucsc.hg38")
        self.assertEqual(naming.cran_name("Rcpp"), "r-rcpp")

    def test_conda_version(self):
        self.assertEqual(naming.conda_version("3.99-0.23"), "3.99_0.23")
        self.assertEqual(naming.conda_version("1.52.0"), "1.52.0")


class DependenciesTest(unittest.TestCase):
    def test_parse(self):
        field = "R (>= 4.0.0), methods,\nS4Vectors(>=0.23.18), Rcpp (== 1.0-1) ,  zlibbioc"
        self.assertEqual(naming.parse_dependencies(field), [
            Dependency("R", ">=", "4.0.0"),
            Dependency("methods"),
            Dependency("S4Vectors", ">=", "0.23.18"),
            Dependency("Rcpp", "==", "1.0-1"),
            Dependency("zlibbioc"),
        ])

    def test_parse_all_operators(self):
        for op in (">=", ">", "<=", "<", "=="):
            self.assertEqual(naming.parse_dependencies(f"x ({op} 1)"), [Dependency("x", op, "1")])

    def test_parse_rejects_garbage(self):
        for field in ("foo (~> 1.0)", "foo (>= 1.0${{ x }})", "foo (>= )", "2foo"):
            with self.assertRaises(ValueError, msg=field):
                naming.parse_dependencies(field)

    def test_empty_field(self):
        self.assertEqual(naming.parse_dependencies(""), [])
        self.assertEqual(naming.parse_dependencies(" , "), [])

    def test_match_spec(self):
        self.assertEqual(naming.match_spec("r-foo", Dependency("foo", ">=", "1.2-3")), "r-foo >=1.2_3")
        self.assertEqual(naming.match_spec("r-foo", Dependency("foo", "==", "2.0")), "r-foo ==2.0")
        self.assertEqual(naming.match_spec("r-foo", Dependency("foo")), "r-foo")


class RVersionTest(unittest.TestCase):
    def test_exceeds_r(self):
        self.assertTrue(naming.exceeds_r("4.7.0", "4.6"))
        self.assertTrue(naming.exceeds_r("5.0", "4.6"))
        self.assertFalse(naming.exceeds_r("4.6.2", "4.6"))
        self.assertFalse(naming.exceeds_r("3.5.0", "4.6"))

    def test_r_base_pin(self):
        self.assertEqual(naming.r_base_pin("4.6"), "r-base >=4.6,<4.7.0a0")
        self.assertEqual(naming.r_base_pin("4.10"), "r-base >=4.10,<4.11.0a0")


class FilenameTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(naming.split_filename("bioconductor-deseq2-1.52.0-r46h1234567_0.conda"),
                         ("bioconductor-deseq2", "1.52.0", "r46h1234567_0"))
        self.assertEqual(naming.split_filename("r-bar-1!2.0-r46_0.tar.bz2"), ("r-bar", "1!2.0", "r46_0"))

    def test_invalid(self):
        for filename in ("foo-1.0.conda", "foo-1.0-0.zip", "-1.0-0.conda", "foo--0.conda",
                         "foo-1.0-0.conda/", "foo bar-1.0-0.conda", "a/b-1.0-0.conda",
                         "x" * 250 + "-1-0.conda", "repodata.json"):
            self.assertIsNone(naming.split_filename(filename), filename)


class TagsTest(unittest.TestCase):
    def test_package_tag_vectors(self):
        for subdir, filename, tag in VECTORS["package"]:
            self.assertEqual(naming.package_tag(subdir, filename), tag)
            self.assertLessEqual(len(tag), 128)

    def test_repodata_tag_vectors(self):
        for subdir, tag in VECTORS["repodata"]:
            self.assertEqual(naming.repodata_tag(subdir), tag)

    def test_failure_tag_vectors(self):
        for subdir, name, version, tag in VECTORS["failure"]:
            self.assertEqual(naming.failure_tag(subdir, name, version), tag)
            self.assertLessEqual(len(tag), 128)
