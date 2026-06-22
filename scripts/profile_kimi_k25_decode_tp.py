#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2026, Advanced Micro Devices, Inc. All rights reserved.
"""Profile full Kimi K2.5 model decode with ATOM ModelRunner.

This script is intentionally a thin harness around the production ATOM
``ModelRunner`` path: it loads the model, allocates/binds KV cache, builds real
``ScheduledBatch`` decode inputs, runs a few decode steps, and exports a torch
profiler chrome trace per TP rank.

Example:
  ATOM_ENABLE_HIP_MLA_QKVA=1 torchrun --nproc_per_node=4 --master-addr=127.0.0.1 \
    --master-port=29610 -- scripts/profile_kimi_k25_decode_tp.py \
    --model /path/to/kimi-k2.5 --trace-dir /tmp/kimi_prezero --batch-size 4
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import time
from pathlib import Path

import numpy as np
import torch
import torch.profiler as profiler

from atom.config import CompilationConfig, CompilationLevel, Config
from atom.model_engine.model_runner import ModelRunner
from atom.model_engine.scheduler import ScheduledBatch
from atom.model_engine.sequence import Sequence, SequenceStatus, SequenceType
from atom.sampling_params import SamplingParams


def rank_info() -> tuple[int, int, int]:
    return (
        int(os.environ.get("RANK", "0")),
        int(os.environ.get("WORLD_SIZE", "1")),
        int(os.environ.get("LOCAL_RANK", os.environ.get("RANK", "0"))),
    )


def is_rank0() -> bool:
    rank, _, _ = rank_info()
    return rank == 0


def barrier() -> None:
    if torch.distributed.is_available() and torch.distributed.is_initialized():
        torch.distributed.barrier()


def make_config(args: argparse.Namespace) -> Config:
    min_batched_tokens = args.batch_size
    if args.prefill_cache:
        min_batched_tokens = max(min_batched_tokens, args.batch_size * args.context_len)
    min_model_len = args.context_len + args.warmup_steps + args.decode_steps + 2
    compile_level = {
        "none": CompilationLevel.NO_COMPILATION,
        "piecewise": CompilationLevel.PIECEWISE,
    }[args.compile_level]
    cfg = Config(
        model=args.model,
        trust_remote_code=args.trust_remote_code,
        max_num_batched_tokens=max(args.max_num_batched_tokens, min_batched_tokens),
        max_num_seqs=args.batch_size,
        max_model_len=max(args.max_model_len, min_model_len),
        gpu_memory_utilization=args.gpu_memory_utilization,
        tensor_parallel_size=args.tp_size,
        enforce_eager=args.enforce_eager,
        kv_cache_block_size=args.block_size,
        kv_cache_dtype=args.kv_cache_dtype,
        load_dummy=args.load_dummy,
        master_addr=args.master_addr,
        port=args.master_port,
        torch_profiler_dir=None,
        mark_trace=True,
        compilation_config=CompilationConfig(
            level=compile_level,
            cudagraph_capture_sizes=[args.batch_size],
        ),
    )
    cfg.parallel_config.data_parallel_master_ip = args.master_addr
    cfg.parallel_config.data_parallel_base_port = args.master_port
    cfg.parallel_config.data_parallel_master_port = args.master_port
    return cfg


def allocate_runner_kv(runner: ModelRunner, requested_blocks: int) -> None:
    # get_num_blocks() also initializes runner-side derived KV attributes
    # such as max_per_req_cache_slots. Keep that setup even when a benchmark
    # passes an explicit block count.
    block_info = runner.get_num_blocks()
    if requested_blocks > 0:
        num_blocks = requested_blocks
    else:
        num_blocks = int(block_info["num_kvcache_blocks"])
    runner.allocate_kv_cache(num_blocks)


def make_decode_sequences(
    *,
    batch_size: int,
    context_len: int,
    block_size: int,
    vocab_size: int,
) -> list[Sequence]:
    sampling = SamplingParams(temperature=0.0, top_k=-1, top_p=1.0)
    seqs: list[Sequence] = []
    next_block = 0
    for i in range(batch_size):
        tokens = [int((i * 997 + j) % vocab_size) for j in range(context_len)]
        seq = Sequence(
            tokens,
            block_size=block_size,
            sampling_params=sampling,
            id=10000 + i,
        )
        seq.status = SequenceStatus.RUNNING
        seq.type = SequenceType.DECODE
        seq.is_first_decode = False
        seq.num_cached_tokens = max(0, context_len - 1)
        nblocks = (context_len + block_size - 1) // block_size
        seq.block_table = list(range(next_block, next_block + nblocks))
        next_block += nblocks
        seqs.append(seq)
    return seqs


def maybe_append_block(seq: Sequence, next_block: int) -> int:
    needed_blocks = (seq.num_tokens + seq.block_size - 1) // seq.block_size
    if len(seq.block_table) < needed_blocks:
        seq.block_table.append(next_block)
        return next_block + 1
    return next_block


def make_decode_batch(seqs: list[Sequence]) -> ScheduledBatch:
    seq_map = {seq.id: seq for seq in seqs}
    return ScheduledBatch(
        seqs=seq_map,
        num_scheduled_tokens=[1] * len(seqs),
        total_tokens_num=len(seqs),
        total_tokens_num_decode=len(seqs),
        total_seqs_num=len(seqs),
        total_seqs_num_decode=len(seqs),
    )


def make_prefill_batch(seqs: list[Sequence]) -> ScheduledBatch:
    num_tokens = [seq.num_tokens for seq in seqs]
    total = int(sum(num_tokens))
    return ScheduledBatch(
        seqs={seq.id: seq for seq in seqs},
        num_scheduled_tokens=num_tokens,
        total_tokens_num=total,
        total_tokens_num_prefill=total,
        total_seqs_num=len(seqs),
        total_seqs_num_prefill=len(seqs),
    )


def advance_sequences(
    seqs: list[Sequence],
    *,
    step: int,
    vocab_size: int,
    next_block: int,
) -> int:
    for seq in seqs:
        token = int((seq.id * 104729 + step) % vocab_size)
        seq.append_token(token)
        next_block = maybe_append_block(seq, next_block)
    return next_block


def prefill_cache(
    runner: ModelRunner,
    seqs: list[Sequence],
    *,
    vocab_size: int,
    next_block: int,
) -> int:
    with torch.profiler.record_function("unprofiled_prefill_cache"):
        runner.forward(make_prefill_batch(seqs))
    next_block = advance_sequences(
        seqs, step=-1, vocab_size=vocab_size, next_block=next_block
    )
    torch.cuda.synchronize(runner.device)
    barrier()
    return next_block


def run_decode_steps(
    runner: ModelRunner,
    seqs: list[Sequence],
    *,
    steps: int,
    vocab_size: int,
    next_block: int,
    label: str,
    advance: bool = True,
) -> int:
    for step in range(steps):
        batch = make_decode_batch(seqs)
        with torch.profiler.record_function(f"{label}_decode_step_{step}"):
            runner.forward(batch)
        if advance:
            next_block = advance_sequences(
                seqs, step=step, vocab_size=vocab_size, next_block=next_block
            )
    torch.cuda.synchronize(runner.device)
    barrier()
    return next_block


def export_trace(prof: profiler.profile, trace_dir: Path, name: str, rank: int) -> Path:
    trace_dir.mkdir(parents=True, exist_ok=True)
    json_path = trace_dir / f"{name}_rank{rank}.json"
    gz_path = trace_dir / f"{name}_rank{rank}.json.gz"
    prof.export_chrome_trace(str(json_path))
    with open(json_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
        while chunk := src.read(64 * 1024 * 1024):
            dst.write(chunk)
    json_path.unlink()
    return gz_path


def write_run_meta(args: argparse.Namespace, trace_dir: Path) -> None:
    if not is_rank0():
        return
    meta = {
        "model": args.model,
        "mode": args.name,
        "tp_size": args.tp_size,
        "batch_size": args.batch_size,
        "context_len": args.context_len,
        "decode_steps": args.decode_steps,
        "warmup_steps": args.warmup_steps,
        "prefill_cache": args.prefill_cache,
        "static_decode_length": args.static_decode_length,
        "env": {
            "ATOM_ENABLE_HIP_MLA_QKVA": os.environ.get("ATOM_ENABLE_HIP_MLA_QKVA", ""),
            "ATOM_ENABLE_ALLREDUCE_RMSNORM_FUSION": os.environ.get(
                "ATOM_ENABLE_ALLREDUCE_RMSNORM_FUSION", ""
            ),
        },
    }
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / f"{args.name}_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Kimi K2.5 checkpoint dir")
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--name", default="kimi_decode")
    parser.add_argument("--tp-size", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--context-len", type=int, default=128)
    parser.add_argument("--decode-steps", type=int, default=10)
    parser.add_argument("--warmup-steps", type=int, default=2)
    parser.add_argument("--prefill-cache", action="store_true")
    parser.add_argument(
        "--static-decode-length",
        action="store_true",
        help="reuse the same decode sequence lengths each step; useful at max context length",
    )
    parser.add_argument("--max-model-len", type=int, default=256)
    parser.add_argument("--max-num-batched-tokens", type=int, default=256)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    parser.add_argument("--num-kvcache-blocks", type=int, default=0)
    parser.add_argument("--block-size", type=int, default=16)
    parser.add_argument("--kv-cache-dtype", default="bf16")
    parser.add_argument("--master-addr", default=os.environ.get("MASTER_ADDR", "127.0.0.1"))
    parser.add_argument("--master-port", type=int, default=int(os.environ.get("MASTER_PORT", "29610")))
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--load-dummy", action="store_true")
    parser.add_argument("--record-shapes", action="store_true")
    parser.add_argument("--enforce-eager", action="store_true")
    parser.add_argument(
        "--compile-level",
        choices=("piecewise", "none"),
        default="piecewise",
        help="use 'none' to test eager model forward inside CUDA graph capture",
    )
    parser.add_argument(
        "--capture-cudagraph",
        action="store_true",
        help="capture the decode cudagraph before profiling, matching server startup",
    )
    parser.add_argument(
        "--no-profiler",
        action="store_true",
        help="run decode steps and synchronize without torch profiler/exporting a trace",
    )
    args = parser.parse_args()

    rank, world_size, _ = rank_info()
    if world_size != args.tp_size:
        raise ValueError(f"torchrun WORLD_SIZE={world_size}, expected --tp-size={args.tp_size}")

    cfg = make_config(args)
    runner = ModelRunner(rank=rank % args.tp_size, config=cfg)
    allocate_runner_kv(runner, args.num_kvcache_blocks)
    if args.capture_cudagraph and not args.enforce_eager:
        runner.capture_cudagraph()

    vocab_size = int(getattr(runner.config.hf_config, "vocab_size"))
    initial_blocks = args.batch_size * ((args.context_len + args.block_size - 1) // args.block_size)
    seqs = make_decode_sequences(
        batch_size=args.batch_size,
        context_len=args.context_len,
        block_size=args.block_size,
        vocab_size=vocab_size,
    )
    next_block = initial_blocks

    if is_rank0():
        print(
            f"profile {args.name}: model={args.model} tp={args.tp_size} "
            f"bs={args.batch_size} ctx={args.context_len} steps={args.decode_steps} "
            f"prefill_cache={args.prefill_cache} "
            f"static_decode_length={args.static_decode_length} "
            f"enforce_eager={args.enforce_eager} "
            f"capture_cudagraph={args.capture_cudagraph} "
            f"no_profiler={args.no_profiler} "
            f"prezero={os.environ.get('ATOM_ENABLE_HIP_MLA_QKVA', '')} "
            f"direct_py={os.environ.get('ATOM_HIP_MLA_QKVA_DIRECT_PY', '')}"
        )

    if args.prefill_cache:
        next_block = prefill_cache(
            runner,
            seqs,
            vocab_size=vocab_size,
            next_block=next_block,
        )

    next_block = run_decode_steps(
        runner,
        seqs,
        steps=args.warmup_steps,
        vocab_size=vocab_size,
        next_block=next_block,
        label="warmup",
        advance=not args.static_decode_length,
    )

    barrier()
    trace_dir = Path(args.trace_dir)
    if args.no_profiler:
        start_event = torch.cuda.Event(enable_timing=True)
        end_event = torch.cuda.Event(enable_timing=True)
        torch.cuda.synchronize(runner.device)
        start = time.perf_counter()
        start_event.record(torch.cuda.current_stream(runner.device))
        run_decode_steps(
            runner,
            seqs,
            steps=args.decode_steps,
            vocab_size=vocab_size,
            next_block=next_block,
            label=args.name,
            advance=not args.static_decode_length,
        )
        end_event.record(torch.cuda.current_stream(runner.device))
        torch.cuda.synchronize(runner.device)
        barrier()
        write_run_meta(args, trace_dir)
        if is_rank0():
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            gpu_elapsed_ms = start_event.elapsed_time(end_event)
            print(
                f"decode completed without profiler: steps={args.decode_steps} "
                f"gpu_event_ms={gpu_elapsed_ms:.3f} elapsed_ms={elapsed_ms:.3f}"
            )
            try:
                from atom.models.deepseek_v2 import get_qkva_prezero_event_timings

                qkva_timings = get_qkva_prezero_event_timings()
            except Exception as exc:
                print(f"qkva_prezero_event_timing unavailable: {exc}")
                qkva_timings = []
            if qkva_timings:
                print("qkva_prezero_event_timing:")
                print(
                    "layer prezero_us window_before_wait_us wait_us "
                    "covered_by_window"
                )
                for item in qkva_timings[:16]:
                    covered = item["window_before_wait_us"] >= item["prezero_us"]
                    print(
                        f"{item['layer']:5d} "
                        f"{item['prezero_us']:10.3f} "
                        f"{item['window_before_wait_us']:21.3f} "
                        f"{item['wait_us']:7.3f} "
                        f"{int(covered)}"
                    )
        return

    with profiler.profile(
        activities=[profiler.ProfilerActivity.CPU, profiler.ProfilerActivity.CUDA],
        record_shapes=args.record_shapes,
        profile_memory=False,
        with_stack=False,
    ) as prof:
        run_decode_steps(
            runner,
            seqs,
            steps=args.decode_steps,
            vocab_size=vocab_size,
            next_block=next_block,
            label=args.name,
            advance=not args.static_decode_length,
        )
    torch.cuda.synchronize(runner.device)
    barrier()

    path = export_trace(prof, trace_dir, args.name, rank)
    write_run_meta(args, trace_dir)
    if is_rank0():
        print(f"trace written under {trace_dir}")
    barrier()
    runner.exit()


if __name__ == "__main__":
    main()
