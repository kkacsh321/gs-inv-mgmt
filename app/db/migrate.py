import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config


def alembic_config() -> Config:
    project_root = Path(__file__).resolve().parents[2]
    return Config(str(project_root / "alembic.ini"))


def upgrade(revision: str = "head") -> None:
    command.upgrade(alembic_config(), revision)


def downgrade(revision: str) -> None:
    command.downgrade(alembic_config(), revision)


def current(verbose: bool = False) -> None:
    command.current(alembic_config(), verbose=verbose)


def history(verbose: bool = False) -> None:
    command.history(alembic_config(), verbose=verbose)


def revision(message: str, autogenerate: bool = True) -> None:
    command.revision(alembic_config(), message=message, autogenerate=autogenerate)


def main() -> None:
    parser = argparse.ArgumentParser(description="GoldenStackers DB migration utility")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("upgrade")

    downgrade_parser = sub.add_parser("downgrade")
    downgrade_parser.add_argument("revision")

    current_parser = sub.add_parser("current")
    current_parser.add_argument("--verbose", action="store_true")

    history_parser = sub.add_parser("history")
    history_parser.add_argument("--verbose", action="store_true")

    revision_parser = sub.add_parser("revision")
    revision_parser.add_argument("-m", "--message", required=True)
    revision_parser.add_argument("--no-autogenerate", action="store_true")

    args = parser.parse_args()

    if args.command == "upgrade":
        upgrade()
    elif args.command == "downgrade":
        downgrade(args.revision)
    elif args.command == "current":
        current(verbose=args.verbose)
    elif args.command == "history":
        history(verbose=args.verbose)
    elif args.command == "revision":
        revision(message=args.message, autogenerate=not args.no_autogenerate)


if __name__ == "__main__":
    main()
