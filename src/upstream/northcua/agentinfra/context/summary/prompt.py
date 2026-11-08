"""Stable, package-backed Jinja rendering for context-summary prompts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from jinja2 import BaseLoader, Environment, PackageLoader, StrictUndefined, Template
from nexau.core.messages import Message, Role, TextBlock

from northcua.agentkernel.context.domain.models import ContextSummaryDocumentV1
from northcua.agentkernel.context.domain.policy import ContextSummaryPolicy
from northcua.agentkernel.json_types import JsonObject

_PAYLOAD_FINGERPRINT_SENTINEL = "<northcua-summary-payload>"
_ERROR_FINGERPRINT_SENTINEL = "<northcua-summary-validation-error>"


@dataclass(frozen=True, slots=True)
class SummaryTemplateSet:
    """Trusted package template names for one versioned prompt profile."""

    system: str
    user: str
    repair_user: str


@dataclass(frozen=True, slots=True)
class SummarySystemTemplateContext:
    """Closed template context for the stable summary system instruction."""

    prompt_profile: str
    policy_profile: str
    profile_rules: tuple[str, ...]
    schema_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SummaryUserTemplateContext:
    """Closed template context containing already-canonicalized source data."""

    payload_json: str


@dataclass(frozen=True, slots=True)
class SummaryRepairTemplateContext:
    """Closed template context for one bounded correction request."""

    validation_error: str
    maximum_canonical_characters: int
    maximum_json_characters: int


_BUILTIN_TEMPLATE_SETS: Mapping[str, SummaryTemplateSet] = {
    "northcua_v1": SummaryTemplateSet(
        system="northcua_v1_system.j2",
        user="northcua_v1_user.j2",
        repair_user="northcua_v1_repair_user.j2",
    )
}


class SummaryPromptRenderer:
    """Compile and render trusted summary templates with a typed context."""

    def __init__(
        self,
        *,
        loader: BaseLoader | None = None,
        template_sets: Mapping[str, SummaryTemplateSet] | None = None,
    ) -> None:
        """Create one immutable renderer and eagerly validate its templates.

        ``loader`` and ``template_sets`` are composition seams for tests and
        trusted embedding code. Runtime YAML never controls either value.
        """

        self._environment = Environment(
            loader=loader or PackageLoader("northcua.agentinfra.context.summary", "templates"),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            auto_reload=False,
        )
        self._template_sets = dict(template_sets or _BUILTIN_TEMPLATE_SETS)
        self._templates: dict[str, tuple[Template, Template, Template]] = {
            profile: (
                self._environment.get_template(template_set.system),
                self._environment.get_template(template_set.user),
                self._environment.get_template(template_set.repair_user),
            )
            for profile, template_set in self._template_sets.items()
        }

    def messages(
        self,
        policy: ContextSummaryPolicy,
        payload: JsonObject,
        *,
        prompt_profile: str,
    ) -> list[Message]:
        """Render provider-neutral SYSTEM and USER messages."""

        system_template, user_template, _ = self._resolve(prompt_profile)
        system_text = self._render_system(system_template, policy, prompt_profile)
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        user_text = user_template.render(context=SummaryUserTemplateContext(payload_json=payload_json)).strip()
        return [
            Message(role=Role.SYSTEM, content=[TextBlock(text=system_text)]),
            Message(role=Role.USER, content=[TextBlock(text=user_text)]),
        ]

    def repair_message(
        self,
        *,
        prompt_profile: str,
        validation_error: str,
        maximum_canonical_characters: int,
        maximum_json_characters: int,
    ) -> Message:
        """Render a hard-limit-aware correction message."""

        _, _, repair_template = self._resolve(prompt_profile)
        text = repair_template.render(
            context=SummaryRepairTemplateContext(
                validation_error=validation_error,
                maximum_canonical_characters=maximum_canonical_characters,
                maximum_json_characters=maximum_json_characters,
            )
        ).strip()
        return Message(role=Role.USER, content=[TextBlock(text=text)])

    def fingerprint(self, policy: ContextSummaryPolicy, *, prompt_profile: str) -> str:
        """Fingerprint all stable prompt behavior while excluding dynamic source."""

        system_template, user_template, repair_template = self._resolve(prompt_profile)
        template_set = self._template_sets[prompt_profile]
        loader = self._environment.loader
        if loader is None:
            raise ValueError("context summary prompt renderer requires a source loader")
        template_sources = {
            "system": loader.get_source(self._environment, template_set.system)[0],
            "user": loader.get_source(self._environment, template_set.user)[0],
            "repair_user": loader.get_source(self._environment, template_set.repair_user)[0],
        }
        system_text = self._render_system(system_template, policy, prompt_profile)
        user_framing = user_template.render(
            context=SummaryUserTemplateContext(payload_json=_PAYLOAD_FINGERPRINT_SENTINEL)
        ).strip()
        repair_framing = repair_template.render(
            context=SummaryRepairTemplateContext(
                validation_error=_ERROR_FINGERPRINT_SENTINEL,
                maximum_canonical_characters=0,
                maximum_json_characters=0,
            )
        ).strip()
        payload = json.dumps(
            {
                "policy_profile": policy.profile,
                "profile_rules": policy.rules,
                "prompt_profile": prompt_profile,
                "repair_framing": repair_framing,
                "system_prompt": system_text,
                "template_sources": template_sources,
                "user_framing": user_framing,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _resolve(self, prompt_profile: str) -> tuple[Template, Template, Template]:
        try:
            return self._templates[prompt_profile]
        except KeyError as exc:
            raise ValueError(f"unknown context summary prompt profile: {prompt_profile}") from exc

    @staticmethod
    def _render_system(
        template: Template,
        policy: ContextSummaryPolicy,
        prompt_profile: str,
    ) -> str:
        context = SummarySystemTemplateContext(
            prompt_profile=prompt_profile,
            policy_profile=policy.profile,
            profile_rules=policy.rules,
            schema_fields=tuple(ContextSummaryDocumentV1.model_fields),
        )
        return template.render(context=context).strip()


@lru_cache(maxsize=1)
def _default_renderer() -> SummaryPromptRenderer:
    """Lazily load immutable package templates only when context management runs."""

    return SummaryPromptRenderer()


def summary_prompt_fingerprint(policy: ContextSummaryPolicy, *, prompt_profile: str) -> str:
    """Return the stable fingerprint for one resolved policy and template profile."""

    return _default_renderer().fingerprint(policy, prompt_profile=prompt_profile)


def summary_messages(
    policy: ContextSummaryPolicy,
    payload: JsonObject,
    *,
    prompt_profile: str = "northcua_v1",
) -> list[Message]:
    """Render summary messages through the shared package template renderer."""

    return _default_renderer().messages(policy, payload, prompt_profile=prompt_profile)


def summary_repair_message(
    *,
    prompt_profile: str,
    validation_error: str,
    maximum_canonical_characters: int,
    maximum_json_characters: int,
) -> Message:
    """Render one correction message through the same versioned template set."""

    return _default_renderer().repair_message(
        prompt_profile=prompt_profile,
        validation_error=validation_error,
        maximum_canonical_characters=maximum_canonical_characters,
        maximum_json_characters=maximum_json_characters,
    )
