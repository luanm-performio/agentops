from django.db import models


class Agent(models.Model):
    name = models.CharField(max_length=100)
    working_directory = models.CharField(max_length=500)
    system_prompt = models.TextField(blank=True)
    mcp_servers = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class AgentSchedule(models.Model):
    INTERVAL = "interval"
    CRON = "cron"
    SCHEDULE_TYPES = [(INTERVAL, "Interval"), (CRON, "Cron")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="schedules")
    prompt = models.TextField()
    schedule_type = models.CharField(max_length=10, choices=SCHEDULE_TYPES, default=INTERVAL)
    interval_minutes = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Run every N minutes",
    )
    cron_expression = models.CharField(
        max_length=100, blank=True,
        help_text="Standard 5-field cron expression, e.g. 0 9 * * 1",
    )
    is_active = models.BooleanField(default=True)
    last_run = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.agent.name} — {self.schedule_display}"

    @property
    def schedule_display(self) -> str:
        if self.schedule_type == self.INTERVAL and self.interval_minutes:
            mins = self.interval_minutes
            if mins % (60 * 24) == 0:
                return f"every {mins // (60*24)} day(s)"
            if mins % 60 == 0:
                return f"every {mins // 60} hour(s)"
            return f"every {mins} min"
        if self.schedule_type == self.CRON:
            return self.cron_expression
        return "—"

    def q_schedule_name(self) -> str:
        return f"agent-schedule-{self.pk}"


class ChatSession(models.Model):
    agent = models.ForeignKey(
        Agent, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="chats",
    )
    title = models.CharField(max_length=200, blank=True)
    claude_session_id = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title or f"Chat #{self.pk}"


class ChatMessage(models.Model):
    USER = "user"
    ASSISTANT = "assistant"
    ROLE_CHOICES = [(USER, "User"), (ASSISTANT, "Assistant")]

    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:40]}"


class AgentRun(models.Model):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STATUS_CHOICES = [
        (RUNNING, "Running"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
    ]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="runs")
    schedule = models.ForeignKey(
        AgentSchedule, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="runs",
    )
    prompt = models.TextField()
    output = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.agent.name} run #{self.pk}"

    @property
    def duration_seconds(self) -> float | None:
        if self.completed_at and self.started_at:
            return round((self.completed_at - self.started_at).total_seconds(), 1)
        return None
