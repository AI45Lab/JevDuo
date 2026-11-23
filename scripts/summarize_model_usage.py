#!/usr/bin/env python3
"""Summarize only durable provider-call receipts, separately for each agent/task."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

METRICS = ("input_tokens", "output_tokens", "cache_input_tokens", "uncached_input_tokens",
           "total_tokens", "reasoning_tokens")


def collect(run, filename="model_usage.jsonl"):
    calls, warnings = [], []
    for path in sorted(run.rglob(filename)):
        records = {}
        with path.open(encoding="utf-8") as source:
            for number, line in enumerate(source, 1):
                try:
                    record = json.loads(line)
                    if (not isinstance(record, dict) or record.get("schema_version") != 1 or
                            record.get("event") not in {"request", "response", "failure"}):
                        raise ValueError("unsupported receipt")
                    call_id = record["call_id"]
                    item = records.setdefault(call_id, {"responses": [], "failures": []})
                    if record["event"] == "request":
                        if "request" in item:
                            raise ValueError("duplicate request id")
                        item["request"] = record
                    else:
                        item["responses" if record["event"] == "response" else "failures"].append(record)
                except (ValueError, KeyError, TypeError) as error:
                    warnings.append(f"{path.relative_to(run)}:{number}: {error}")
                    break  # Preserve a readable prefix; never guess past a torn tail.
        parts = path.relative_to(run).parts
        task = parts[parts.index("episodes") + 1] if "episodes" in parts else "unknown"
        for call_id, item in records.items():
            request = item.get("request")
            if request is None:
                warnings.append(f"{path.relative_to(run)}: orphan receipt {call_id}")
                continue
            responses = item["responses"]
            response = responses[0] if len(responses) == 1 else {}
            if len(responses) > 1:
                warnings.append(f"{path.relative_to(run)}: ambiguous multiple responses for {call_id}")
            calls.append({
                **request, "task_id": task, "receipt_path": str(path.relative_to(run)),
                "response_count": len(responses), "failure_count": len(item["failures"]),
                "status": ("response" if responses else "failure" if item["failures"]
                           else "pending_or_interrupted"),
                "tokens": response.get("tokens") or {key: None for key in METRICS},
                "raw_usage": response.get("raw_usage"),
                "response_id": response.get("response_id"), "response_model": response.get("response_model"),
                "http_status": response.get("http_status"),
                "error_types": [event["error_type"] for event in item["failures"]],
                "seconds": (max([response.get("at", request["at"]),
                                 *(r["at"] for r in item["failures"])]) - request["at"])
                if responses or item["failures"] else None,
            })
    return calls, warnings


def aggregate(calls):
    tokens = {}
    for name in METRICS:
        known = [item["tokens"].get(name) for item in calls if item["tokens"].get(name) is not None]
        tokens[name] = {"reported_sum": sum(known) if known else None,
                        "known_calls": len(known), "missing_calls": len(calls) - len(known),
                        "complete": len(known) == len(calls)}
    return {"calls": len(calls), "statuses": dict(Counter(item["status"] for item in calls)),
            "responses": sum(item["response_count"] for item in calls),
            "failures": sum(item["failure_count"] for item in calls), "tokens": tokens}


def summarize(calls, fields):
    groups = defaultdict(list)
    for call in calls:
        groups[tuple(call.get(field, "unknown") for field in fields)].append(call)
    return [{**dict(zip(fields, key, strict=True)), **aggregate(items)}
            for key, items in sorted(groups.items())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.run.is_dir():
        parser.error("run directory does not exist")
    output = args.run / "model-usage"
    output.mkdir(exist_ok=True)
    paths = [output / name for name in ("summary.json", "calls.jsonl", "by-task-agent.csv")]
    if not args.force and any(path.exists() for path in paths):
        parser.error("usage export exists; use --force to refresh")
    calls, warnings = collect(args.run)
    external, external_warnings = collect(args.run, "user_simulator_usage.jsonl")
    report = {
        "schema_version": 1,
        "scope": "actor model calls only; official evaluator excluded",
        "notes": ["input_tokens includes cache_input_tokens; do not add them",
                  "output_tokens already includes reasoning_tokens when reported as completion details",
                  "reported_sum sums known fields only; missing_calls are not zeros",
                  "request is an SDK/HTTP invocation, not proof of provider billing",
                  "no usage is estimated for timeouts, failed or interrupted requests"],
        "overall": aggregate(calls), "by_agent": summarize(calls, ("agent_id",)),
        "by_agent_purpose": summarize(calls, ("agent_id", "purpose")),
        "by_task_agent": summarize(calls, ("task_id", "agent_id")),
        "external_services": summarize(external, ("agent_id",)),
        "warnings": warnings + external_warnings,
    }
    temporary = output / "summary.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(paths[0])
    with paths[1].open("w", encoding="utf-8") as target:
        for scope, items in (("actor", calls), ("external_service", external)):
            for call in items:
                target.write(json.dumps({**call, "scope": scope}, ensure_ascii=False) + "\n")
    columns = ["task_id", "agent_id", "calls", "responses", "failures"]
    columns += [f"{metric}_{suffix}" for metric in METRICS for suffix in ("reported_sum", "missing_calls")]
    with paths[2].open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns)
        writer.writeheader()
        for row in report["by_task_agent"]:
            writer.writerow({**{key: row[key] for key in columns[:5]},
                             **{f"{metric}_{suffix}": row["tokens"][metric][suffix]
                                for metric in METRICS for suffix in ("reported_sum", "missing_calls")}})
    print(json.dumps({"calls": len(calls), "warnings": warnings, "summary": str(paths[0])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
