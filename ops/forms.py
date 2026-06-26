import json
from pathlib import Path
from typing import cast

from django import forms
from django.core.files.uploadedfile import UploadedFile

from .command_registry import command_choices, get_command
from .models import CalcLogDashboard, CommandSchedule


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(
        self,
        data: object,
        initial: object = None,
    ) -> list[UploadedFile]:
        if not data:
            super().clean(data, initial)
            return []
        values = data if isinstance(data, (list, tuple)) else [data]
        return [cast(UploadedFile, super().clean(value, initial)) for value in values]


class CalcLogDashboardForm(forms.ModelForm):
    log_files = MultipleFileField(
        label="Calculation logs",
        error_messages={"required": "Select at least one .log file."},
        widget=MultipleFileInput(
            attrs={
                "class": "file-input file-input-bordered w-full",
                "accept": ".log,text/plain",
            }
        ),
    )

    class Meta:
        model = CalcLogDashboard
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "input input-bordered w-full",
                    "placeholder": "June calculation comparison",
                    "autocomplete": "off",
                    "autofocus": True,
                }
            ),
        }

    def clean_log_files(self) -> list[UploadedFile]:
        files = cast(list[UploadedFile], self.cleaned_data["log_files"])
        invalid_names = [
            uploaded_file.name
            for uploaded_file in files
            if Path(uploaded_file.name).suffix.lower() != ".log"
        ]
        if invalid_names:
            raise forms.ValidationError(
                f"Only .log files are supported: {', '.join(invalid_names)}"
            )

        safe_names = [Path(uploaded_file.name).name for uploaded_file in files]
        normalized_names = [name.casefold() for name in safe_names]
        if len(normalized_names) != len(set(normalized_names)):
            raise forms.ValidationError(
                "Each selected log file must have a unique name."
            )

        return sorted(files, key=lambda uploaded_file: uploaded_file.name.casefold())


class LockMonitorRunForm(forms.Form):
    tenant_host = forms.CharField(
        label="Tenant host",
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": "input input-bordered w-full",
                "placeholder": "tenant.performio.com",
                "autocomplete": "off",
                "autofocus": True,
            }
        ),
    )

    def clean_tenant_host(self) -> str:
        tenant_host = self.cleaned_data["tenant_host"].strip()
        if not tenant_host:
            raise forms.ValidationError("Enter a tenant host.")
        return tenant_host


COMMAND_PARAMETER_FIELD_NAMES = (
    "tenant_host",
    "remote_host",
    "ssh_user",
    "remote_user",
    "database_server_url",
    "schema_name",
    "backup_command",
    "remote_backup_path",
    "backup_process_pattern",
    "backup_poll_interval_seconds",
    "backup_timeout_seconds",
    "local_backup_path",
    "local_database",
    "overwrite_local",
    "dry_run",
    "alert_threshold_seconds",
    "alert_agent_id",
    "alert_agent_name",
    "alert_recipient",
)

REQUIRED_COMMAND_FIELDS = {
    "backup_download_import": {
        "remote_host": "Remote host is required.",
        "remote_user": "Remote user is required.",
        "database_server_url": "Database server is required.",
        "schema_name": "Schema is required.",
        "backup_command": "Backup command is required.",
        "remote_backup_path": "Remote backup path is required.",
        "backup_poll_interval_seconds": "Backup poll interval is required.",
        "backup_timeout_seconds": "Backup timeout is required.",
        "local_backup_path": "Local backup path is required.",
        "local_database": "Local database is required.",
    },
    "lock_monitor": {"tenant_host": "Tenant host is required."},
}


def _command_text_input(placeholder: str) -> forms.TextInput:
    return forms.TextInput(
        attrs={
            "class": "input input-bordered w-full font-mono",
            "placeholder": placeholder,
        }
    )


def _command_textarea(
    *, placeholder: str, height_class: str = "h-24"
) -> forms.Textarea:
    return forms.Textarea(
        attrs={
            "class": f"textarea textarea-bordered w-full {height_class} font-mono",
            "placeholder": placeholder,
            "spellcheck": "false",
        }
    )


def _command_number_input(
    *,
    placeholder: str,
    maximum: int,
    minimum: int = 1,
) -> forms.NumberInput:
    return forms.NumberInput(
        attrs={
            "class": "input input-bordered w-full font-mono",
            "placeholder": placeholder,
            "min": str(minimum),
            "max": str(maximum),
        }
    )


def _command_checkbox() -> forms.CheckboxInput:
    return forms.CheckboxInput(attrs={"class": "checkbox checkbox-sm"})


def _set_command_parameter_initials(
    form: forms.Form,
    params: dict[str, object],
) -> None:
    for field_name in COMMAND_PARAMETER_FIELD_NAMES:
        if field_name in form.fields:
            form.fields[field_name].initial = params.get(field_name, "")


class CommandRunForm(forms.Form):
    command_key = forms.ChoiceField(
        choices=command_choices,
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    tenant_host = forms.CharField(
        label="Tenant host",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input input-bordered w-full font-mono",
                "placeholder": "tenant.performio.com",
            }
        ),
    )
    remote_host = forms.CharField(
        label="Remote host",
        required=False,
        widget=_command_text_input("devbox.performio.co"),
    )
    ssh_user = forms.CharField(
        label="SSH user",
        required=False,
        widget=_command_text_input("optional jump/login user"),
    )
    remote_user = forms.CharField(
        label="Remote command user",
        required=False,
        widget=_command_text_input("staff"),
    )
    database_server_url = forms.CharField(
        label="Database server",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input input-bordered w-full font-mono",
                "placeholder": "mysql.example.internal",
            }
        ),
    )
    schema_name = forms.CharField(
        label="Schema",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input input-bordered w-full font-mono",
                "placeholder": "tenant_schema",
            }
        ),
    )
    backup_command = forms.CharField(
        label="Backup command",
        required=False,
        widget=_command_textarea(
            placeholder=(
                "backup-db --host {database_server_url} "
                "--schema {schema_name} --output {remote_backup_path}"
            )
        ),
    )
    remote_backup_path = forms.CharField(
        label="Remote backup path",
        required=False,
        widget=_command_text_input("/tmp/tenant_schema.sql.gz"),
    )
    backup_process_pattern = forms.CharField(
        label="Backup process pattern",
        required=False,
        widget=_command_text_input("defaults to the remote backup filename"),
    )
    backup_poll_interval_seconds = forms.IntegerField(
        label="Backup poll interval (seconds)",
        min_value=1,
        max_value=300,
        required=False,
        widget=_command_number_input(placeholder="15", maximum=300),
    )
    backup_timeout_seconds = forms.IntegerField(
        label="Backup timeout (seconds)",
        min_value=1,
        max_value=7200,
        required=False,
        widget=_command_number_input(placeholder="7200", maximum=7200),
    )
    local_backup_path = forms.CharField(
        label="Local backup path",
        required=False,
        widget=_command_text_input("/tmp/performio-backup.sql.gz"),
    )
    local_database = forms.CharField(
        label="Local database",
        required=False,
        widget=_command_text_input("performio_local"),
    )
    overwrite_local = forms.BooleanField(
        label="Overwrite local database",
        required=False,
        widget=_command_checkbox(),
    )
    dry_run = forms.BooleanField(
        label="Dry run",
        required=False,
        widget=_command_checkbox(),
    )
    alert_threshold_seconds = forms.IntegerField(
        label="Alert threshold (seconds)",
        min_value=0,
        max_value=86400,
        required=False,
        widget=_command_number_input(
            placeholder="7200",
            maximum=86400,
            minimum=0,
        ),
    )
    alert_agent_id = forms.CharField(
        label="Alert agent ID",
        required=False,
        widget=_command_text_input("optional numeric agent id"),
    )
    alert_agent_name = forms.CharField(
        label="Alert agent name",
        required=False,
        widget=_command_text_input("Slack Alert Agent"),
    )
    alert_recipient = forms.CharField(
        label="Alert recipient",
        required=False,
        widget=_command_text_input("me"),
    )
    params_text = forms.CharField(
        label="Parameters",
        widget=forms.Textarea(
            attrs={
                "class": "textarea textarea-bordered w-full h-40 font-mono",
                "spellcheck": "false",
            }
        ),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        command_key = (
            self.data.get("command_key")
            or self.initial.get("command_key")
            or command_choices()[0][0]
        )
        command = get_command(command_key)
        self.fields["params_text"].initial = json.dumps(
            command.default_params, indent=2
        )
        self.fields["tenant_host"].initial = command.default_params.get(
            "tenant_host", ""
        )
        self.fields["database_server_url"].initial = command.default_params.get(
            "database_server_url", ""
        )
        self.fields["schema_name"].initial = command.default_params.get(
            "schema_name", ""
        )
        _set_command_parameter_initials(self, command.default_params)

    def clean_params_text(self) -> dict:
        raw = self.cleaned_data["params_text"]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise forms.ValidationError("Parameters must be a JSON object.")
        return parsed

    def clean(self) -> dict:
        cleaned = super().clean()
        return _merge_command_explicit_params(self, cleaned)

    @property
    def params(self) -> dict:
        return self.cleaned_data["params_text"]


class CommandScheduleForm(forms.ModelForm):
    command_key = forms.ChoiceField(
        choices=command_choices,
        widget=forms.Select(attrs={"class": "select select-bordered w-full"}),
    )
    tenant_host = forms.CharField(
        label="Tenant host",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input input-bordered w-full font-mono",
                "placeholder": "tenant.performio.com",
            }
        ),
    )
    remote_host = forms.CharField(
        label="Remote host",
        required=False,
        widget=_command_text_input("devbox.performio.co"),
    )
    ssh_user = forms.CharField(
        label="SSH user",
        required=False,
        widget=_command_text_input("optional jump/login user"),
    )
    remote_user = forms.CharField(
        label="Remote command user",
        required=False,
        widget=_command_text_input("staff"),
    )
    database_server_url = forms.CharField(
        label="Database server",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input input-bordered w-full font-mono",
                "placeholder": "mysql.example.internal",
            }
        ),
    )
    schema_name = forms.CharField(
        label="Schema",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input input-bordered w-full font-mono",
                "placeholder": "tenant_schema",
            }
        ),
    )
    backup_command = forms.CharField(
        label="Backup command",
        required=False,
        widget=_command_textarea(
            placeholder=(
                "backup-db --host {database_server_url} "
                "--schema {schema_name} --output {remote_backup_path}"
            )
        ),
    )
    remote_backup_path = forms.CharField(
        label="Remote backup path",
        required=False,
        widget=_command_text_input("/tmp/tenant_schema.sql.gz"),
    )
    backup_process_pattern = forms.CharField(
        label="Backup process pattern",
        required=False,
        widget=_command_text_input("defaults to the remote backup filename"),
    )
    backup_poll_interval_seconds = forms.IntegerField(
        label="Backup poll interval (seconds)",
        min_value=1,
        max_value=300,
        required=False,
        widget=_command_number_input(placeholder="15", maximum=300),
    )
    backup_timeout_seconds = forms.IntegerField(
        label="Backup timeout (seconds)",
        min_value=1,
        max_value=7200,
        required=False,
        widget=_command_number_input(placeholder="7200", maximum=7200),
    )
    local_backup_path = forms.CharField(
        label="Local backup path",
        required=False,
        widget=_command_text_input("/tmp/performio-backup.sql.gz"),
    )
    local_database = forms.CharField(
        label="Local database",
        required=False,
        widget=_command_text_input("performio_local"),
    )
    overwrite_local = forms.BooleanField(
        label="Overwrite local database",
        required=False,
        widget=_command_checkbox(),
    )
    dry_run = forms.BooleanField(
        label="Dry run",
        required=False,
        widget=_command_checkbox(),
    )
    alert_threshold_seconds = forms.IntegerField(
        label="Alert threshold (seconds)",
        min_value=0,
        max_value=86400,
        required=False,
        widget=_command_number_input(
            placeholder="7200",
            maximum=86400,
            minimum=0,
        ),
    )
    alert_agent_id = forms.CharField(
        label="Alert agent ID",
        required=False,
        widget=_command_text_input("optional numeric agent id"),
    )
    alert_agent_name = forms.CharField(
        label="Alert agent name",
        required=False,
        widget=_command_text_input("Slack Alert Agent"),
    )
    alert_recipient = forms.CharField(
        label="Alert recipient",
        required=False,
        widget=_command_text_input("me"),
    )
    params_text = forms.CharField(
        label="Parameters",
        widget=forms.Textarea(
            attrs={
                "class": "textarea textarea-bordered w-full h-36 font-mono",
                "spellcheck": "false",
            }
        ),
    )

    class Meta:
        model = CommandSchedule
        fields = [
            "command_key",
            "tenant_host",
            "database_server_url",
            "schema_name",
            "schedule_type",
            "interval_minutes",
            "cron_expression",
            "is_active",
        ]
        widgets = {
            "schedule_type": forms.Select(
                attrs={
                    "class": "select select-bordered w-full",
                    "id": "id_command_schedule_type",
                    "onchange": "toggleCommandScheduleFields(this.value)",
                }
            ),
            "interval_minutes": forms.NumberInput(
                attrs={
                    "class": "input input-bordered w-full",
                    "placeholder": "e.g. 60 (= every hour)",
                    "min": "1",
                }
            ),
            "cron_expression": forms.TextInput(
                attrs={
                    "class": "input input-bordered w-full font-mono",
                    "placeholder": "0 9 * * 1  (= Mon 9am)",
                }
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "checkbox checkbox-sm"}),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        command_key = (
            self.data.get("command_key")
            or self.initial.get("command_key")
            or (self.instance.command_key if self.instance and self.instance.pk else "")
            or command_choices()[0][0]
        )
        params = (
            self.instance.params
            if self.instance and self.instance.pk
            else get_command(command_key).default_params
        )
        self.fields["params_text"].initial = json.dumps(params, indent=2)
        self.fields["tenant_host"].initial = params.get("tenant_host", "")
        self.fields["database_server_url"].initial = params.get(
            "database_server_url", ""
        )
        self.fields["schema_name"].initial = params.get("schema_name", "")
        _set_command_parameter_initials(self, params)

    def clean_params_text(self) -> dict:
        raw = self.cleaned_data["params_text"]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise forms.ValidationError("Parameters must be a JSON object.")
        return parsed

    def clean(self) -> dict:
        cleaned = super().clean()
        cleaned = _merge_command_explicit_params(self, cleaned)
        stype = cleaned.get("schedule_type")
        if stype == CommandSchedule.INTERVAL and not cleaned.get("interval_minutes"):
            self.add_error(
                "interval_minutes", "Required when using interval scheduling."
            )
        if (
            stype == CommandSchedule.CRON
            and not cleaned.get("cron_expression", "").strip()
        ):
            self.add_error("cron_expression", "Required when using cron scheduling.")
        return cleaned

    def save(self, commit: bool = True) -> CommandSchedule:
        schedule = super().save(commit=False)
        schedule.params = self.cleaned_data["params_text"]
        if commit:
            schedule.save()
        return schedule


def _merge_command_explicit_params(form: forms.Form, cleaned: dict) -> dict:
    params = cleaned.get("params_text")
    if not isinstance(params, dict):
        return cleaned

    command_key = str(cleaned.get("command_key", ""))
    command_params = get_command(command_key).default_params
    required_fields = REQUIRED_COMMAND_FIELDS.get(command_key, {})
    explicit_params: dict[str, object] = {}

    for field_name in COMMAND_PARAMETER_FIELD_NAMES:
        if field_name not in command_params:
            continue

        if field_name not in cleaned:
            continue
        if form.is_bound and field_name not in form.data:
            continue

        value = cleaned.get(field_name)
        if isinstance(value, str):
            value = value.strip()
        explicit_params[field_name] = value
        if value in {"", None} and field_name in required_fields:
            form.add_error(field_name, required_fields[field_name])

    cleaned["params_text"] = {**params, **explicit_params}
    return cleaned
