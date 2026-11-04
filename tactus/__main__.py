#!/usr/bin/env python3
"""Program's entry point."""

import contextlib
import sys

from . import GeneralConstants
from .argparse_wrapper import get_args_parser
from .config_parser import ConfigParserDefaults, ConfigPaths, ParsedConfig
from .host_actions import TactusHost
from .logs import LoggerHandlers, log_elapsed_time, logger

# Enable logger if the project is being used as an application
logger.enable(GeneralConstants.PACKAGE_NAME)


@log_elapsed_time()
def main(argv=None):
    """Program's main routine.

    Note:
        This function deliberately never calls ``sys.exit()`` itself, so that it
        stays safe to call repeatedly in-process -- as
        :class:`tactus.test_runner.TestCases` does (it re-invokes this very function
        for ``case``/``start suite`` subcommands while driving `tactus test -m`/`-r`).
        Raising ``SystemExit`` here would abort that in-process loop after its first
        iteration. Process exit-code propagation lives in :func:`cli` instead, which
        is what is actually registered as the ``tactus`` console script.

    Args:
        argv: Command line arguments. Defaults to ``sys.argv[1:]``.

    Returns:
        bool or None: The value returned by the dispatched command's
            ``run_command`` callback. ``True`` or ``None`` (the latter being what
            most commands implicitly return) mean success; ``False`` means
            failure. See :func:`cli` for how this is turned into a process exit
            code.

    """
    if argv is None:
        argv = sys.argv[1:]

    args = get_args_parser().parse_args(argv)

    # Commands that manage their own config (e.g. "test") skip standard config loading
    if getattr(args, "standalone_command", False):
        return args.run_command(args=args)

    # Evaluate tactus host and config paths
    tactus_host = TactusHost().detect_tactus_host()
    with contextlib.suppress(AttributeError):
        if args.config_data_dir is not None:
            for config_data_dir in args.config_data_dir[::-1]:
                ConfigPaths.CONFIG_DATA_SEARCHPATHS.insert(0, config_data_dir)
    config = ParsedConfig.from_file(
        args.config_file,
        json_schema=ConfigParserDefaults.MAIN_CONFIG_JSON_SCHEMA,
        host=tactus_host,
    )

    with contextlib.suppress(KeyError):
        # Reset default loglevel if specified in the config
        logger.configure(
            handlers=LoggerHandlers(default_level=config["general.loglevel"])
        )

    return args.run_command(args=args, config=config)


def cli(argv=None):
    """Console-script entry point: run :func:`main` and exit with a matching code.

    ``run_command`` callbacks return ``True``/``None`` on success and ``False`` on
    failure (most return nothing meaningful, i.e. ``None``, and are therefore
    treated as successful). This is what is registered as the ``tactus`` console
    script (see ``pyproject.toml``'s ``[project.scripts]``), so that e.g. a failing
    ``tactus test -c <config>`` (real test FAILURE/MISSING/RUNNING) is visible to
    CI via a non-zero process exit code, while :func:`main` itself stays free of
    ``sys.exit()`` calls and safe to reuse in-process.

    Args:
        argv: Command line arguments. Defaults to ``sys.argv[1:]``.

    """
    sys.exit(1 if main(argv) is False else 0)


if __name__ == "__main__":
    cli()
