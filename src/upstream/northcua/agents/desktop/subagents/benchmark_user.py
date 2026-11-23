"""An optional official OSWorld user interaction service, separate from GUI executors."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import Field

from northcua.agentinfra.nexau_compat.adapters.openai_chat import (
    capture_chat_responses,
    wrap_openai_chat_client,
)
from northcua.agentinfra.providers.chat_response_capture import (
    ChatResponseCapture,
    _append_receipt,
    _ChatResponseAbort,
)
from northcua.agentkit.orchestration.contracts import ContractModel, SubagentResult, SubagentSpec

_MODEL_FACADE_LOCK = threading.Lock()


class Question(ContractModel):
    question: str = Field(min_length=1, max_length=16000)


class BenchmarkUser:
    spec = SubagentSpec(
        agent_id="user",
        description=(
            "USER CLARIFICATION SERVICE, not a GUI executor. Ask the task's configured official simulated "
            "user when essential user information, corrected documents, or a decision is missing. "
            "Use before declaring failure for missing information that only the user can supply. "
            "Set allowed_tools=[ask_user], agent_input={question: the exact question}. "
            "It returns the user's real answer using the benchmark's original user-simulation implementation; "
            "it cannot execute GUI actions or complete the task. Do not ask unchanged questions repeatedly. "
            "An answer does not authorize replay of an unresolved write. Continue GUI work with gui_jev "
            "or the bounded general fallback after receiving the answer."
        ),
        capabilities=("user_clarification",), required_tools=("ask_user",), input_model=Question,
        bind_input=lambda intent: {"question": intent.user_question},
    )

    def __init__(self, simulator, model_client, model, log_dir):
        self.simulator, self.model_client, self.model = simulator, model_client, model
        self.log_dir = Path(log_dir)
        self._lock = _MODEL_FACADE_LOCK

    @classmethod
    def from_spec(cls, path):
        data = json.loads(Path(path).read_text())
        root = Path(data["runtime_root"]).resolve()
        desktop_root = (root / "desktop_env").resolve()
        # Import only from the pinned official runtime, never an installed stale desktop_env.
        for name, module in list(sys.modules.items()):
            if name == "desktop_env" or name.startswith("desktop_env."):
                origin = getattr(module, "__file__", None)
                if origin and not Path(origin).resolve().is_relative_to(desktop_root):
                    raise RuntimeError("Another desktop_env runtime is already loaded")
        sys.path.insert(0, str(root))
        official = importlib.import_module("desktop_env.user_simulator")
        model_client = importlib.import_module("desktop_env.evaluators.model_client")
        if (not Path(official.__file__).resolve().is_relative_to(desktop_root)
                or not Path(model_client.__file__).resolve().is_relative_to(desktop_root)):
            raise RuntimeError("Official user simulator resolved outside the pinned runtime")
        config = dict(data["user_simulator"])
        model = data["model"]
        if config.get("type", "llm") == "llm":
            config.update(provider="openai_compatible", model=model["model"], base_url=model["base_url"],
                          api_key=model["api_key"], max_tokens=8192)
        simulator = official.create_user_simulator(config)
        if config.get("type", "llm") == "llm":
            # Dedicated inherited USER_SIM env vars must not override this run's resolved transport.
            simulator.provider, simulator.model = "openai_compatible", model["model"]
            simulator.base_url, simulator.api_key = model["base_url"], model["api_key"]
            simulator.max_tokens = 8192
        simulator.reset(instruction=data["instruction"])
        return cls(simulator, model_client, model, data["log_dir"])

    async def run(self, request, runtime):
        arguments = Question.model_validate(request.contract.agent_input).model_dump()
        reply = await runtime.execute("ask_user", arguments, sensitive=True)
        return SubagentResult(
            subgoal_id=request.contract.subgoal_id, attempt_id=request.attempt_id,
            outcome="achieved", exit_reason="user_answered",
            summary=json.dumps({"question": arguments["question"], "answer": reply["answer"]}, ensure_ascii=False),
            evidence_refs=[request.observation["observation_id"]],
        )

    async def ask(self, arguments, *, request, max_seconds=180):
        question = Question.model_validate(arguments).question
        stopped = threading.Event()
        deadline = time.monotonic() + min(180, max_seconds)
        task = asyncio.create_task(asyncio.to_thread(self._respond, question, request, stopped, deadline))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stopped.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise

    def _respond(self, question, request, stopped, deadline):
        with self._lock:
            if stopped.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("User question expired before dispatch")
            call = uuid4().hex
            metadata = {"session_id": request.session_id, "attempt_id": request.attempt_id,
                        "round_num": request.round_num, "interaction_id": call,
                        "agent_id": "user_simulator", "purpose": "user_interaction"}
            capture = ChatResponseCapture(
                self.log_dir / "user_simulator_responses.jsonl", metadata, 1,
                usage_path=self.log_dir / "user_simulator_usage.jsonl",
            )
            original_generate, original_backend = self.model_client.generate_chat, self.model_client.create_backend
            clients = []

            def create_backend(config):
                backend = original_backend(config)
                if not hasattr(backend, "_client"):
                    raise RuntimeError("User simulator requires the configured OpenAI-compatible backend")
                old = backend._client
                client = old.with_options(
                    timeout=max(0.01, min(deadline - time.monotonic(),
                                          float(self.model.get("timeout_seconds", 180)))), max_retries=0,
                    http_client=httpx.Client(trust_env=not self.model.get("direct_api", True)),
                )
                old.close()
                clients.append(client)
                backend._client = wrap_openai_chat_client(client)
                return backend

            def generate(messages, options=None, **kwargs):
                return original_generate(messages, options={**(options or {}), "retry_attempts": 1}, **kwargs)

            _append_receipt(self.log_dir / "conversation.jsonl", {**metadata, "event": "question", "question": question})
            try:
                self.model_client.generate_chat, self.model_client.create_backend = generate, create_backend
                with capture_chat_responses(capture):
                    answer = self.simulator.respond(question)
                if not isinstance(answer, str) or not answer.strip():
                    raise RuntimeError("Official user simulator returned an empty answer")
                _append_receipt(self.log_dir / "conversation.jsonl", {**metadata, "event": "answer", "answer": answer})
                return {"ok": True, "answer": answer}
            except _ChatResponseAbort as error:
                capture.record_failure(error.error, None)
                raise error.error from None
            except Exception as error:
                capture.record_failure(error, None)
                raise
            finally:
                self.model_client.generate_chat, self.model_client.create_backend = original_generate, original_backend
                for client in clients:
                    client.close()
