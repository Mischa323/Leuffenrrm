"""Pydantic request/response models for the API."""
from __future__ import annotations

from pydantic import BaseModel


class WakeRequest(BaseModel):
    """Optional override; if omitted the device's stored MAC/broadcast are used."""
    mac: str | None = None
    broadcast_ip: str | None = None
    port: int = 9
    node_id: str | None = None


class PowerRequest(BaseModel):
    action: str  # reboot | shutdown | lock | logoff


class SubnetRequest(BaseModel):
    cidr: str
    broadcast: str | None = None


class GroupRequest(BaseModel):
    name: str


class MoveDeviceRequest(BaseModel):
    group_id: str | None = None


class MoveOrgRequest(BaseModel):
    org_id: str


class OrgRequest(BaseModel):
    name: str


class OrgUserRequest(BaseModel):
    email: str
    role: str = "admin"


class ShellRequest(BaseModel):
    cmd: str


class ScriptRequest(BaseModel):
    name: str
    content: str
    shell: str = "shell"          # shell | powershell
    description: str | None = None
    category: str = "Script"      # Monitoring | Installation | Maintenance | ...


class ScriptRunRequest(BaseModel):
    device_id: str
    timeout: float = 120


class ScheduleRequest(BaseModel):
    script_id: str
    name: str | None = None
    target_type: str = "all"          # device | group | all
    target_id: str | None = None
    trigger: str = "interval"         # interval | daily
    interval_minutes: int | None = None
    at_time: str | None = None        # 'HH:MM' for daily


class ScriptFileRequest(BaseModel):
    name: str
    content_b64: str


class MonitorRequest(BaseModel):
    name: str
    monitor_script_id: str
    remediation_script_id: str | None = None
    target_type: str = "all"
    target_id: str | None = None
    trigger: str = "interval"
    interval_minutes: int | None = 15
    at_time: str | None = None
    variables: dict | None = None     # name -> value, passed as env to both scripts
    notify_email: bool = True
    severity: str = "warning"         # info | warning | critical


class MonitorRuleRequest(BaseModel):
    """Add a template-backed metric-threshold monitor (e.g. "disk >= 90% for 5 min")."""
    template_id: str
    name: str | None = None
    threshold: float | None = None          # defaults to the template's suggestion
    duration_minutes: float | None = None   # defaults to the template's suggestion
    target_type: str = "all"                # device | group | all
    target_id: str | None = None
    notify_email: bool = True
    severity: str | None = None             # defaults to the template's suggestion
