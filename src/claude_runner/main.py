"""Process entry point for the ``claude-runner`` executable."""

import sys

from claude_runner.cli.commands import app


def main() -> None:
    """Run the Typer application."""
    _force_utf8_output()
    app()


def _force_utf8_output() -> None:
    """Print UTF-8 regardless of the console code page.

    Windows consoles still default to a legacy code page, which mangles both
    agent output and session titles that are not plain ASCII.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    main()
