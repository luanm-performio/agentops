from .commands.backup_download_import import COMMAND as BACKUP_DOWNLOAD_IMPORT
from .commands.base import CommandDefinition, CommandParams
from .commands.lock_monitor import COMMAND as LOCK_MONITOR

COMMANDS: dict[str, CommandDefinition] = {
    command.key: command
    for command in (
        BACKUP_DOWNLOAD_IMPORT,
        LOCK_MONITOR,
    )
}


def command_choices() -> list[tuple[str, str]]:
    return [(command.key, command.label) for command in COMMANDS.values()]


def get_command(key: str) -> CommandDefinition:
    try:
        return COMMANDS[key]
    except KeyError as exc:
        raise ValueError(f"Unknown command: {key}") from exc


def run_registered_command(
    command_key: str,
    params: CommandParams,
    *,
    command_run_id: int | None = None,
) -> str:
    command = get_command(command_key)
    merged_params = {**command.default_params, **params}
    merged_params["_command_run_id"] = command_run_id
    return command.handler(merged_params)
