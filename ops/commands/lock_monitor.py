from typing import cast

from .base import CommandDefinition, CommandParams, positive_int, required_str


def run(params: CommandParams) -> str:
    tenant_host = required_str(params, "tenant_host")
    long_running_seconds = positive_int(
        params,
        "long_running_seconds",
        maximum=300,
    )

    from ..lock_monitor import run_lock_monitor  # noqa: PLC0415

    capture = run_lock_monitor(
        tenant_host,
        long_running_seconds=long_running_seconds,
        command_run_id=cast(int | None, params.get("_command_run_id")),
    )
    return (
        f"Lock monitor capture #{capture.pk} completed for {tenant_host}: "
        f"{capture.data_source_count} data source(s), "
        f"{capture.process_count} process(es), {capture.lock_count} lock wait(s)."
    )


COMMAND = CommandDefinition(
    key="lock_monitor",
    label="Lock Monitor",
    description=(
        "Resolve a tenant host, capture full running queries and InnoDB lock "
        "waits, and save the snapshot for review."
    ),
    default_params={
        "tenant_host": "",
        "long_running_seconds": 300,
    },
    handler=run,
)
