"""Command line interface: python -m biocpkgconda <command>."""

import argparse
import json
import sys
from pathlib import Path

from . import Error, config, index, metadata, recipe, record
from .naming import parse_target
from .registry import Registry


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


def _registry(channel: config.Channel):
    host, repository = config.registry_reference(channel)
    return Registry.from_environment(host, repository), config.source_url(repository)


def cmd_check_package(args) -> None:
    target = parse_target(args.target)
    for path, _ in record.check(args.files, target, metadata.load(_channel(args), args.cache)):
        print(f"{path.name} is a valid build of {args.target}")


def cmd_publish(args) -> None:
    channel = _channel(args)
    registry, source = _registry(channel)
    target = parse_target(args.target)
    record.publish(args.files, target, registry, metadata.load(channel, args.cache), source)


def cmd_fail(args) -> None:
    channel = _channel(args)
    registry, source = _registry(channel)
    target = parse_target(args.target)
    record.fail(target, args.run_url, registry, metadata.load(channel, args.cache), source)


def cmd_index(args) -> None:
    channel = _channel(args)
    registry, source = _registry(channel)
    state, _ = index.index(registry, channel.subdirs, source)
    args.out.write_text(json.dumps(state))


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

    command = commands.add_parser("check-package", parents=[common],
                                  help="check that built .conda files are the package and install only its files")
    command.add_argument("--target", required=True, help="<subdir>/<Package> the files were built for")
    command.add_argument("files", nargs="+", type=Path, metavar="FILE")
    command.set_defaults(func=cmd_check_package)

    command = commands.add_parser("publish", parents=[common], help="push the built .conda file of a package")
    command.add_argument("--target", required=True, help="<subdir>/<Package> the files were built for")
    command.add_argument("files", nargs="+", type=Path, metavar="FILE")
    command.set_defaults(func=cmd_publish)

    command = commands.add_parser("fail", parents=[common], help="record a failed build of a package")
    command.add_argument("--target", required=True, help="<subdir>/<Package>")
    command.add_argument("--run-url", required=True, help="URL of the failed workflow run")
    command.set_defaults(func=cmd_fail)

    command = commands.add_parser("index", parents=[common], help="rebuild repodata from the registry")
    command.add_argument("--out", type=Path, required=True, metavar="FILE", help="write the channel state here")
    command.set_defaults(func=cmd_index)

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
