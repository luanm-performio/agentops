from collections.abc import Callable
from dataclasses import dataclass

CommandParams = dict[str, object]


@dataclass(frozen=True)
class CommandDefinition:
    key: str
    label: str
    description: str
    default_params: CommandParams
    handler: Callable[[CommandParams], str]
    schedulable: bool = True


def required_str(params: CommandParams, key: str) -> str:
    value = str(params.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required.")
    return value


def positive_int(
    params: CommandParams,
    key: str,
    *,
    maximum: int,
) -> int:
    try:
        value = int(params.get(key, 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a whole number.") from exc
    if value <= 0:
        raise ValueError(f"{key} must be greater than zero.")
    if value > maximum:
        raise ValueError(f"{key} must not exceed {maximum} seconds.")
    return value
