from django.db import models


class CalcLogDashboard(models.Model):
    name = models.CharField(max_length=150, unique=True)
    source_folder = models.TextField()
    source_files = models.JSONField(default=list)
    html_content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class CommandSchedule(models.Model):
    INTERVAL = "interval"
    CRON = "cron"
    SCHEDULE_TYPES = [(INTERVAL, "Interval"), (CRON, "Cron")]

    command_key = models.CharField(max_length=100)
    params = models.JSONField(default=dict, blank=True)
    schedule_type = models.CharField(
        max_length=10, choices=SCHEDULE_TYPES, default=INTERVAL
    )
    interval_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Run every N minutes",
    )
    cron_expression = models.CharField(
        max_length=100,
        blank=True,
        help_text="Standard 5-field cron expression, e.g. 0 9 * * 1",
    )
    is_active = models.BooleanField(default=True)
    last_run = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.command_key} - {self.schedule_display}"

    @property
    def schedule_display(self) -> str:
        if self.schedule_type == self.INTERVAL and self.interval_minutes:
            mins = self.interval_minutes
            if mins % (60 * 24) == 0:
                return f"every {mins // (60 * 24)} day(s)"
            if mins % 60 == 0:
                return f"every {mins // 60} hour(s)"
            return f"every {mins} min"
        if self.schedule_type == self.CRON:
            return self.cron_expression
        return "-"

    def q_schedule_name(self) -> str:
        return f"command-schedule-{self.pk}"


class CommandRun(models.Model):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STATUS_CHOICES = [
        (QUEUED, "Queued"),
        (RUNNING, "Running"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
    ]

    schedule = models.ForeignKey(
        CommandSchedule,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="runs",
    )
    command_key = models.CharField(max_length=100)
    params = models.JSONField(default=dict, blank=True)
    output = models.TextField(blank=True)
    error = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=QUEUED)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.command_key} run #{self.pk}"

    @property
    def duration_seconds(self) -> float | None:
        if self.completed_at and self.started_at:
            return round((self.completed_at - self.started_at).total_seconds(), 1)
        return None


class LockMonitorCapture(models.Model):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STATUS_CHOICES = [
        (RUNNING, "Running"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
    ]

    command_run = models.ForeignKey(
        CommandRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="lock_monitor_captures",
    )
    tenant_host = models.CharField(max_length=500, db_index=True)
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default=RUNNING,
    )
    data_source_count = models.PositiveIntegerField(default=0)
    process_count = models.PositiveIntegerField(default=0)
    lock_count = models.PositiveIntegerField(default=0)
    warning = models.TextField(blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.tenant_host} capture #{self.pk}"


class LockMonitoringRecord(models.Model):
    PROCESS = "process"
    LOCK_WAIT = "lock_wait"
    RESOURCE = "resource"
    RECORD_TYPES = [
        (PROCESS, "Process"),
        (LOCK_WAIT, "Lock wait"),
        (RESOURCE, "Resource snapshot"),
    ]

    capture = models.ForeignKey(
        LockMonitorCapture,
        on_delete=models.CASCADE,
        related_name="records",
    )
    record_type = models.CharField(max_length=20, choices=RECORD_TYPES)
    region = models.CharField(max_length=100, blank=True)
    database_server_url = models.CharField(max_length=500)
    schema_name = models.CharField(max_length=255, db_index=True)
    process_id = models.BigIntegerField(null=True, blank=True)
    waiting_process_id = models.BigIntegerField(null=True, blank=True)
    blocking_process_id = models.BigIntegerField(null=True, blank=True)
    user = models.CharField(max_length=255, blank=True)
    host = models.CharField(max_length=500, blank=True)
    database_name = models.CharField(max_length=255, blank=True)
    command = models.CharField(max_length=100, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    state = models.CharField(max_length=500, blank=True)
    query_text = models.TextField(blank=True)
    blocking_query_text = models.TextField(blank=True)
    is_tenant_schema = models.BooleanField(default=False)
    is_long_running = models.BooleanField(default=False)
    is_lock_waiting = models.BooleanField(default=False)
    is_lock_blocking = models.BooleanField(default=False)
    explain_json = models.JSONField(null=True, blank=True)
    explanation = models.TextField(blank=True)
    explain_error = models.TextField(blank=True)
    explain_skipped_reason = models.TextField(blank=True)
    explain_generated_at = models.DateTimeField(null=True, blank=True)
    statement_metrics = models.JSONField(default=dict, blank=True)
    server_metrics = models.JSONField(default=dict, blank=True)
    contention_signals = models.JSONField(default=list, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "lock_monitoring_table"
        ordering = ["-captured_at", "-pk"]
        indexes = [
            models.Index(fields=["record_type", "captured_at"]),
            models.Index(fields=["database_server_url", "schema_name"]),
        ]

    def __str__(self) -> str:
        identifier = self.process_id or self.waiting_process_id or self.pk
        return f"{self.record_type} {identifier}"
