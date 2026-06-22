import json

from django.core.management.base import BaseCommand, CommandError

from ops.command_registry import run_registered_command


class Command(BaseCommand):
    help = "Run a registered managed command with JSON parameters."

    def add_arguments(self, parser) -> None:
        parser.add_argument("command_key")
        parser.add_argument(
            "--params", default="{}", help="JSON object of command parameters."
        )

    def handle(self, *args, **options) -> None:
        try:
            params = json.loads(options["params"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON params: {exc}") from exc
        if not isinstance(params, dict):
            raise CommandError("Params must be a JSON object.")

        output = run_registered_command(options["command_key"], params)
        self.stdout.write(output)
