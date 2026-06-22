#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026, Advanced Micro Devices, Inc. All rights reserved.
"""Analyze Kimi decode torch profiler traces for AR/RMS/GEMM kernel time.

The script consumes chrome traces produced by ``profile_kimi_k25_decode_tp.py``.
It groups events by rough operator families and reports per-rank totals, max
rank totals, and optional baseline-vs-prezero deltas.

By default operator-family totals only include profiler events whose chrome
trace category is ``kernel``.  This avoids double-counting nested CPU/ATen
wrapper events and their child GPU kernels.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


FAMILIES: list[tuple[str, tuple[str, ...]]] = [
    (
        "ar_rms",
        (
            "fused_allreduce_rmsnorm",
            "allreduce_rmsnorm",
            "all_reduce_rmsnorm",
            "ar_rms",
            "fused_ar",
        ),
    ),
    (
        "all_reduce",
        (
            "all_reduce",
            "allreduce",
            "custom_all_reduce",
            "quick_all_reduce",
            "nccl",
            "rccl",
        ),
    ),
    (
        "prezero_qkva",
        (
            "hip_mla_qkv_a_norm",
            "mla_qk_rmsnorm",
            "prezero",
            "tgemm_prezero",
        ),
    ),
    (
        "rmsnorm",
        (
            "rmsnorm",
            "rms_norm",
            "_fused_qk_rmsnorm",
            "qk_rmsnorm",
            "layernorm",
        ),
    ),
    (
        "gemm",
        (
            "gemm",
            "matmul",
            "rocblas",
            "hipblas",
            "deepgemm",
            "linear",
            "wgrad",
        ),
    ),
]

SEGMENT_FAMILIES = ("ar_rms", "all_reduce", "prezero_qkva", "rmsnorm", "gemm")


def open_trace(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_trace_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    yield from sorted(path.rglob("*.json"))
    yield from sorted(path.rglob("*.json.gz"))


def rank_from_name(path: Path) -> str:
    m = re.search(r"rank(\d+)", path.name)
    return m.group(1) if m else path.stem


def family_for(name: str) -> str | None:
    lowered = name.lower()
    for family, needles in FAMILIES:
        if any(needle in lowered for needle in needles):
            if family == "rmsnorm" and any(
                needle in lowered
                for needle in ("fused_allreduce_rmsnorm", "allreduce_rmsnorm", "ar_rms")
            ):
                return "ar_rms"
            return family
    return None


def analyze_one(path: Path, include_cpu_ops: bool) -> dict:
    data = open_trace(path)
    if "traceEvents" not in data:
        return {}
    totals_us: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    examples: dict[str, Counter[str]] = defaultdict(Counter)
    step_total_us = 0.0
    step_count = 0

    for ev in data.get("traceEvents", []):
        if ev.get("ph") != "X":
            continue
        name = str(ev.get("name", ""))
        dur = float(ev.get("dur", 0.0))
        if "_decode_step_" in name:
            step_total_us += dur
            step_count += 1
        if not include_cpu_ops and ev.get("cat") != "kernel":
            continue
        fam = family_for(name)
        if fam is None:
            continue
        totals_us[fam] += dur
        counts[fam] += 1
        if len(examples[fam]) < 20:
            examples[fam][name] += 1

    return {
        "path": str(path),
        "rank": rank_from_name(path),
        "totals_us": dict(totals_us),
        "counts": dict(counts),
        "step_total_us": step_total_us,
        "step_count": step_count,
        "examples": {k: v.most_common(8) for k, v in examples.items()},
    }


def summarize(path: Path, include_cpu_ops: bool) -> dict:
    ranks = [
        result
        for p in iter_trace_files(path)
        if (result := analyze_one(p, include_cpu_ops))
    ]
    families = [name for name, _ in FAMILIES]
    max_by_family = {
        fam: max((r["totals_us"].get(fam, 0.0) for r in ranks), default=0.0)
        for fam in families
    }
    count_by_family_at_max = {}
    avg_by_family_at_max = {}
    for fam in families:
        rank_at_max = max(
            ranks,
            key=lambda r: r["totals_us"].get(fam, 0.0),
            default=None,
        )
        count = int(rank_at_max["counts"].get(fam, 0)) if rank_at_max else 0
        count_by_family_at_max[fam] = count
        avg_by_family_at_max[fam] = (
            max_by_family.get(fam, 0.0) / count if count else 0.0
        )
    sum_by_family = {
        fam: sum((r["totals_us"].get(fam, 0.0) for r in ranks))
        for fam in families
    }
    max_step_total = max((r["step_total_us"] for r in ranks), default=0.0)
    segment_total = sum(max_by_family.get(fam, 0.0) for fam in SEGMENT_FAMILIES)
    return {
        "path": str(path),
        "event_filter": "all X events" if include_cpu_ops else "kernel events only",
        "ranks": ranks,
        "max_by_family_us": max_by_family,
        "count_by_family_at_max": count_by_family_at_max,
        "avg_by_family_at_max_us": avg_by_family_at_max,
        "sum_by_family_us": sum_by_family,
        "max_step_total_us": max_step_total,
        "target_segment_total_us": segment_total,
    }


def print_summary(label: str, summary: dict, show_examples: bool) -> None:
    print(f"\n== {label}: {summary['path']} ==")
    print(f"event_filter: {summary['event_filter']}")
    print(
        f"{'family':<16} {'max_rank_us':>14} {'count':>8} "
        f"{'avg_us':>10} {'sum_ranks_us':>14} {'pct_step_max':>13}"
    )
    step = summary["max_step_total_us"] or 0.0
    for fam, _ in FAMILIES:
        max_us = summary["max_by_family_us"].get(fam, 0.0)
        count = summary["count_by_family_at_max"].get(fam, 0)
        avg_us = summary["avg_by_family_at_max_us"].get(fam, 0.0)
        sum_us = summary["sum_by_family_us"].get(fam, 0.0)
        pct = (100.0 * max_us / step) if step else 0.0
        print(
            f"{fam:<16} {max_us:14.3f} {count:8d} "
            f"{avg_us:10.3f} {sum_us:14.3f} {pct:12.2f}%"
        )
    print(f"{'target_segment':<16} {summary['target_segment_total_us']:14.3f}")
    print(f"{'decode_step_total':<16} {step:14.3f}")

    if not show_examples:
        return
    for rank in summary["ranks"]:
        print(f"\n  rank {rank['rank']} examples:")
        for fam, items in rank["examples"].items():
            names = ", ".join(f"{name} x{count}" for name, count in items[:4])
            print(f"    {fam}: {names}")


def print_comparison(base: dict, opt: dict) -> None:
    print("\n== Comparison: baseline -> optimized ==")
    print(f"{'family':<16} {'base_max_us':>13} {'opt_max_us':>13} {'delta_us':>12} {'speedup':>9}")
    for fam, _ in FAMILIES:
        b = base["max_by_family_us"].get(fam, 0.0)
        o = opt["max_by_family_us"].get(fam, 0.0)
        speedup = (b / o) if o else 0.0
        print(f"{fam:<16} {b:13.3f} {o:13.3f} {b - o:12.3f} {speedup:9.3f}")
    b_step = base["max_step_total_us"]
    o_step = opt["max_step_total_us"]
    b_seg = base["target_segment_total_us"]
    o_seg = opt["target_segment_total_us"]
    print(f"{'target_segment':<16} {b_seg:13.3f} {o_seg:13.3f} {b_seg-o_seg:12.3f} {(b_seg/o_seg if o_seg else 0.0):9.3f}")
    print(f"{'decode_step_total':<16} {b_step:13.3f} {o_step:13.3f} {b_step-o_step:12.3f} {(b_step/o_step if o_step else 0.0):9.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, help="single trace dir/file")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--optimized", type=Path)
    parser.add_argument("--examples", action="store_true")
    parser.add_argument(
        "--include-cpu-ops",
        action="store_true",
        help="include CPU/ATen wrapper events in family totals; may double-count nested work",
    )
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    if args.trace:
        summary = summarize(args.trace, args.include_cpu_ops)
        print_summary("trace", summary, args.examples)
        result = {"trace": summary}
    else:
        if not args.baseline or not args.optimized:
            raise SystemExit("pass --trace or both --baseline and --optimized")
        base = summarize(args.baseline, args.include_cpu_ops)
        opt = summarize(args.optimized, args.include_cpu_ops)
        print_summary("baseline", base, args.examples)
        print_summary("optimized", opt, args.examples)
        print_comparison(base, opt)
        result = {"baseline": base, "optimized": opt}

    if args.json_out:
        args.json_out.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
