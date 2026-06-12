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


class OrgRequest(BaseModel):
    name: str


class OrgUserRequest(BaseModel):
    email: str
    role: str = "admin"


class ShellRequest(BaseModel):
    cmd: str
