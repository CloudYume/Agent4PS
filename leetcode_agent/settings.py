from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class ApiSettings(BaseModel):
    endpoint: HttpUrl
    model: str = Field(min_length=1)
    vision_model: str | None = Field(default=None, min_length=1)
    fallback_model: str | None = Field(default=None, min_length=1)
    key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: int = Field(ge=5, le=300)
    first_answer_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    primary_completion_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    stream: bool = True
    thinking: Literal["enabled", "disabled"] | None = None

    @model_validator(mode="after")
    def valid_fallback(self) -> "ApiSettings":
        if (self.fallback_model is None) != (self.first_answer_timeout_seconds is None):
            raise ValueError("api.fallback_model and api.first_answer_timeout_seconds must be configured together")
        if self.fallback_model is not None:
            if self.fallback_model == self.model:
                raise ValueError("api.fallback_model must differ from api.model")
            if self.first_answer_timeout_seconds >= self.timeout_seconds:
                raise ValueError("api.first_answer_timeout_seconds must be less than api.timeout_seconds")
        if self.primary_completion_timeout_seconds is not None:
            if self.fallback_model is None:
                raise ValueError("api.primary_completion_timeout_seconds requires api.fallback_model")
            if self.primary_completion_timeout_seconds >= self.timeout_seconds:
                raise ValueError("api.primary_completion_timeout_seconds must be less than api.timeout_seconds")
            if self.primary_completion_timeout_seconds <= self.first_answer_timeout_seconds:
                raise ValueError("api.primary_completion_timeout_seconds must exceed api.first_answer_timeout_seconds")
        return self


class LeetCodeSettings(BaseModel):
    base_url: HttpUrl
    language: str
    start_id: int = Field(ge=1)

    @field_validator("language")
    @classmethod
    def require_python3(cls, value: str) -> str:
        if value != "python3":
            raise ValueError("this version supports only python3")
        return value


class WorkflowSettings(BaseModel):
    max_repairs: int = Field(ge=0, le=10)
    max_model_calls: int = Field(ge=2, le=50)
    auto_submit: bool
    review_mode: Literal["always", "on_repair"] = "on_repair"


class BrowserSettings(BaseModel):
    mode: Literal["edge_extension"]
    host: Literal["127.0.0.1"]
    port: int = Field(ge=1024, le=65535)
    poll_interval_seconds: float = Field(ge=0.25, le=10)
    submit_trigger: Literal["button", "shortcut"] = "button"
    navigation_trigger: Literal["url", "shortcut"] = "url"


class SearchSettings(BaseModel):
    provider: Literal["bing_html"]
    fallback_after_failures: int = Field(ge=1)
    max_pages: int = Field(ge=0, le=10)


class OutputSettings(BaseModel):
    save_artifacts: bool = True


class Settings(BaseModel):
    api: ApiSettings
    leetcode: LeetCodeSettings
    browser: BrowserSettings
    workflow: WorkflowSettings
    search: SearchSettings
    output: OutputSettings = Field(default_factory=OutputSettings)


def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must contain a mapping")
    api = raw.get("api")
    if isinstance(api, dict) and "key_env" in api:
        name = api["key_env"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(
                "api.key_env must be an environment variable name such as MODEL_API_KEY; "
                "set the API key in that variable, not in config.yaml"
            )
    return Settings.model_validate(raw)
