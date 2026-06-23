# Prezero Upstreaming TODO

## Accepted / Execute

- [x] Rename the generic fused AR+RMS side-effect buffer from `gemm_zero` to
  `zero_fill` in the AITER custom-allreduce API. Keep MLA-local variable names
  separate when they describe a specific qkv_a buffer.
- [x] Avoid passing zero-fill side effects through the normal `RMSNorm.forward()`
  call. Add an explicit prezero helper for the DeepSeek input layernorm path.
- [x] Split PRs by ownership:
  - AITER: prezero GEMM, zero-fill RMSNorm producers, custom allreduce zero-fill
    support, and kernel tests.
  - ATOM: Kimi MLA qkv_a integration and model-level gating.
- [x] Move qkva prezero shape dispatch out of ATOM: call AITER `tgemm_prezero()`
  directly and let it fall back to ordinary tuned GEMM on CSV miss.

## Pending / Decide Later

- [ ] qkva gate policy with input RMSNorm quant fusion:
  - Option A: when qkva is enabled and shape/dtype gates match, disable input
    RMSNorm quant fusion automatically.
  - Option B: add a qkva path that accepts quantized input + scale.
- [ ] `tgemm_prezero` return contract:
  - Keep current `out = tgemm_prezero(C, A, B)` alias/fresh fallback behavior, or
  - return `(out, hit)` so callers can reason explicitly about fallback.
- [ ] Rename `out_hidden_dim` to a clearer generic name such as
  `out_last_dim` / `output_width`.
- [ ] Refactor repeated custom-allreduce zero-fill launch bookkeeping into a
  helper/macro such as `append_zero_fill_blocks`.

## Validation Before PR

- [ ] Run AITER op tests after clearing JIT cache:
  `op_tests/test_tgemm_prezero.py` and `op_tests/test_rmsnorm_prezero.py`.
- [ ] Re-run ATOM Kimi accuracy with qkva on/off.
- [ ] Re-run fixed-length conc4 performance with `RANDOM_RANGE_RATIO=1`.
- [ ] Capture one qkva=1 torch profiler trace and verify the prezero kernels are
  present.
