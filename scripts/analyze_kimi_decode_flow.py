#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026, Advanced Micro Devices, Inc. All rights reserved.
"""Report ordered Kimi decode kernel averages for the AR + MLA attention path."""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
from collections import defaultdict
from pathlib import Path


STAGES = [
    "ar_rms_before_attn",
    "prezero_fill_or_copy",
    "attn_qkva_gemm",
    "attn_qkva_prezero_gemm",
    "qk_rmsnorm",
    "q_b_proj",
    "kv_b_proj",
    "rope_cache",
    "mla_decode",
    "mla_reduce_rma",
    "attn_o_proj",
    "ar_after_attn",
    "moe_router",
    "moe_sort_quant",
    "moe_gemm1",
    "moe_gemm2",
    "ar_after_moe",
]


def open_trace(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def iter_trace_files(path: Path):
    if path.is_file():
        yield path
        return
    yield from sorted(path.glob("*rank*.json.gz"))
    yield from sorted(path.glob("*rank*.json"))


def rank_from_name(path: Path) -> str:
    name = path.name
    marker = "rank"
    if marker not in name:
        return path.stem
    rest = name.split(marker, 1)[1]
    digits = []
    for ch in rest:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    return "".join(digits) or path.stem


def has_any(names: list[str], *needles: str) -> bool:
    lowered = " ".join(names).lower()
    return any(needle in lowered for needle in needles)


def short_stage(name: str, prev_stage: str | None, next_names: list[str]) -> str | None:
    lowered = name.lower()

    if "allreduce_fusion_kernel_1stage" in lowered:
        if prev_stage in (
            "attn_o_proj",
            "mla_reduce_rma",
            "rope_cache",
            "kv_b_proj",
        ):
            return "ar_after_attn"
        return "ar_after_moe"

    if ("fillfunctor" in lowered or "copybuffer" in lowered) and has_any(
        next_names,
        "prezero_gemm::bf16gemm",
        "prezero_gemm::mla_qk_rmsnorm",
        "fillfunctor",
        "copybuffer",
    ):
        return "prezero_fill_or_copy"
    if "prezero_gemm::bf16gemm" in lowered:
        return "attn_qkva_prezero_gemm"
    if "prezero_gemm::mla_qk_rmsnorm" in lowered:
        return "qk_rmsnorm"
    if "fused_qk_rmsnorm_kernel" in lowered:
        return "qk_rmsnorm"

    if "hgemm_bf16_16x64x256" in lowered:
        if has_any(next_names[:4], "fused_qk_rmsnorm_kernel", "prezero_gemm::mla_qk_rmsnorm"):
            return "attn_qkva_gemm"
        return None
    if "cijk_alik_bljk_bbs" in lowered:
        return "q_b_proj"
    if "block_size_m_4_block_size_n_32_block_size_k_128" in lowered:
        return "kv_b_proj"
    if "fuse_qk_rope_concat_and_cache_mla" in lowered:
        return "rope_cache"
    if "mla_a8w8_qh16_qseqlen1" in lowered:
        return "mla_decode"
    if "kn_mla_reduce" in lowered:
        return "mla_reduce_rma"
    if "block_size_m_8_block_size_n_32_block_size_k_512" in lowered:
        return "attn_o_proj"
    if "hgemm_bf16_32x64x128" in lowered:
        return "attn_o_proj"

    if "grouped_topk_kernel" in lowered:
        return "moe_router"
    if "moe_sorting" in lowered or "mxfp4_quant_moe_sort" in lowered:
        return "moe_sort_quant"
    if "moe1" in lowered:
        return "moe_gemm1"
    if "moe2" in lowered:
        return "moe_gemm2"

    return None


def analyze_file(path: Path) -> dict:
    data = open_trace(path)
    rows = []
    for ev in data.get("traceEvents", []):
        if ev.get("ph") == "X" and ev.get("cat") == "kernel":
            rows.append((float(ev.get("ts", 0.0)), float(ev.get("dur", 0.0)), ev.get("name", "")))
    rows.sort()

    durations = defaultdict(list)
    prev_stage: str | None = None
    names = [row[2] for row in rows]
    for idx, (_, dur, name) in enumerate(rows):
        stage = short_stage(name, prev_stage, names[idx + 1 : idx + 8])
        if stage is None:
            continue
        durations[stage].append(dur)
        prev_stage = stage

    return {
        "rank": rank_from_name(path),
        "path": str(path),
        "durations": dict(durations),
    }


def summarize(path: Path) -> dict:
    ranks = [analyze_file(p) for p in iter_trace_files(path)]
    out = {"path": str(path), "ranks": ranks, "stages": {}}
    for stage in STAGES:
        rank_stats = []
        for rank in ranks:
            vals = rank["durations"].get(stage, [])
            if not vals:
                rank_stats.append({"rank": rank["rank"], "count": 0, "avg_us": 0.0, "sum_us": 0.0})
                continue
            rank_stats.append(
                {
                    "rank": rank["rank"],
                    "count": len(vals),
                    "avg_us": statistics.mean(vals),
                    "sum_us": sum(vals),
                    "p50_us": statistics.median(vals),
                }
            )
        max_rank = max(rank_stats, key=lambda x: x["sum_us"], default=None)
        out["stages"][stage] = {"ranks": rank_stats, "max_rank": max_rank}
    return out


def print_one(label: str, summary: dict) -> None:
    print(f"\n== {label}: {summary['path']} ==")
    print(f"{'stage':<24} {'count':>7} {'avg_us':>10} {'p50_us':>10} {'sum_us':>11}")
    for stage in STAGES:
        stat = summary["stages"][stage]["max_rank"] or {}
        print(
            f"{stage:<24} {int(stat.get('count', 0)):7d} "
            f"{stat.get('avg_us', 0.0):10.3f} {stat.get('p50_us', 0.0):10.3f} "
            f"{stat.get('sum_us', 0.0):11.3f}"
        )


def print_compare(base: dict, opt: dict) -> None:
    print("\n== ordered stage comparison, max-rank p50 ==")
    print(f"{'stage':<24} {'base_p50':>10} {'opt_p50':>10} {'delta':>10} {'speedup':>8}")
    for stage in STAGES:
        b = (base["stages"][stage]["max_rank"] or {}).get("p50_us", 0.0)
        o = (opt["stages"][stage]["max_rank"] or {}).get("p50_us", 0.0)
        speedup = b / o if o else 0.0
        print(f"{stage:<24} {b:10.3f} {o:10.3f} {b - o:10.3f} {speedup:8.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--optimized", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    base = summarize(args.baseline)
    opt = summarize(args.optimized)
    print_one("baseline", base)
    print_one("optimized", opt)
    print_compare(base, opt)
    if args.json_out:
        args.json_out.write_text(json.dumps({"baseline": base, "optimized": opt}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
