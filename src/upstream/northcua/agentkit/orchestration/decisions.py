"""Shared, metered System One transport for routing and GUI decisions."""

from __future__ import annotations

import asyncio

import httpx

from northcua.agentinfra.providers.token_usage import ModelUsageLedger


class DecisionClient:
    def __init__(self, config, *, timeout=30):
        self.config, self.timeout = config, timeout

    @classmethod
    def from_env(cls, *, model=None, timeout=30):
        from jev_desktop import TypeSafeModel

        config = TypeSafeModel.from_env()
        if model:
            from jev_desktop.model import resolve_model

            config.model = resolve_model(model, config.endpoint)
        return cls(config, timeout=timeout)

    async def ask(self, request, call_model, *, post_model=None, model_usage: ModelUsageLedger | None = None):
        payload = {**request, "model": self.config.model}

        async def post():
            call_id = (
                model_usage.start(self.config.model, backend="typesafe", attempt=attempt + 1)
                if model_usage
                else None
            )
            try:
                if post_model is None:
                    async with httpx.AsyncClient(follow_redirects=False) as client:
                        response = await client.post(
                            self.config.endpoint,
                            headers={"Authorization": f"Bearer {self.config.api_key}"},
                            json=payload,
                            timeout=self.timeout,
                        )
                else:
                    response = await post_model(
                        self.config.endpoint,
                        payload,
                        api_key=self.config.api_key,
                        timeout=self.timeout,
                        backend="typesafe",
                        attempt=attempt + 1,
                    )
                try:
                    result = response.json()
                except ValueError:
                    result = None
                if model_usage is not None:
                    model_usage.response(call_id, result, http_status=response.status_code)
                response.raise_for_status()
                if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                    raise ValueError("invalid Jev response")
                return result
            except BaseException as error:
                if model_usage is not None:
                    model_usage.failure(call_id, error)
                raise

        for attempt in range(3):
            try:
                return await call_model(self.config.model, post)
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in (429, 500, 502, 503, 504, 529) or attempt == 2:
                    raise RuntimeError(f"Jev returned HTTP {error.response.status_code}") from None
                await asyncio.sleep(0.5 * 2**attempt)
            except httpx.TransportError:
                if attempt == 2:
                    raise RuntimeError("Jev transport stayed unavailable; no action dispatched") from None
                await asyncio.sleep(0.5 * 2**attempt)
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("Jev returned no valid decision; no action dispatched") from None
        raise RuntimeError("Jev stayed unavailable")
