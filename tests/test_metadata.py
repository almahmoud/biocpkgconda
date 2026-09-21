import io
import json
import unittest
from contextlib import redirect_stderr
from unittest import mock

from biocpkgconda import Error, metadata
from biocpkgconda.naming import Dependency

from .support import channel, fixture, requires_zstandard


class ParseTest(unittest.TestCase):
    def test_bioc_package(self):
        packages = metadata.parse_bioc(fixture("PACKAGES.bioc"), "3.23", "bioc")
        deseq2 = packages["DESeq2"]
        self.assertEqual(deseq2.conda_name, "bioconductor-deseq2")
        self.assertEqual(deseq2.conda_version, "1.52.0")
        self.assertTrue(deseq2.needs_compilation)
        self.assertEqual(deseq2.md5, "5e8a7c1f0d9b3a2e4c6f8a0b1d3e5f7a")
        self.assertEqual(deseq2.license, "LGPL (>= 3)")
        self.assertEqual(deseq2.url, "https://bioconductor.org/packages/3.23/bioc/src/contrib/DESeq2_1.52.0.tar.gz")
        self.assertEqual(deseq2.homepage, "https://bioconductor.org/packages/3.23/bioc/html/DESeq2.html")
        self.assertIn(Dependency("SummarizedExperiment", ">=", "1.1.6"), deseq2.depends)
        self.assertNotIn("methods", [d.name for d in deseq2.depends])
        self.assertNotIn("stats4", [d.name for d in deseq2.depends])
        self.assertEqual(deseq2.linking_to, (Dependency("Rcpp"), Dependency("RcppArmadillo")))
        self.assertEqual(deseq2.strong_dependencies[-2:], ("MatrixGenerics", "RcppArmadillo"))
        self.assertEqual(deseq2.strong_dependencies.count("Rcpp"), 1)
        self.assertIsNone(deseq2.r_version)

    def test_r_requirement(self):
        packages = metadata.parse_bioc(fixture("PACKAGES.bioc"), "3.23", "bioc")
        self.assertEqual(packages["BiocGenerics"].r_version, "4.0.0")
        self.assertEqual(packages["BiocGenerics"].depends, (Dependency("generics"),))
        self.assertFalse(packages["BiocGenerics"].needs_compilation)
        self.assertEqual(packages["NewR"].r_version, "4.7.0")

    def test_data_repository_urls(self):
        packages = metadata.parse_bioc(fixture("PACKAGES.bioc"), "3.23", "data/annotation")
        self.assertEqual(packages["NewR"].url,
                         "https://bioconductor.org/packages/3.23/data/annotation/src/contrib/NewR_1.0.0.tar.gz")

    def test_cran_skips_copies_for_other_r_versions(self):
        packages = metadata.parse_cran(fixture("PACKAGES.cran"))
        self.assertEqual(set(packages), {"Rcpp", "Matrix"})
        matrix = packages["Matrix"]
        self.assertEqual(matrix.md5, "33333333333333333333333333333333")
        self.assertEqual(matrix.conda_name, "r-matrix")
        self.assertEqual(matrix.conda_version, "1.7_6")
        self.assertEqual(matrix.url, "https://cloud.r-project.org/src/contrib/Matrix_1.7-6.tar.gz")
        self.assertEqual(matrix.homepage, "https://cran.r-project.org/package=Matrix")
        self.assertEqual(matrix.depends, (Dependency("lattice"),))

    def test_malformed_dependencies_skip_the_package(self):
        with redirect_stderr(io.StringIO()) as stderr:
            packages = metadata.parse_cran("Package: Broken\nVersion: 1.0\nImports: x (~ 1)\n\n"
                                           "Package: Fine\nVersion: 1.0\n")
        self.assertEqual(list(packages), ["Fine"])
        self.assertIn("'Broken'", stderr.getvalue())

    def test_invalid_records_are_skipped(self):
        text = ("Package: ok\nVersion: 1.0-2\nMD5sum: NOT-A-DIGEST\n\n"
                "Package: bad name\nVersion: 1.0\n\nPackage: badversion\nVersion: 1.0${{x}}\n")
        with redirect_stderr(io.StringIO()) as stderr:
            packages = metadata.parse_cran(text)
        self.assertEqual(list(packages), ["ok"])
        self.assertEqual(packages["ok"].md5, "")
        self.assertEqual(stderr.getvalue().count("skipping"), 2)

    def test_sources_prefer_bioconductor(self):
        bioc = metadata.parse_bioc(fixture("PACKAGES.bioc"), "3.23", "bioc")
        cran = metadata.parse_cran(fixture("PACKAGES.cran"))
        cran["DESeq2"] = cran["Rcpp"]
        sources = metadata.Sources(bioc, cran)
        self.assertTrue(sources.get("DESeq2").is_bioc)
        self.assertFalse(sources.get("Rcpp").is_bioc)
        self.assertIsNone(sources.get("biclust"))


class FetchTest(unittest.TestCase):
    def test_bioc_packages_fetches_each_repository(self):
        urls = []

        def fake_fetch(url, cache=None):
            urls.append(url)
            return fixture("PACKAGES.bioc").encode()

        with mock.patch.object(metadata, "fetch", fake_fetch):
            packages = metadata.bioc_packages(channel())
        self.assertEqual(urls, [f"https://bioconductor.org/packages/3.23/{r}/src/contrib/PACKAGES"
                                for r in ("bioc", "data/annotation", "data/experiment", "workflows")])
        self.assertEqual(packages["DESeq2"].repository, "workflows")

    def test_r_packages_for(self):
        repodata = json.loads(fixture("repodata.json"))
        self.assertEqual(metadata.r_packages_for(repodata, "4.6"), {"r-ggplot2", "r-xml"})
        self.assertEqual(metadata.r_packages_for(repodata, "4.5"), {"r-ggplot2", "r-base64enc", "r-xml"})

    @requires_zstandard
    def test_conda_forge_r(self):
        import zstandard

        def compiled(*names):
            return {"packages.conda": {f"{n}-1.0-r46h0_0.conda": {"name": n, "version": "1.0",
                                                                   "depends": ["r-base >=4.6,<4.7.0a0"]}
                                       for n in names}}

        repodata = {"noarch": json.loads(fixture("repodata.json")),
                    "linux-64": compiled("r-both", "r-linux"),
                    "osx-arm64": compiled("r-both", "r-osx")}
        urls = []

        def fake_fetch(url, cache=None):
            urls.append(url)
            return zstandard.ZstdCompressor().compress(json.dumps(repodata[url.split("/")[-2]]).encode())

        with mock.patch.object(metadata, "fetch", fake_fetch):
            self.assertEqual(metadata.conda_forge_r(channel()), frozenset({"r-ggplot2", "r-xml", "r-both"}))
        self.assertEqual(urls, [f"https://conda.anaconda.org/conda-forge/{s}/repodata.json.zst" for s in repodata])


class RVersionTest(unittest.TestCase):
    def test_r_version_for_bioc(self):
        text = fixture("config.yaml")
        self.assertEqual(metadata.r_version_for_bioc(text, "3.22"), "4.5")
        self.assertEqual(metadata.r_version_for_bioc(text, "3.23"), "4.6")
        self.assertIsNone(metadata.r_version_for_bioc(text, "3.99"))
        self.assertIsNone(metadata.r_version_for_bioc("release_version: 1\n", "3.23"))

    def test_check_r_version(self):
        with mock.patch.object(metadata, "fetch", return_value=fixture("config.yaml").encode()):
            metadata.check_r_version(channel())
        mismatched = fixture("config.yaml").replace('"3.23": "4.6"', '"3.23": "4.7"')
        with mock.patch.object(metadata, "fetch", return_value=mismatched.encode()):
            with self.assertRaisesRegex(Error, "requires R 4.7"):
                metadata.check_r_version(channel())
