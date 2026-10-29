"""Export load evidence: python scripts/summarize-load.py RAW_JSON OUTPUT_DIR.

Creates cohorts.csv, files.csv, resources.csv and summary.json exclusively. CSV
uses LF and blank cells for unknown values; JSON uses null. Reads no fixtures.
"""

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path


def at(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def maximum(values):
    observed = [v for v in values if number(v)]
    return max(observed) if observed else None


def complete_sum(values):
    values = list(values)
    return sum(values) if values and all(number(v) for v in values) else None


def matches(file, source_key, measured_key):
    source, measured = file.get(source_key), at(file, measured_key, source_key)
    return source == measured if source is not None and measured is not None else None


def counted(events, key):
    if events is None:
        return None
    return dict(sorted(Counter(str(e[key]) for e in events if key in e).items()))


def csv_file(path, rows, fields):
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def export(raw_path, output):
    raw_bytes = raw_path.read_bytes()
    raw = json.loads(raw_bytes)
    runs, resources = raw.get("runs", []), raw.get("resources", {})
    samples = resources.get("samples", [])
    per_run = raw.get("files_per_run")
    requested_runs = raw.get("requested_runs")
    expected = raw.get("expected_file_outcomes")
    if expected is None and number(per_run) and number(requested_runs):
        expected = per_run * requested_runs
    cohort_rows, file_rows = [], []
    for run in runs:
        files = run.get("files", [])
        accepted = (
            run.get("status") == "passed"
            and per_run is not None
            and len(files) == per_run
            and run.get("file_integrity_passed") == per_run
        )
        cohort_rows.append(
            {
                "run": run.get("run"),
                "status": run.get("status"),
                "file_denominator": per_run,
                "recorded_files": len(files) if "files" in run else None,
                "file_integrity_passed": run.get("file_integrity_passed"),
                "cohort_accepted_file_outcomes": len(files) if accepted else 0,
                "overlap_s": at(run, "overlap", "common_seconds"),
                "source_bytes": (
                    raw["file_bytes"] * per_run
                    if number(raw.get("file_bytes")) and number(per_run)
                    else None
                ),
                "transfer_window_s": run.get("transfer_window_s"),
                "transfer_MB_per_s": run.get("transfer_MB_per_s"),
                "selection_to_ready_s": run.get("selection_to_ready_s"),
                "selection_to_ready_MB_per_s": run.get("selection_to_ready_MB_per_s"),
                "full_cohort_s": run.get("total_elapsed_s"),
                "api_count": at(run, "api_latency", "count"),
                "api_p50_ms": at(run, "api_latency", "p50_ms"),
                "api_p95_ms": at(run, "api_latency", "p95_ms"),
                "outbox_publications": run.get("outbox_publications"),
                "error": run.get("error") or run.get("fatal"),
                "cleanup_error": run.get("cleanup_error"),
            }
        )
        inventory = {f.get("id"): f for f in at(run, "final_inventory", "files") or []}
        for file in files:
            attempts = file.get("task_attempts")
            row = {
                "run": run.get("run"),
                "cohort_status": run.get("status"),
                "name": file.get("name"),
                "file_id": file.get("id"),
                "status_before_cleanup": at(inventory.get(file.get("id")), "status"),
                "integrity_status": file.get("integrity_status"),
                "source_bytes": file.get("bytes"),
                "source_sha256": file.get("sha256"),
                "worker_bytes": at(file, "worker", "bytes"),
                "worker_sha256": at(file, "worker", "sha256"),
                "worker_bytes_match": matches(file, "bytes", "worker"),
                "worker_sha256_match": matches(file, "sha256", "worker"),
                "download_bytes": at(file, "download", "bytes"),
                "download_sha256": at(file, "download", "sha256"),
                "download_bytes_match": matches(file, "bytes", "download"),
                "download_sha256_match": matches(file, "sha256", "download"),
                "task_attempt_count": len(attempts) if attempts is not None else None,
                "task_retry_ordinals_sum": (
                    complete_sum(t.get("retries") for t in attempts)
                    if attempts is not None
                    else None
                ),
                "task_outcomes": (
                    json.dumps(counted(attempts, "outcome"), sort_keys=True)
                    if attempts is not None
                    else None
                ),
            }
            for key in (
                "browser_hash_s",
                "transfer_window_s",
                "completion_api_s",
                "completion_to_ready_s",
                "stream_verification_s",
                "selection_to_ready_s",
                "download_s",
                "download_hash_s",
                "part_put_count",
                "expected_parts",
                "retried_parts",
                "committed_result_count",
                "pre_ready_download_status",
                "cross_workspace_pending_status",
                "cross_workspace_download_status",
            ):
                row[key] = file.get(key)
            file_rows.append(row)
    resource_rows = []
    service_names = list(at(raw, "budgets", "resource_budgets", "containers") or {})
    for index, sample in enumerate(samples, 1):
        observed = {s.get("service"): s for s in sample.get("services", [])}
        for name in sorted(set(service_names) | set(observed)):
            service = observed.get(name, {})
            row = {
                "sample": index,
                "run": sample.get("run"),
                "phase": sample.get("phase"),
                "t_s": sample.get("t_s"),
                "collection_started_at_s": sample.get("collection_started_at_s"),
                "collection_finished_at_s": sample.get("collection_finished_at_s"),
                "service": name,
                "service_cpu_percent": service.get("cpu_percent"),
                "service_memory_usage_bytes": service.get("memory_usage_bytes"),
                "service_rss_sum_bytes": service.get("rss_sum_bytes"),
                "service_oom": service.get("oom"),
                "service_oom_kill": service.get("oom_kill"),
            }
            for key in (
                "browser_rss_sum_bytes",
                "browser_cpu_percent",
                "node_rss_bytes",
                "node_cpu_percent",
            ):
                row[key] = sample.get(key)
            for column, path in {
                "api_inflight": ("ops", "api_inflight"),
                "api_pool_size": ("ops", "db_pool", "size"),
                "api_pool_checked_out": ("ops", "db_pool", "checked_out"),
                "api_pool_overflow": ("ops", "db_pool", "overflow"),
                "api_pool_capacity": ("ops", "db_pool", "capacity"),
                "pg_connections": ("database", "connections", "total"),
                "pg_active_connections": ("database", "connections", "active"),
                "pg_lock_waiting": ("database", "connections", "lock_waiting"),
                "pg_idle_in_transaction": (
                    "database",
                    "connections",
                    "idle_in_transaction",
                ),
                "pg_ungranted_locks": ("database", "ungranted_locks"),
                "pg_deadlocks": ("database", "database", "deadlocks"),
                "outbox_count": ("database", "outbox", "count"),
                "outbox_oldest_age_s": ("database", "outbox", "oldest_age_seconds"),
                "redis_queued": ("database", "broker", "queued_messages"),
                "redis_unacked": ("database", "broker", "unacked_messages"),
                "redis_unacked_index": ("database", "broker", "unacked_index"),
                "processing": ("database", "work", "processing"),
                "active_claims": ("database", "work", "active_claims"),
                "expired_claims": ("database", "work", "expired_claims"),
            }.items():
                row[column] = at(sample, *path)
            for key in ("active_files", "hash_workers", "global_parts"):
                value = at(sample, "scheduler", key)
                row["scheduler_" + key] = int(value) if value is not None else None
            resource_rows.append(row)
    # The same browser/DB sample repeats once per service in resources.csv;
    # maxima are unaffected, but counts must use the original samples.
    peak_fields = [
        k
        for k in (resource_rows[0] if resource_rows else {})
        if k
        not in {
            "sample",
            "run",
            "phase",
            "t_s",
            "collection_started_at_s",
            "collection_finished_at_s",
            "service",
        }
        and not k.startswith("service_")
    ]
    peaks = {key: maximum(r[key] for r in resource_rows) for key in peak_fields}
    service_peaks = {
        name: {
            key: maximum(r[key] for r in resource_rows if r["service"] == name)
            for key in (
                "service_cpu_percent",
                "service_memory_usage_bytes",
                "service_rss_sum_bytes",
                "service_oom",
                "service_oom_kill",
            )
        }
        for name in sorted(set(service_names) | {r["service"] for r in resource_rows})
    }
    api_events = (
        [event for run in runs for event in run.get("api_events", [])]
        if runs and all("api_events" in run for run in runs)
        else None
    )
    storage_events = (
        [event for run in runs for event in run.get("storage_events", [])]
        if runs and all("storage_events" in run for run in runs)
        else None
    )
    summary = {
        "schema_version": 1,
        "exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "raw_evidence_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "run_id": raw.get("run_id"),
        "status": raw.get("status"),
        "error": raw.get("error"),
        "scope": raw.get("scope"),
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "units": {"MB": 1000000, "GB": 1000000000, "MiB": 1048576, "GiB": 1073741824},
        "missing_value_policy": "Unknown or incomplete measurements are JSON null / CSV blank; no fixture contents are read.",
        "denominators": {
            "requested_cohorts": requested_runs,
            "recorded_cohorts": len(runs) if "runs" in raw else None,
            "files_per_cohort": per_run,
            "file_bytes": raw.get("file_bytes"),
            "expected_file_outcomes": expected,
            "recorded_file_outcomes": len(file_rows),
            "accepted_file_outcomes": raw.get("file_outcomes_passed"),
            "independently_verified_file_outcomes": raw.get("file_integrity_passed"),
            "passed_cohorts": sum(r.get("status") == "passed" for r in runs),
            "release_gate_passed": raw.get("release_gate_passed"),
            "target_size": raw.get("target_size"),
            "definition": "A file can pass integrity in a failed cohort. Only passed complete cohorts count toward the concurrency release gate; small fixtures do not establish 500 MB outcomes.",
        },
        "environment": raw.get("environment"),
        "budget_sha256": raw.get("budget_sha256"),
        "budgets": raw.get("budgets"),
        "sources_unchanged_during_run": raw.get("sources_unchanged_during_run"),
        "fixture_generation_and_independent_hash_s": raw.get(
            "fixture_generation_and_independent_hash_s"
        ),
        "total_runner_s": raw.get("total_runner_s"),
        "cohorts": cohort_rows,
        "observed_peaks": peaks,
        "service_observed_peaks": service_peaks,
        "sampling": {
            "sample_count": len(samples) if "samples" in resources else None,
            "nominal_interval_ms": resources.get("interval_ms"),
            "sample_errors": resources.get("sample_errors"),
            "skipped_overlapping_collections": resources.get("skipped_overlapping_collections"),
            "definition": resources.get("definition"),
            "scope": "PG connection counts include the read-only observer. API pool metrics describe the API process only. CPU/RSS maxima are sampled; service RSS sums may double-count shared pages. OOM fields are counters, not inferred process outcomes.",
        },
        "api": {
            "status_counts": counted(api_events, "status"),
            "admission_429_count": (
                sum(e.get("status") == 429 for e in api_events) if api_events is not None else None
            ),
            "unhandled_5xx_count": (
                sum(number(e.get("status")) and e["status"] >= 500 for e in api_events)
                if api_events is not None
                else None
            ),
            "latency_by_cohort": [
                {
                    "run": r.get("run"),
                    "all_routes": r.get("api_latency"),
                    "by_route": r.get("api_latency_by_route"),
                }
                for r in runs
            ],
            "transport_failure_count": (
                sum(len(r["api_transport_failures"]) for r in runs)
                if runs and all("api_transport_failures" in r for r in runs)
                else None
            ),
            "population": "Native browser application API requests, excluding observer probes; exact latency populations are retained per cohort. Missing optional failure lists remain unknown.",
        },
        "storage": {
            "status_counts": counted(storage_events, "status"),
            "protocol_counts": counted(storage_events, "protocol"),
            "part_put_count": complete_sum(f["part_put_count"] for f in file_rows),
            "retried_parts": complete_sum(f["retried_parts"] for f in file_rows),
        },
        "workers": {
            "task_attempts": complete_sum(f["task_attempt_count"] for f in file_rows),
            "reported_task_retry_ordinals_sum": complete_sum(
                f["task_retry_ordinals_sum"] for f in file_rows
            ),
            "outbox_publications": complete_sum(r.get("outbox_publications") for r in runs),
            "scope": "Task attempts are correlated by exact file ID. Celery retries are recorded attempt retry ordinals, not independently counted redeliveries. Outbox publications cover the dedicated worker's cohort time window.",
        },
        "final_scoped_inventory_by_cohort": [
            {
                "run": r.get("run"),
                "before_cleanup": r.get("final_inventory"),
                "after_cleanup": r.get("after_cleanup"),
                "cleanup": r.get("cleanup"),
                "cleanup_error": r.get("cleanup_error"),
            }
            for r in runs
        ],
    }
    names = ["cohorts.csv", "files.csv", "resources.csv", "summary.json"]
    output.mkdir(parents=True, exist_ok=True)
    for name in names:
        if (output / name).exists():
            raise FileExistsError(output / name)
    for name, rows, fallback in (
        ("cohorts.csv", cohort_rows, ["run", "status", "file_denominator"]),
        ("files.csv", file_rows, ["run", "cohort_status", "file_id", "integrity_status"]),
        ("resources.csv", resource_rows, ["sample", "run", "service"]),
    ):
        csv_file(output / name, rows, list(rows[0]) if rows else fallback)
    with (output / "summary.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "status": "exported",
                "cohorts": len(cohort_rows),
                "files": len(file_rows),
                "resource_rows": len(resource_rows),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_json", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    export(args.raw_json, args.output_dir)
