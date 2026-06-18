from django import forms

from tools import MCP_CHOICES

from .models import Agent, AgentSchedule


class AgentForm(forms.ModelForm):
    mcp_servers = forms.MultipleChoiceField(
        choices=MCP_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple(),
    )

    class Meta:
        model = Agent
        fields = ["name", "working_directory", "system_prompt", "mcp_servers"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "input input-bordered w-full",
                "placeholder": "my-agent",
                "autofocus": True,
            }),
            "working_directory": forms.TextInput(attrs={
                "class": "input input-bordered w-full",
                "placeholder": "/home/user/project",
            }),
            "system_prompt": forms.Textarea(attrs={
                "class": "textarea textarea-bordered w-full h-28",
                "placeholder": "You are a helpful agent...",
            }),
        }


class AgentScheduleForm(forms.ModelForm):
    class Meta:
        model = AgentSchedule
        fields = ["agent", "prompt", "schedule_type", "interval_minutes", "cron_expression", "is_active"]
        widgets = {
            "agent": forms.Select(attrs={"class": "select select-bordered w-full"}),
            "prompt": forms.Textarea(attrs={
                "class": "textarea textarea-bordered w-full h-24",
                "placeholder": "What should the agent do on each run?",
            }),
            "schedule_type": forms.Select(attrs={
                "class": "select select-bordered w-full",
                "id": "id_schedule_type",
                "onchange": "toggleScheduleFields(this.value)",
            }),
            "interval_minutes": forms.NumberInput(attrs={
                "class": "input input-bordered w-full",
                "placeholder": "e.g. 60 (= every hour)",
                "min": "1",
            }),
            "cron_expression": forms.TextInput(attrs={
                "class": "input input-bordered w-full font-mono",
                "placeholder": "0 9 * * 1  (= Mon 9am)",
            }),
            "is_active": forms.CheckboxInput(attrs={"class": "checkbox checkbox-sm"}),
        }

    def clean(self) -> dict:
        cleaned = super().clean()
        stype = cleaned.get("schedule_type")
        if stype == AgentSchedule.INTERVAL and not cleaned.get("interval_minutes"):
            self.add_error("interval_minutes", "Required when using interval scheduling.")
        if stype == AgentSchedule.CRON and not cleaned.get("cron_expression", "").strip():
            self.add_error("cron_expression", "Required when using cron scheduling.")
        return cleaned
