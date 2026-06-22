import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import UploadedFile


@dataclass(frozen=True)
class CalcLogDashboardResult:
    html: str
    source_files: list[str]


def build_calc_log_dashboard(
    uploaded_files: Sequence[UploadedFile],
) -> CalcLogDashboardResult:
    log_files = sorted(
        uploaded_files,
        key=lambda uploaded_file: uploaded_file.name.casefold(),
    )
    if not log_files:
        raise ValueError("Select at least one .log file.")

    # These imports are deliberately local: importing the UI does not need to
    # load the comparatively heavy spreadsheet dependencies.
    from .calc_log_scripts import generate_dashboard, parsecalclogdetail

    with TemporaryDirectory(prefix="agentops-calc-log-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        log_paths: list[Path] = []
        for uploaded_file in log_files:
            log_path = temporary_path / Path(uploaded_file.name).name
            with log_path.open("wb") as destination:
                for chunk in uploaded_file.chunks():
                    destination.write(chunk)
            log_paths.append(log_path)

        workbook_path = Path(temporary_directory) / "calculation_logs.xlsx"
        parsecalclogdetail.main(
            str(workbook_path),
            *(str(path) for path in log_paths),
        )
        data = generate_dashboard.build_data(str(workbook_path))

    template_path = Path(generate_dashboard.TEMPLATE_PATH)
    template = template_path.read_text(encoding="utf-8")
    serialized_data = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = template.replace(generate_dashboard.DATA_MARKER, serialized_data)
    return CalcLogDashboardResult(
        html=html,
        source_files=[Path(uploaded_file.name).name for uploaded_file in log_files],
    )
