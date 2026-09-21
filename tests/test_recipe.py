import dataclasses
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from biocpkgconda import Error, __main__, config, recipe
from biocpkgconda.config import Override

from .support import ROOT, channel, md5, sources, tarball

DESEQ2_DESCRIPTION = """\
Package: DESeq2
Type: Package
Title: Differential gene expression analysis based on the negative
        binomial distribution
Version: 1.52.0
License: LGPL (>= 3)
SystemRequirements: GNU make, OpenMP
"""

GENERICS_DESCRIPTION = """\
Package: BiocGenerics
Title: S4 generic functions used in Bioconductor
Version: 0.58.1
License: MIT + file LICENSE
"""


def deseq2_tarball():
    return tarball("DESeq2", {"DESCRIPTION": DESEQ2_DESCRIPTION, "R/core.R": "",
                              "src/DESeq2.cpp": "", "src/Makevars": "", "src/init.c": ""})


class Case(unittest.TestCase):
    def setUp(self):
        self.channel = channel()
        self.sources = sources()
        self.rules = config.load_sysreqs(ROOT / "sysreqs.toml")

    def package(self, name, data):
        package = dataclasses.replace(self.sources.get(name), md5=md5(data))
        target = self.sources.bioc if package.is_bioc else self.sources.cran
        target[name] = package
        return package

    def render(self, name, data, subdir, override=None):
        package = self.package(name, data)
        with mock.patch.object(recipe, "open_stream", return_value=io.BytesIO(data)):
            info = recipe.fetch_source(package)
        return recipe.render(package, info, subdir, self.channel, self.sources, self.rules, override)


class RenderTest(Case):
    def test_compiled_package(self):
        result = self.render("DESeq2", deseq2_tarball(), "linux-64")
        self.assertEqual(result["package"], {"name": "bioconductor-deseq2", "version": "1.52.0"})
        self.assertEqual(result["source"]["url"],
                         "https://bioconductor.org/packages/3.23/bioc/src/contrib/DESeq2_1.52.0.tar.gz")
        self.assertEqual(result["build"], {"number": 0, "script": "build.sh",
                                           "dynamic_linking": {"rpaths": ["lib/R/lib/", "lib/"]}})
        requirements = result["requirements"]
        self.assertEqual(requirements["build"], ["${{ compiler('c') }}", "${{ stdlib('c') }}",
                                                 "${{ compiler('cxx') }}", "make", "pkg-config"])
        self.assertEqual(requirements["host"][0], "r-base")
        self.assertIn("bioconductor-summarizedexperiment >=1.1.6", requirements["host"])
        self.assertIn("r-rcpp >=0.11.0", requirements["host"])
        self.assertIn("r-rcpparmadillo", requirements["host"])
        self.assertEqual(requirements["host"].count("r-rcpp >=0.11.0"), 1)
        self.assertEqual(requirements["host"][-1], "zlib")
        self.assertNotIn("llvm-openmp", requirements["host"])
        self.assertEqual(requirements["run"][0], "r-base >=4.6,<4.7.0a0")
        self.assertIn("r-ggplot2 >=3.4.0", requirements["run"])
        self.assertNotIn("r-rcpparmadillo", requirements["run"])
        self.assertEqual(result["tests"], [{"r": {"libraries": ["DESeq2"]}}])
        self.assertEqual(result["about"], {
            "homepage": "https://bioconductor.org/packages/3.23/bioc/html/DESeq2.html",
            "license": "LGPL-3.0-or-later",
            "license_file": ["${{ PREFIX }}/lib/R/share/licenses/LGPL-3"],
            "summary": "Differential gene expression analysis based on the negative binomial distribution",
        })

    def test_platform_rules(self):
        result = self.render("DESeq2", deseq2_tarball(), "osx-arm64")
        self.assertIn("llvm-openmp", result["requirements"]["host"])

    def test_fortran(self):
        data = tarball("DESeq2", {"DESCRIPTION": DESEQ2_DESCRIPTION, "src/lib/solve.f90": ""})
        build = self.render("DESeq2", data, "linux-64")["requirements"]["build"]
        self.assertIn("${{ compiler('fortran') }}", build)
        self.assertNotIn("${{ compiler('cxx') }}", build)

    def test_noarch_package(self):
        data = tarball("BiocGenerics", {"DESCRIPTION": GENERICS_DESCRIPTION, "LICENSE": "YEAR: 2025", "R/a.R": ""})
        result = self.render("BiocGenerics", data, "noarch")
        self.assertEqual(result["build"], {"number": 0, "noarch": "generic", "script": "build.sh"})
        self.assertEqual(result["requirements"], {"host": ["r-base", "r-generics"],
                                                  "run": ["r-base >=4.6,<4.7.0a0", "r-generics"]})
        self.assertEqual(result["about"]["license"], "MIT")
        self.assertEqual(result["about"]["license_file"], ["LICENSE"])

    def test_title_is_not_a_template(self):
        description = GENERICS_DESCRIPTION.replace("S4 generic functions", "${{ env.get('X') }} functions")
        data = tarball("BiocGenerics", {"DESCRIPTION": description})
        summary = self.render("BiocGenerics", data, "noarch")["about"]["summary"]
        self.assertEqual(summary, "$ {{ env.get('X') }} functions used in Bioconductor")

    def test_missing_license_file_is_left_out(self):
        data = tarball("BiocGenerics", {"DESCRIPTION": GENERICS_DESCRIPTION})
        self.assertNotIn("license_file", self.render("BiocGenerics", data, "noarch")["about"])

    def test_src_in_noarch_target(self):
        data = tarball("BiocGenerics", {"DESCRIPTION": GENERICS_DESCRIPTION, "src/a.c": ""})
        with self.assertRaisesRegex(Error, "contains src/"):
            self.render("BiocGenerics", data, "noarch")

    def test_overrides_and_sysreqs(self):
        description = GENERICS_DESCRIPTION + "SystemRequirements: Perl, Java (>= 8), libxml2\n"
        data = tarball("BiocGenerics", {"DESCRIPTION": description})
        override = Override(build=("perl >=5.32",), host=("libxml2-devel >=2.12",), run=("samtools",))
        requirements = self.render("BiocGenerics", data, "noarch", override)["requirements"]
        self.assertEqual(requirements["build"], ["perl >=5.32"])
        self.assertEqual(requirements["host"], ["r-base", "r-generics", "libxml2-devel >=2.12"])
        self.assertEqual(requirements["run"], ["r-base >=4.6,<4.7.0a0", "r-generics", "perl", "openjdk", "samtools"])

    def test_dependency_from_conda_forge(self):
        del self.sources.cran["locfit"]
        result = self.render("DESeq2", deseq2_tarball(), "linux-64")
        self.assertIn("r-locfit", result["requirements"]["run"])

    def test_md5_mismatch(self):
        package = dataclasses.replace(self.sources.get("DESeq2"), md5="0" * 32)
        with mock.patch.object(recipe, "open_stream", return_value=io.BytesIO(deseq2_tarball())):
            with self.assertRaisesRegex(Error, "md5"):
                recipe.fetch_source(package)

    def test_unreadable_tarball(self):
        with self.assertRaisesRegex(Error, "cannot read"):
            recipe.read_source(io.BytesIO(b"not a tarball"), "DESeq2")
        with self.assertRaisesRegex(Error, "no DESeq2/DESCRIPTION"):
            recipe.read_source(io.BytesIO(tarball("Other", {"DESCRIPTION": ""})), "DESeq2")


class CheckTest(Case):
    def test_rejections(self):
        deseq2, generics = self.sources.get("DESeq2"), self.sources.get("BiocGenerics")
        with self.assertRaisesRegex(Error, "skipped: too large"):
            recipe.check_buildable(deseq2, "linux-64", self.channel, Override(skip="too large"))
        with self.assertRaisesRegex(Error, "requires R >= 4.7.0"):
            recipe.check_buildable(self.sources.get("NewR"), "noarch", self.channel, None)
        with self.assertRaisesRegex(Error, "not noarch"):
            recipe.check_buildable(deseq2, "noarch", self.channel, None)
        with self.assertRaisesRegex(Error, "build it for noarch"):
            recipe.check_buildable(generics, "linux-64", self.channel, None)
        with self.assertRaisesRegex(Error, "not configured"):
            recipe.check_buildable(deseq2, "win-64", self.channel, None)
        recipe.check_buildable(deseq2, "linux-64", self.channel, Override(host=("gsl",)))


class MergeTest(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(recipe.merge(["r-base", "r-a", "r-a >=1.0", "r-a >=1.2_1", "r-b", "r-b"]),
                         ["r-base", "r-a >=1.2_1", "r-b"])
        self.assertEqual(recipe.merge(["r-a >=1.0", "r-a <2.0"]), ["r-a >=1.0,<2.0"])
        self.assertEqual(recipe.merge(["${{ compiler('c') }}", "${{ compiler('c') }}", "make"]),
                         ["${{ compiler('c') }}", "make"])


class YamlTest(unittest.TestCase):
    def test_dump(self):
        data = {
            "package": {"name": "r-a", "version": "1.0_2"},
            "build": {"number": 0, "noarch": "generic"},
            "requirements": {"host": ["r-base", "${{ compiler('c') }}"], "run": []},
            "tests": [{"r": {"libraries": ["A"]}, "other": "x"}],
            "about": {"summary": 'Say "hi"\\n'},
        }
        self.assertEqual(recipe.dump_yaml(data), "\n".join([
            "package:",
            '  name: "r-a"',
            '  version: "1.0_2"',
            "build:",
            "  number: 0",
            '  noarch: "generic"',
            "requirements:",
            "  host:",
            '    - "r-base"',
            '    - "${{ compiler(\'c\') }}"',
            "  run: []",
            "tests:",
            "  - r:",
            "      libraries:",
            '        - "A"',
            '    other: "x"',
            "about:",
            '  summary: "Say \\"hi\\"\\\\n"',
        ]))


class CommandTest(unittest.TestCase):
    def test_writes_recipe(self):
        data = deseq2_tarball()
        fake = sources()
        fake.bioc["DESeq2"] = dataclasses.replace(fake.bioc["DESeq2"], md5=md5(data))
        del fake.cran["locfit"]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(__main__.metadata, "load", return_value=fake), \
                mock.patch.object(__main__.metadata, "conda_forge_r", side_effect=AssertionError("not needed")), \
                mock.patch.dict(sys.modules, {"zstandard": None}), \
                mock.patch.object(recipe, "open_stream", return_value=io.BytesIO(data)), \
                mock.patch("sys.stdout", io.StringIO()):
            code = __main__.main(["recipe", "--config", str(ROOT), "--target", "linux-64/DESeq2", "--out", tmp])
            self.assertEqual(code, 0)
            text = (Path(tmp) / "recipe.yaml").read_text()
            self.assertTrue(text.startswith('package:\n  name: "bioconductor-deseq2"\n'))
            self.assertIn('    - "r-locfit"\n', text)
            script = Path(tmp) / "build.sh"
            self.assertEqual(script.read_text(), recipe.BUILD_SH)
            self.assertTrue(script.stat().st_mode & 0o111)

    def test_error_exit(self):
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(__main__.main(["recipe", "--config", str(ROOT), "--target", "DESeq2", "--out", "x"]), 1)
        self.assertIn("invalid target", stderr.getvalue())


class ConfigFilesTest(unittest.TestCase):
    def test_repository_files_load(self):
        rules = config.load_sysreqs(ROOT / "sysreqs.toml")
        self.assertGreater(len(rules), 20)
        config.load_overrides(ROOT / "overrides.toml")

    def test_rules(self):
        rules = config.load_sysreqs(ROOT / "sysreqs.toml")

        def hosts(text, subdir="linux-64"):
            return [h for r in rules if r.applies(text, subdir) for h in r.host]

        self.assertEqual(hosts("GNU make, gsl"), ["gsl"])
        self.assertEqual(hosts("libcurl4-openssl-dev, libxml2-dev, libssl-dev"), ["libxml2-devel", "libcurl", "openssl"])
        self.assertEqual(hosts("libbz2 & liblzma & libcurl (with header files)"), ["libcurl", "bzip2", "xz"])
        self.assertEqual(hosts("C++17"), [])
        self.assertEqual(hosts("OpenMP", "osx-arm64"), ["llvm-openmp"])

    def invalid(self, loader, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.toml"
            path.write_text(text)
            with self.assertRaises(Error):
                loader(path)

    def test_invalid_sysreqs(self):
        self.invalid(config.load_sysreqs, "[[rule]]\npattern = '('\nhost = ['gsl']\n")
        self.invalid(config.load_sysreqs, "[[rule]]\npattern = 'x'\nhost = ['${{ evil }}']\n")
        self.invalid(config.load_sysreqs, "[[rule]]\npattern = 'x'\nplatforms = ['osx-arm65']\n")
        self.invalid(config.load_sysreqs, "[[rule]]\npattern = 'x'\nhots = ['gsl']\n")

    def test_invalid_overrides(self):
        self.invalid(config.load_overrides, "[package.A]\nskip = ''\n")
        self.invalid(config.load_overrides, "[package.A]\nhost = 'gsl'\n")
        self.invalid(config.load_overrides, "[package.A]\nhost = ['gsl; rm -rf /']\n")
        self.invalid(config.load_overrides, "[packages.A]\nhost = ['gsl']\n")

    def test_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overrides.toml"
            path.write_text('[package."BSgenome.X"]\nskip = "too large"\n[package.Rhtslib]\nhost = ["xz", "libcurl >=8"]\n')
            overrides = config.load_overrides(path)
        self.assertEqual(overrides["BSgenome.X"].skip, "too large")
        self.assertEqual(overrides["Rhtslib"].host, ("xz", "libcurl >=8"))
