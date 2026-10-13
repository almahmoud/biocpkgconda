# biocpkgconda

A conda channel of Bioconductor 3.23 packages, built for R 4.6 on top of conda-forge.

- Channel: `https://conda.biocpkgcontainers.org`
- Platforms: `linux-64` and `osx-arm64` for packages with compiled code, `noarch` for the rest

A `noarch` package installs on a platform once its compiled dependencies are built for that platform.

## Install

Put the channel before conda-forge and use strict channel priority.

conda:

```sh
conda create -n bioc bioconductor-deseq2 \
  --override-channels --strict-channel-priority \
  -c https://conda.biocpkgcontainers.org -c conda-forge
```

mamba:

```sh
mamba create -n bioc bioconductor-deseq2 \
  --override-channels --strict-channel-priority \
  -c https://conda.biocpkgcontainers.org -c conda-forge
```

pixi:

```sh
pixi init --channel https://conda.biocpkgcontainers.org --channel conda-forge
pixi add bioconductor-deseq2
```

To make the channel the default with [Miniforge](https://conda-forge.org/download/), add this to `~/.condarc`:

```yaml
channels:
  - https://conda.biocpkgcontainers.org
  - conda-forge
channel_priority: strict
```

Nothing here uses conda's `defaults` channel (`repo.anaconda.com`), which needs a paid Anaconda license at organizations over 200 people. `--override-channels` makes conda read only the channels given on the command line. Miniforge, micromamba and pixi do not configure `defaults`. Miniconda and Anaconda installers configure it, so on those the `.condarc` above is not enough: check `conda config --show channels` and use `--override-channels`.

Do not combine this channel with bioconda in one environment. Bioconda publishes packages under the same names, built against different dependencies.

## Package names

| Package | Conda name | Example |
|---|---|---|
| Bioconductor | `bioconductor-<name in lowercase>` | `bioconductor-deseq2`, `bioconductor-bsgenome.hsapiens.ucsc.hg38` |
| CRAN | `r-<name in lowercase>` | `r-ggplot2`, `r-wgcna` |

The channel builds the CRAN packages that Bioconductor packages need, directly or through other packages, unless conda-forge has an R 4.6 build of the package in `noarch` or for every platform. conda-forge has no R 4.6 builds of them at present, so the channel builds all of them. Other CRAN packages are not in the channel.

Versions are the R package versions with `-` replaced by `_`: `XML 3.99-0.24` is `r-xml 3.99_0.24`.

## What a package contains

- The Bioconductor 3.23 or CRAN source release, checked against the MD5 sum in the repository index and installed with `R CMD INSTALL --build`.
- Run dependencies from `Depends` and `Imports`; `LinkingTo` packages are used at build time only. `Suggests` and `Enhances` are not dependencies; install those packages yourself when a vignette or optional feature needs them.
- A load test: every package runs `library(<Package>)` in a fresh environment before it is published.
- Files in `lib/R/library/<Package>/` only. Files the build writes elsewhere in the prefix, including link or activation scripts, are left out of the package.
- Compilers and system libraries from conda-forge, at the versions pinned by conda-forge.

Some packages are not built: those that depend on packages in none of Bioconductor, CRAN and conda-forge, those that need a newer R, and those whose build failed twice. The sync summary lists them with the reason.

## Build status

The [Sync channel](https://github.com/almahmoud/biocpkgconda/actions/workflows/sync.yml) workflow runs every six hours and after each batch of builds. Its run summary counts the packages in each state per platform, and lists the packages that are not built with the reason and the number of packages they hold back.

In a [Build packages](https://github.com/almahmoud/biocpkgconda/actions/workflows/build.yml) run, the build job of each package shows the rattler-build log and keeps the rendered recipe as a run artifact for 14 days.

A [Channel status](https://github.com/almahmoud/biocpkgconda/actions/workflows/status.yml) run, started by hand, keeps `status.json` as its `status` artifact for 7 days. For every package in the channel it lists the version, its dependencies in the channel, and per platform whether the package installs with all its dependencies, its build status, and the package that holds it back. It also lists the failed builds of current versions with their run and the step that failed: `recipe`, `build`, `package`, `verify` or `check`. To write the same file from the registry:

```sh
GITHUB_REPOSITORY=almahmoud/biocpkgconda python -m biocpkgconda status --out status.json
```

## Reporting a broken package

Open an [issue](https://github.com/almahmoud/biocpkgconda/issues) with the conda package name, the platform, and the error message or the failing build job.

## Fixing a package

Most fixes are a line in one of two files.

`overrides.toml` changes one package, keyed by its R name:

```toml
[package.ExamplePkg]
build = ["automake"]          # added to requirements.build
host = ["bzip2", "libcurl"]   # added to requirements.host
run = ["pandoc >=3.1"]        # added to requirements.run

[package."BSgenome.Example.UCSC.xx1"]
skip = "source tarball exceeds the runner disk"
```

Four more keys change how one package is built:

```toml
[package.ExamplePkg]
makevars = { osx-arm64 = ["CPPFLAGS += -DEXAMPLE"] }
patches = ["configure.patch"]
binary_relocation = { osx-arm64 = ["lib/R/library/ExamplePkg/bin/*"] }
env = { LIBS = "-Wl,-rpath,${{ PREFIX }}/lib" }
```

- `makevars`: make assignments (`=`, `+=`, `:=` or `?=`) added to the user Makevars of `R CMD INSTALL`, per subdir.
- `patches`: files in `patches/<Package>/`, applied in order to the unpacked source. Write them with `diff -u` against the current source tarball, with paths relative to the package directory (`a/configure`, `b/configure`). A patch applies on every subdir, and a build fails when a new release no longer matches it.
- `binary_relocation`: per subdir, `false` or globs relative to the prefix. rattler-build rewrites the rpaths and install names of only the matching files, or of none for `false`.
- `env`: environment variables exported by `build.sh` before `R CMD INSTALL`. Values may hold letters, digits, spaces, `,._/=+:@-`, and `${{ PREFIX }}` or `${{ BUILD_PREFIX }}` for the host and build prefixes. `PATH`, `PREFIX`, `BUILD_PREFIX`, `SRC_DIR` and `HOME` cannot be set.

`sysreqs.toml` maps the `SystemRequirements` field of any package to conda-forge packages:

```toml
[[rule]]
pattern = '(?i)\bgsl\b|gnu scientific library'   # Python regular expression
host = ["gsl"]

[[rule]]
pattern = '(?i)openmp'
host = ["llvm-openmp"]
platforms = ["osx-arm64", "osx-64"]               # only for these subdirs
```

Entries are conda match specs: a package name with an optional version constraint. A fix applies the next time the package is built. A package that is already published is rebuilt at its next release; one whose build failed twice is retried by a Sync channel run with `retry_failed` set.

To preview the recipe for a package (Python 3.12, `pip install -r requirements.txt`, network access):

```sh
python -m biocpkgconda recipe --target linux-64/DESeq2 --out work/recipe
```

Run the tests with `python -m unittest`.

## Building a package locally

`linux-64` and `noarch` packages are built in `ghcr.io/almahmoud/biocpkgconda-builder:3.23`, which holds the pinned rattler-build and conda-forge build settings. With Docker and network access, from the repository root:

```sh
mkdir -p work

docker run --rm --init --platform linux/amd64 --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
  -v "$PWD:/work:ro" -v "$PWD/work:/work/work" -w /work \
  ghcr.io/almahmoud/biocpkgconda-builder:3.23 \
  python -m biocpkgconda recipe --target linux-64/DESeq2 --out work/recipe

docker run --rm --init --platform linux/amd64 --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
  -e GITHUB_REPOSITORY=almahmoud/biocpkgconda -v "$PWD:/work:ro" -v "$PWD/work:/work/work" -w /work \
  ghcr.io/almahmoud/biocpkgconda-builder:3.23 \
  python -m biocpkgconda build --recipe work/recipe --output-dir work/out \
    --variant-config /opt/conda-build-config/conda_build_config.yaml
```

The build runs the package's own `configure` and `Makevars`, so the container sees the checkout read-only and writes to `work/` only. The package is written under `work/out/`. The build reads the channel's packages anonymously from the GHCR repository of `GITHUB_REPOSITORY` (or `channel.registry` in `channel.toml`) through a mirror on 127.0.0.1. The image is multi-arch, and `--platform linux/amd64` keeps an arm64 host from building for `linux-aarch64`, a subdir the channel neither publishes nor resolves dependencies for.

## Repository layout

| Path | Contents |
|---|---|
| `channel.toml` | Bioconductor and R versions, platforms and their runners, build limits, pinned tools |
| `overrides.toml` | Per-package recipe fixes |
| `patches/` | Source patches named in `overrides.toml`, one directory per package |
| `sysreqs.toml` | `SystemRequirements` rules |
| `biocpkgconda/` | Metadata, recipe rendering, planning, indexing and publishing (`python -m biocpkgconda --help`) |
| `dashboard/` | Static status page that reads `status.json` from its own directory |
| `docker/` | Builder image `ghcr.io/almahmoud/biocpkgconda-builder:<Bioconductor version>`, based on `ghcr.io/bioconductor/r-ver` |
| `.github/workflows/` | `sync.yml` plans and dispatches builds; `build.yml` runs `package.yml` for each target, which builds, tests and publishes it; `status.yml` writes `status.json`; `builder-image.yml` publishes the builder image; `tests.yml` runs the unit tests |

## License

MIT. See [LICENSE](LICENSE).
