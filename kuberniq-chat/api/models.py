"""Pydantic request / response models for the Kuberniq Chat API."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


# Matches kuberniq-server TokenResponse record (camelCase from C# serializer)
class LoginResponse(BaseModel):
    accessToken: str
    refreshToken: str
    expiresIn: int


class ChatMessage(BaseModel):
    role: str          # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    model: Optional[str] = None   # overrides server default if provided
    yaml_content: Optional[str] = None  # uploaded YAML for manifest analysis


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    allowed_namespaces: list[str] = []


class UpdateNamespacesRequest(BaseModel):
    allowed_namespaces: list[str]


class UpdateUserRequest(BaseModel):
    role: str | None = None
    allowed_namespaces: list[str] | None = None
