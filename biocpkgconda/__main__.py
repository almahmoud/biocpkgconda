"""Command line interface: python -m biocpkgconda <command>."""

import argparse
import sys
from pathlib import Path

from . import Error, config, metadata, recipe
from .naming import parse_target


def _channel(args) -> config.Channel:
    return config.load_channel(args.config / "channel.toml")


def cmd_recipe(args) -> None:
    channel = _channel(args)
    rules = config.load_sysreqs(args.config / "sysreqs.toml")
    overrides = config.load_overrides(args.config / "overrides.toml")
    target = parse_target(args.target)
    sources = metadata.load(channel, args.cache)
    recipe.generate(target, channel, sources, rules, overrides, args.out)
    print(f"wrote {args.out / 'recipe.yaml'}")


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=Path("."), metavar="DIR",
                        help="directory with channel.toml, overrides.toml and sysreqs.toml (default: .)")
    common.add_argument("--cache", type=Path, metavar="DIR", help="reuse metadata downloads from this directory")

    root = argparse.ArgumentParser(prog="python -m biocpkgconda",
                                   description="Build Bioconductor packages as a conda channel.")
    commands = root.add_subparsers(dest="command", required=True, metavar="<command>")

    command = commands.add_parser("recipe", parents=[common], help="render recipe.yaml and build.sh for a package")
    command.add_argument("--target", required=True, help="<subdir>/<Package>, e.g. linux-64/DESeq2")
    command.add_argument("--out", type=Path, required=True, metavar="DIR")
    command.set_defaults(func=cmd_recipe)

    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except Error as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
