import base64
import gzip
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .base import CommandDefinition, CommandParams, positive_int, required_str


def run(params: CommandParams) -> str:
    remote_host = required_str(params, "remote_host")
    remote_user = required_str(params, "remote_user")
    ssh_user = str(params.get("ssh_user", "")).strip()
    database_server_url = required_str(params, "database_server_url")
    schema_name = required_str(params, "schema_name")
    remote_backup_path = required_str(params, "remote_backup_path")
    backup_command = _format_backup_command_template(
        required_str(params, "backup_command"),
        database_server_url=database_server_url,
        schema_name=schema_name,
        remote_backup_path=remote_backup_path,
    )
    local_backup_path = required_str(params, "local_backup_path")
    local_database = required_str(params, "local_database")
    backup_process_pattern = _backup_process_pattern(params, remote_backup_path)
    backup_poll_interval_seconds = positive_int(
        params,
        "backup_poll_interval_seconds",
        maximum=300,
    )
    backup_timeout_seconds = positive_int(
        params,
        "backup_timeout_seconds",
        maximum=7200,
    )
    overwrite_local = bool(params.get("overwrite_local", False))
    dry_run = bool(params.get("dry_run", True))

    _validate_database_name(local_database)
    remote_target = f"{ssh_user}@{remote_host}" if ssh_user else remote_host
    commands = [
        [
            "ssh",
            remote_target,
            _remote_user_bash_command(remote_user, backup_command),
        ],
        ["scp", f"{remote_target}:{remote_backup_path}", local_backup_path],
    ]

    local_sql = (
        f"DROP DATABASE IF EXISTS `{local_database}`; "
        f"CREATE DATABASE `{local_database}`;"
        if overwrite_local
        else f"CREATE DATABASE IF NOT EXISTS `{local_database}`;"
    )
    commands.append(["mysql", "-e", local_sql])

    if dry_run:
        lines = [
            _format_command(commands[0]),
            (
                "wait for remote process matching "
                f"{backup_process_pattern!r} and non-empty file "
                f"{remote_backup_path!r} "
                f"(every {backup_poll_interval_seconds}s, "
                f"timeout {backup_timeout_seconds}s)"
            ),
            *(_format_command(command) for command in commands[1:]),
            f"mysql {local_database} < {local_backup_path}",
        ]
        return "Dry run. Commands that would run:\n" + "\n".join(lines)

    output_parts: list[str] = []
    _run_command(commands[0], output_parts)
    _wait_for_remote_backup(
        remote_target=remote_target,
        remote_user=remote_user,
        remote_backup_path=remote_backup_path,
        process_pattern=backup_process_pattern,
        poll_interval_seconds=backup_poll_interval_seconds,
        timeout_seconds=backup_timeout_seconds,
        output_parts=output_parts,
    )
    _run_command(commands[1], output_parts)
    _run_command(commands[2], output_parts)
    _import_backup(local_backup_path, local_database, output_parts)
    return "\n".join(output_parts)


def _backup_process_pattern(
    params: CommandParams,
    remote_backup_path: str,
) -> str:
    configured = str(params.get("backup_process_pattern", "")).strip()
    return configured or Path(remote_backup_path).name


def _wait_for_remote_backup(
    *,
    remote_target: str,
    remote_user: str,
    remote_backup_path: str,
    process_pattern: str,
    poll_interval_seconds: int,
    timeout_seconds: int,
    output_parts: list[str],
) -> None:
    status_command = _remote_backup_status_command(
        remote_backup_path=remote_backup_path,
        process_pattern=process_pattern,
    )
    ssh_command = [
        "ssh",
        remote_target,
        _remote_user_bash_command(remote_user, status_command),
    ]
    started_at = time.monotonic()
    output_parts.append(
        f"Waiting for remote backup process matching {process_pattern!r}."
    )

    while True:
        result = subprocess.run(
            ssh_command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Could not check remote backup status: "
                f"{result.stderr.strip() or f'exit code {result.returncode}'}"
            )

        status_lines = [line.strip() for line in result.stdout.splitlines() if line]
        status = status_lines[-1] if status_lines else ""
        if status == "READY":
            elapsed_seconds = round(time.monotonic() - started_at)
            output_parts.append(
                f"Remote backup finished after {elapsed_seconds}s: {remote_backup_path}"
            )
            return
        if status not in {"RUNNING", "WAITING"}:
            raise RuntimeError(f"Unexpected remote backup status: {status or 'empty'}")

        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds >= timeout_seconds:
            raise TimeoutError(
                "Remote backup did not finish within "
                f"{timeout_seconds}s. Last status: {status}."
            )
        time.sleep(poll_interval_seconds)


def _remote_backup_status_command(
    *,
    remote_backup_path: str,
    process_pattern: str,
) -> str:
    encoded_path = base64.b64encode(remote_backup_path.encode()).decode()
    encoded_pattern = base64.b64encode(process_pattern.encode()).decode()
    return (
        f"backup_path=$(printf %s {shlex.quote(encoded_path)} | base64 --decode); "
        f"pattern=$(printf %s {shlex.quote(encoded_pattern)} | base64 --decode); "
        "if ps -ef | grep -F -- \"$pattern\" | grep -v '[g]rep' >/dev/null; then "
        "printf RUNNING; "
        'elif test -s "$backup_path"; then printf READY; '
        "else printf WAITING; fi"
    )


def _remote_user_bash_command(remote_user: str, command: str) -> str:
    return shlex.join(["sudo", "-iu", remote_user, "bash", "-lc", command])


def _format_backup_command_template(
    template: str,
    *,
    database_server_url: str,
    schema_name: str,
    remote_backup_path: str,
) -> str:
    try:
        return template.format(
            database_server_url=database_server_url,
            schema_name=schema_name,
            remote_backup_path=remote_backup_path,
        )
    except KeyError as exc:
        placeholder = exc.args[0]
        raise ValueError(f"Unknown backup_command placeholder: {placeholder}") from exc


def _validate_database_name(database_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]+", database_name):
        raise ValueError(
            "local_database may only contain letters, numbers, and underscores."
        )
    lowered = database_name.lower()
    if any(token in lowered for token in ["prod", "production"]):
        raise ValueError("Refusing to import into a production-looking local_database.")


def _run_command(command: list[str], output_parts: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output_parts.append(f"$ {_format_command(command)}")
    if result.stdout:
        output_parts.append(result.stdout)
    if result.stderr:
        output_parts.append(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {_format_command(command)}"
        )


def _import_backup(
    local_backup_path: str,
    local_database: str,
    output_parts: list[str],
) -> None:
    path = Path(local_backup_path)
    output_parts.append(f"$ mysql {local_database} < {path}")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as backup:
        process = subprocess.Popen(["mysql", local_database], stdin=subprocess.PIPE)
        if process.stdin is None:
            raise RuntimeError("Could not open mysql stdin for import.")
        with process.stdin:
            shutil.copyfileobj(backup, process.stdin)
        return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"MySQL import failed with exit code {return_code}.")


def _format_command(command: list[str]) -> str:
    if "<" in command:
        return " ".join(command)
    return shlex.join(command)


COMMAND = CommandDefinition(
    key="backup_download_import",
    label="Backup, Download, Import",
    description=(
        "SSH to a devbox, run and wait for a backup as staff, download the "
        "backup, and import it into a local MySQL database."
    ),
    default_params={
        "remote_host": "devbox.performio.co",
        "ssh_user": "",
        "remote_user": "staff",
        "database_server_url": "",
        "schema_name": "",
        "backup_command": "",
        "remote_backup_path": "",
        "backup_process_pattern": "",
        "backup_poll_interval_seconds": 15,
        "backup_timeout_seconds": 7200,
        "local_backup_path": "/tmp/performio-backup.sql.gz",
        "local_database": "performio_local",
        "overwrite_local": False,
        "dry_run": True,
    },
    handler=run,
)
