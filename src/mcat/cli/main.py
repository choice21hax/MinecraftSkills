"""`mcat` command-line entry point.

Exit codes: 0 success, 1 failed, 2 usage error, 3 partial (ran, not fully verified), 4 blocked.
Pass --json (anywhere before the subcommand or after it) for the stable JSON envelope.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable

from mcat import __version__
from mcat.cli import commands_core as core
from mcat.models import LOADERS
from mcat.reporting import Result
from mcat.security.credentials import PROVIDERS
from mcat.security.redact import redact

Handler = Callable[[argparse.Namespace], Result]


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="emit the JSON result envelope")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcat",
        description="Minecraft Agent Toolkit: deterministic modpack, migration, mod-dev and debug engine.",
        epilog="Exit codes: 0 success, 1 failed, 2 usage, 3 partial/unverified, 4 blocked.",
    )
    parser.add_argument("--version", action="version", version=f"mcat {__version__}")
    parser.add_argument("--json", action="store_true", help="emit the JSON result envelope")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr (redacted)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def cmd(parent: argparse._SubParsersAction, name: str, handler: Handler, help_: str) -> argparse.ArgumentParser:
        p = parent.add_parser(name, help=help_, description=help_)
        _add_json(p)
        p.set_defaults(handler=handler)
        return p

    p = cmd(sub, "doctor", core.doctor, "diagnose Python, Java, Gradle, packwiz, keyring, providers, skills, sandbox")
    p.add_argument("--offline", action="store_true", help="skip Modrinth/CurseForge connectivity checks")
    p.add_argument("--project", help="project directory for project-scope skill checks (default: cwd)")

    p = cmd(sub, "inspect", core.inspect, "read-only analysis of a pack archive or directory (no downloads)")
    p.add_argument("path", help=".mrpack, CurseForge .zip, packwiz directory, or instance directory")

    p = cmd(sub, "search", core.search, "search Modrinth/CurseForge for mods or modpacks")
    p.add_argument("query")
    p.add_argument("--provider", choices=["modrinth", "curseforge", "all"], default="modrinth")
    p.add_argument("--minecraft", help="game version filter, e.g. 26.2 (a string, never a number)")
    p.add_argument("--loader", choices=LOADERS)
    p.add_argument("--type", choices=["mod", "modpack"], default="mod")
    p.add_argument("--limit", type=int, default=10)

    auth = sub.add_parser("auth", help="manage provider credentials in the OS keyring (never displayed)")
    asub = auth.add_subparsers(dest="auth_command", required=True, metavar="ACTION")
    cmd(asub, "setup", core.auth_setup, "interactive guided setup for all providers")
    p = cmd(asub, "set", core.auth_set, "store a credential (hidden prompt)")
    p.add_argument("provider", choices=PROVIDERS)
    cmd(asub, "status", core.auth_status, "show which credentials are configured (never the values)")
    p = cmd(asub, "test", core.auth_test, "verify stored credentials against the provider API")
    p.add_argument("provider", nargs="?", choices=PROVIDERS)
    p = cmd(asub, "remove", core.auth_remove, "delete a stored credential")
    p.add_argument("provider", choices=PROVIDERS)

    skills = sub.add_parser("skills", help="install the minecraft-* agent skills for Claude Code / Codex")
    ssub = skills.add_subparsers(dest="skills_command", required=True, metavar="ACTION")
    for name, handler, help_ in (
        ("install", core.skills_install, "install (symlink or managed copy) into agent discovery paths"),
        ("status", core.skills_status, "report installation and discovery state"),
        ("uninstall", core.skills_uninstall, "remove MCAT-managed installs (never unrelated skills)"),
        ("validate", core.skills_validate, "validate canonical SKILL.md files against the Agent Skills spec"),
    ):
        p = cmd(ssub, name, handler, help_)
        if name == "validate":
            continue
        p.add_argument("--agents", type=core._agents, default=["claude", "codex"], help="claude,codex")
        p.add_argument(
            "--scope",
            choices=["project", "user", "all"] if name == "status" else ["project", "user"],
            default="all" if name == "status" else "project",
        )
        p.add_argument("--project", default=".", help="project root for project-scope installs")
        if name == "install":
            p.add_argument("--copy", action="store_true", help="managed copies instead of symlinks")
            p.add_argument(
                "--force", action="store_true", help="replace conflicting unrelated skills (explicit approval)"
            )

    from mcat.cli import commands_migrate, commands_mod, commands_pack, commands_test

    commands_pack.register(sub, cmd)
    commands_migrate.register(sub, cmd)
    commands_mod.register(sub, cmd)
    commands_test.register(sub, cmd)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        result: Result = args.handler(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:  # last-resort: report as a failed result instead of a traceback with secrets
        logging.getLogger("mcat").debug("unhandled error", exc_info=True)
        result = Result(getattr(args, "command", "mcat"))
        result.fail(f"internal error: {type(e).__name__}: {redact(str(e))}")
    if getattr(args, "json", False):
        print(result.to_json())
    else:
        print(result.to_text())
        if args.verbose and result.data:
            import json

            print(redact(json.dumps(result.to_dict()["data"], indent=2, default=str)))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
