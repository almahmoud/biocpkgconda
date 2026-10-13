import dataclasses
import io
import os
import shutil
import subprocess
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
        self.assertNotIn("patches", result["source"])
        self.assertEqual(result["build"], {"number": 0, "script": "build.sh", "files": ["lib/R/library/DESeq2/**"],
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
        self.assertEqual(result["build"], {"number": 0, "noarch": "generic", "script": "build.sh",
                                           "files": ["lib/R/library/BiocGenerics/**"]})
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

    def test_patches(self):
        directory = ROOT / "patches" / "DESeq2"
        override = Override(patches=(directory / "first.patch", directory / "second.patch"))
        source = self.render("DESeq2", deseq2_tarball(), "linux-64", override)["source"]
        self.assertEqual(source["patches"], ["first.patch", "second.patch"])

    def test_binary_relocation(self):
        override = Override(binary_relocation={"osx-arm64": False, "linux-64": ("lib/R/library/DESeq2/bin/*",)})
        osx = self.render("DESeq2", deseq2_tarball(), "osx-arm64", override)["build"]["dynamic_linking"]
        self.assertIs(osx["binary_relocation"], False)
        self.assertIn("  binary_relocation: false", recipe.dump_yaml({"dynamic_linking": osx}).splitlines())
        linux = self.render("DESeq2", deseq2_tarball(), "linux-64", override)["build"]["dynamic_linking"]
        self.assertEqual(linux["binary_relocation"], ["lib/R/library/DESeq2/bin/*"])
        other = Override(binary_relocation={"osx-arm64": False})
        linux = self.render("DESeq2", deseq2_tarball(), "linux-64", other)["build"]["dynamic_linking"]
        self.assertEqual(linux, {"rpaths": ["lib/R/lib/", "lib/"]})

    def test_java_rpath(self):
        result = self.render("DESeq2", deseq2_tarball(), "linux-64", Override(host=("openjdk",)))
        self.assertEqual(result["build"]["dynamic_linking"]["rpaths"], ["lib/R/lib/", "lib/", "lib/jvm/lib/server/"])
        self.assertTrue(recipe.uses_java(result["requirements"]["host"]))
        self.assertFalse(recipe.uses_java(["r-base", "openjdk-headers", "r-rjava"]))

    def test_env_stays_out_of_the_recipe(self):
        override = Override(env={"LIBS": "-Wl,-rpath,${{ PREFIX }}/lib"})
        result = self.render("DESeq2", deseq2_tarball(), "linux-64", override)
        self.assertEqual(result["build"]["script"], "build.sh")
        self.assertNotIn("LIBS", recipe.dump_yaml(result))

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


class BuildScriptTest(unittest.TestCase):
    def test_noarch(self):
        self.assertEqual(recipe.build_script("BSgenome.X", "noarch", False, False, None), "\n".join([
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "if [ -f configure ] && grep -Fqx 'ac_unique_file=\"BSgenome.X\"' configure; then",
            "  LC_ALL=C sed -i.orig 's/^ac_unique_file=\"BSgenome\\.X\"$/ac_unique_file=\"DESCRIPTION\"/' configure",
            "fi",
            "R CMD INSTALL --build .",
            "",
        ]))

    def test_linux(self):
        script = recipe.build_script("DESeq2", "linux-64", True, False, None)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\nset -euo pipefail\nfor f in src/Makevars "))
        self.assertIn(r"-e 's#(^|[[:space:]=])-(I|L)[[:space:]]*/usr/(include|lib)/?([[:space:]]|$)#\1\4#' -e 'ta'",
                      script)
        self.assertIn("unset GPROF\n", script)
        self.assertIn(r"printf 'CC%s = %s -std=gnu%s\nC%sFLAGS = %s\n' ", script)
        self.assertTrue(script.endswith('export R_MAKEVARS_USER="$makevars"\nR CMD INSTALL --build .\n'))
        for text in ("strip -S", "ac_cv_host", "<<'EOF'", "javareconf", "otool"):
            self.assertNotIn(text, script)

    def test_osx_arm64(self):
        script = recipe.build_script("DESeq2", "osx-arm64", True, False, None)
        self.assertIn("    LC_ALL=C sed -i.orig 's|strip --strip-debug|strip -S|g' \"$f\"\n  fi\n", script)
        self.assertIn("export ac_cv_build=aarch64-apple-darwin20.0.0 ac_cv_host=aarch64-apple-darwin20.0.0\n", script)
        self.assertIn("cat >> \"$makevars\" <<'EOF'\n"
                      "CPPFLAGS += -D_LIBCPP_ENABLE_CXX17_REMOVED_RANDOM_SHUFFLE -D_LIBCPP_DISABLE_AVAILABILITY\n"
                      "CXXFLAGS += -Wno-register\n", script)
        self.assertIn("CXX26FLAGS += -Wno-register\nEOF\nexport R_MAKEVARS_USER=\"$makevars\"\n", script)
        self.assertIn('for lib in "$PREFIX/lib/R/library/DESeq2/libs/"*.so; do\n', script)
        self.assertLess(script.index("R CMD INSTALL --build ."), script.index("-delete_rpath"))
        self.assertNotIn("GPROF", script)
        self.assertNotIn("declare -A", script)

    def test_java(self):
        linux = recipe.build_script("rJava", "linux-64", True, True, None)
        self.assertIn('export JAVA_HOME="$PREFIX/lib/jvm"\nexport PATH="$JAVA_HOME/bin:$PATH"\n'
                      'export LD_LIBRARY_PATH="$JAVA_HOME/lib/server${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
                      "R CMD javareconf\n", linux)
        self.assertLess(linux.index("R CMD javareconf"), linux.index("R CMD INSTALL"))
        osx = recipe.build_script("rJava", "osx-arm64", True, True, None)
        self.assertIn('export PATH="$JAVA_HOME/bin:$PATH"\nR CMD javareconf\n', osx)
        self.assertNotIn("LD_LIBRARY_PATH", osx)

    def test_override(self):
        override = Override(makevars={"linux-64": ("CPPFLAGS += -D_GNU_SOURCE",), "osx-arm64": ("CFLAGS += -Wno-x",)},
                            env={"LIBS": "-Wl,-rpath,${{ PREFIX }}/lib", "TOOL": "${{ BUILD_PREFIX }}/bin/tool"})
        linux = recipe.build_script("seqminer", "linux-64", True, False, override)
        self.assertIn("cat >> \"$makevars\" <<'EOF'\nCPPFLAGS += -D_GNU_SOURCE\nEOF\n", linux)
        self.assertNotIn("-Wno-x", linux)
        self.assertIn('export LIBS="-Wl,-rpath,${PREFIX}/lib"\nexport TOOL="${BUILD_PREFIX}/bin/tool"\n', linux)
        self.assertLess(linux.index("export LIBS"), linux.index("R CMD INSTALL"))
        osx = recipe.build_script("seqminer", "osx-arm64", True, False, override)
        self.assertIn("CXX26FLAGS += -Wno-register\nCFLAGS += -Wno-x\nEOF\n", osx)
        noarch = recipe.build_script("seqminer", "noarch", False, False, override)
        self.assertNotIn("makevars", noarch)
        self.assertIn('export LIBS="-Wl,-rpath,${PREFIX}/lib"\n', noarch)

    def test_invalid_name(self):
        for name in ("", "1A", "A'B", "A;B", "A/B", "A B", "A\nB"):
            with self.subTest(name=name), self.assertRaisesRegex(Error, "invalid R package name"):
                recipe.build_script(name, "linux-64", True, False, None)


# Stands in for R: reports CC17 as empty and saves the user Makevars that R CMD INSTALL would read.
FAKE_R = """\
#!/usr/bin/env bash
case "$*" in
  "CMD config CC") echo cc ;;
  "CMD config CFLAGS") echo -O2 ;;
  "CMD config CC17") echo ;;
  "CMD config CC"*) echo "cc -std=gnu${3#CC}" ;;
  "CMD INSTALL --build .") cp "${R_MAKEVARS_USER:-/dev/null}" installed-makevars ;;
  *) exit 1 ;;
esac
"""

MAKEVARS = """\
PKG_CPPFLAGS = -I/usr/include -I/usr/include/ -DX -I /usr/include
PKG_CFLAGS=-I/usr/include
PKG_LIBS = -L/usr/lib -lz -L/usr/lib/x86_64-linux-gnu -I/usr/include/eigen3
all: $(SHLIB)
\tstrip --strip-debug $(SHLIB)
"""

CONFIGURE = """\
#! /bin/sh
ac_unique_file="Pkg.Name"
ac_unique_file="PkgXName"
"""


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class BuildScriptRunTest(unittest.TestCase):
    def run_script(self, subdir, compiled=True):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source, bin_dir = tmp / "source", tmp / "bin"
            (source / "src").mkdir(parents=True)
            bin_dir.mkdir()
            (source / "src" / "Makevars").write_text(MAKEVARS)
            (source / "src" / "Makevars.in").write_text(MAKEVARS)
            (source / "configure").write_text(CONFIGURE)
            (bin_dir / "R").write_text(FAKE_R)
            (bin_dir / "R").chmod(0o755)
            (source / "build.sh").write_text(recipe.build_script("Pkg.Name", subdir, compiled, False, None))
            env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}", "PREFIX": str(tmp / "prefix"),
                   "HOME": str(tmp), "TMPDIR": str(tmp), "LC_ALL": "C"}
            result = subprocess.run(["bash", "build.sh"], cwd=source, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return {name: (source / name).read_text()
                    for name in ("src/Makevars", "src/Makevars.in", "configure", "installed-makevars")}

    def test_linux(self):
        result = self.run_script("linux-64")
        for name in ("src/Makevars", "src/Makevars.in"):
            lines = result[name].splitlines()
            self.assertEqual(lines[0].split(), ["PKG_CPPFLAGS", "=", "-DX"])
            self.assertEqual(lines[1], "PKG_CFLAGS=")
            self.assertEqual(lines[2].split(), ["PKG_LIBS", "=", "-lz", "-L/usr/lib/x86_64-linux-gnu",
                                                "-I/usr/include/eigen3"])
            self.assertEqual(lines[4], "\tstrip --strip-debug $(SHLIB)")
        self.assertEqual(result["configure"], CONFIGURE.replace('"Pkg.Name"', '"DESCRIPTION"'))
        self.assertEqual(result["installed-makevars"], "CC17 = cc -std=gnu17\nC17FLAGS = -O2\n")

    def test_osx_arm64(self):
        result = self.run_script("osx-arm64")
        self.assertEqual(result["src/Makevars"].splitlines()[4], "\tstrip -S $(SHLIB)")
        makevars = result["installed-makevars"].splitlines()
        self.assertEqual(makevars[:2], ["CC17 = cc -std=gnu17", "C17FLAGS = -O2"])
        self.assertEqual(makevars[2:], list(recipe._OSX_MAKEVARS))

    def test_noarch(self):
        result = self.run_script("noarch", compiled=False)
        self.assertEqual(result["src/Makevars"], MAKEVARS)
        self.assertEqual(result["configure"], CONFIGURE.replace('"Pkg.Name"', '"DESCRIPTION"'))
        self.assertEqual(result["installed-makevars"], "")


class WriteTest(unittest.TestCase):
    def test_copies_patches(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch = Path(tmp) / "patches" / "Pkg" / "fix.patch"
            patch.parent.mkdir(parents=True)
            patch.write_text("--- a/configure\n+++ b/configure\n")
            out = Path(tmp) / "recipe"
            recipe.write({"package": {"name": "r-pkg"}}, out, "#!/usr/bin/env bash\n", (patch,))
            self.assertEqual((out / "recipe.yaml").read_text(), 'package:\n  name: "r-pkg"\n')
            self.assertEqual((out / "build.sh").read_text(), "#!/usr/bin/env bash\n")
            self.assertTrue((out / "build.sh").stat().st_mode & 0o111)
            self.assertEqual((out / "fix.patch").read_text(), patch.read_text())


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
            self.assertEqual(script.read_text(), recipe.build_script("DESeq2", "linux-64", True, False, None))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["build.sh", "recipe.yaml"])
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
        overrides = config.load_overrides(ROOT / "overrides.toml")
        used = {path for override in overrides.values() for path in override.patches}
        self.assertEqual(used, set((ROOT / "patches").glob("*/*.patch")))

    def test_rules(self):
        rules = config.load_sysreqs(ROOT / "sysreqs.toml")

        def hosts(text, subdir="linux-64"):
            return [h for r in rules if r.applies(text, subdir) for h in r.host]

        self.assertEqual(hosts("GNU make, gsl"), ["gsl"])
        self.assertEqual(hosts("libcurl4-openssl-dev, libxml2-dev, libssl-dev"), ["libxml2-devel", "libcurl", "openssl"])
        self.assertEqual(hosts("libbz2 & liblzma & libcurl (with header files)"), ["libcurl", "bzip2", "xz"])
        self.assertEqual(hosts("C++17"), [])
        self.assertEqual(hosts("OpenMP", "osx-arm64"), ["llvm-openmp"])
        self.assertEqual(hosts("X11"), ["xorg-xproto"])
        self.assertEqual(hosts("X11", "osx-arm64"), [])

        def builds(text, subdir="linux-64"):
            return [b for r in rules if r.applies(text, subdir) for b in r.build]

        self.assertEqual(builds("Cargo (Rust's package manager), rustc"), ["rust"])
        self.assertEqual(builds("Cargo (Rust's package manager), rustc", "noarch"), [])

    def invalid(self, loader, text, message=""):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.toml"
            path.write_text(text)
            with self.assertRaisesRegex(Error, message):
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
        self.invalid(config.load_overrides, "[package.'../A']\nhost = ['gsl']\n", "invalid R package name")

    def test_invalid_makevars(self):
        for value in ("['CFLAGS += -O2']", "{ noarch = ['CFLAGS += -O2'] }", "{ osx-arm65 = ['CFLAGS += -O2'] }",
                      "{ linux-64 = [] }", "{ linux-64 = 'CFLAGS += -O2' }", "{ linux-64 = ['CFLAGS'] }",
                      "{ linux-64 = ['-O2'] }", '{ linux-64 = ["CFLAGS += -O2\\nEOF"] }'):
            with self.subTest(value=value):
                self.invalid(config.load_overrides, f"[package.A]\nmakevars = {value}\n", "makevars")

    def test_invalid_patches(self):
        for value in ("'fix.patch'", "['missing.patch']", "['../fix.patch']", "['fix.diff']", "['a/fix.patch']",
                      "[1]"):
            with self.subTest(value=value):
                self.invalid(config.load_overrides, f"[package.A]\npatches = {value}\n", "patches")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "patches" / "A").mkdir(parents=True)
            (Path(tmp) / "patches" / "A" / "fix.patch").write_text("")
            path = Path(tmp) / "overrides.toml"
            path.write_text("[package.A]\npatches = ['fix.patch', 'fix.patch']\n")
            with self.assertRaisesRegex(Error, "distinct"):
                config.load_overrides(path)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "outside").mkdir()
            (tmp / "outside" / "fix.patch").write_text("")
            (tmp / "repo" / "patches" / "B").mkdir(parents=True)
            (tmp / "repo" / "patches" / "C").mkdir()
            (tmp / "repo" / "patches" / "C" / "fix.patch").write_text("")
            (tmp / "repo" / "patches" / "A").symlink_to(tmp / "outside")
            (tmp / "repo" / "patches" / "B" / "fix.patch").symlink_to(tmp / "outside" / "fix.patch")
            (tmp / "other").mkdir()
            (tmp / "other" / "patches").symlink_to(tmp / "repo" / "patches")
            for root, package in (("repo", "A"), ("repo", "B"), ("other", "C")):
                with self.subTest(root=root, package=package):
                    path = tmp / root / "overrides.toml"
                    path.write_text(f"[package.{package}]\npatches = ['fix.patch']\n")
                    with self.assertRaisesRegex(Error, "not a file"):
                        config.load_overrides(path)

    def test_invalid_binary_relocation(self):
        for value in ("false", "{ osx-arm64 = true }", "{ osx-arm64 = [] }", "{ osx-arm64 = ['lib/$(x)'] }",
                      "{ osx-arm64 = ['lib/a b'] }", "{ noarch = false }", "{ osx-arm64 = 'lib/*' }"):
            with self.subTest(value=value):
                self.invalid(config.load_overrides, f"[package.A]\nbinary_relocation = {value}\n", "binary_relocation")

    def test_invalid_env(self):
        for value in ("'x'", "{ libs = 'x' }", "{ 1LIBS = 'x' }", "{ PATH = '/bin' }", "{ PREFIX = '/opt' }",
                      "{ HOME = '/tmp' }", "{ LIBS = '$(id)' }", "{ LIBS = '`id`' }", "{ LIBS = '\"x' }",
                      "{ LIBS = 'a;b' }", "{ LIBS = '$PREFIX' }", "{ LIBS = '${{ SRC_DIR }}' }",
                      "{ LIBS = '${{PREFIX}}' }", "{ LIBS = 1 }", '{ LIBS = "a\\nb" }'):
            with self.subTest(value=value):
                self.invalid(config.load_overrides, f"[package.A]\nenv = {value}\n", "env")

    def test_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overrides.toml"
            patch = Path(tmp) / "patches" / "Rhtslib" / "fix.patch"
            patch.parent.mkdir(parents=True)
            patch.write_text("")
            path.write_text('[package."BSgenome.X"]\nskip = "too large"\n[package.Rhtslib]\nhost = ["xz", "libcurl >=8"]\n'
                            'patches = ["fix.patch"]\n'
                            'makevars = { osx-arm64 = ["CFLAGS += -Wno-error", "FLAG:=1", "X ?= a $(Y)"] }\n'
                            'binary_relocation = { osx-arm64 = false, linux-64 = ["lib/R/library/Rhtslib/bin/*"] }\n'
                            'env = { LIBS = "-Wl,-rpath,${{ PREFIX }}/lib", CC_X = "${{ BUILD_PREFIX }}/bin a=b" }\n')
            overrides = config.load_overrides(path)
            self.assertEqual(overrides["Rhtslib"].patches, (patch,))
        self.assertEqual(overrides["BSgenome.X"].skip, "too large")
        self.assertEqual(overrides["BSgenome.X"].patches, ())
        self.assertEqual(overrides["BSgenome.X"].makevars, {})
        rhtslib = overrides["Rhtslib"]
        self.assertEqual(rhtslib.host, ("xz", "libcurl >=8"))
        self.assertEqual(rhtslib.makevars, {"osx-arm64": ("CFLAGS += -Wno-error", "FLAG:=1", "X ?= a $(Y)")})
        self.assertEqual(rhtslib.binary_relocation, {"osx-arm64": False, "linux-64": ("lib/R/library/Rhtslib/bin/*",)})
        self.assertEqual(rhtslib.env, {"LIBS": "-Wl,-rpath,${{ PREFIX }}/lib", "CC_X": "${{ BUILD_PREFIX }}/bin a=b"})
