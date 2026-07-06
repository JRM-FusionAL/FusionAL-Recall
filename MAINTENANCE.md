# Maintenance Log

## Maintenance Run: 2026-07-01 02:32:15

### Outdated Dependencies Check
Found 31 outdated packages via PyPI API:

| Package | Current | Latest |
|---------|---------|--------|
| cuda-pathfinder | 1.5.5 | 1.5.6 |
| cuda-toolkit | 13.0.2 | 13.3.1 |
| cyclopts | 4.18.0 | 4.20.0 |
| griffelib | 2.0.2 | 2.1.0 |
| huggingface_hub | 0.36.2 | 1.21.0 |
| joserfc | 1.7.1 | 1.7.2 |
| mpmath | 1.3.0 | 1.4.1 |
| numpy | 2.4.6 | 2.5.0 |
| nvidia-cublas | 13.1.1.3 | 13.6.0.2 |
| nvidia-cuda-cccl | 13.3.3.3.1 | 13.3.3.4.1 |
| nvidia-cuda-cupti | 13.0.85 | 13.3.75 |
| nvidia-cuda-nvrtc | 13.0.88 | 13.3.33 |
| nvidia-cuda-runtime | 13.0.96 | 13.3.29 |
| nvidia-cudnn-cu13 | 9.20.0.48 | 9.23.2.1 |
| nvidia-cufft | 12.0.0.61 | 12.3.0.29 |
| nvidia-cufile | 1.15.1.6 | 1.18.1.6 |
| nvidia-curand | 10.4.0.35 | 10.4.3.29 |
| nvidia-cusolver | 12.0.4.66 | 12.2.6.9 |
| nvidia-cusparse | 12.6.3.3 | 12.8.2.51 |
| nvidia-cusparselt-cu13 | 0.8.1 | 0.9.1 |
| nvidia-nccl-cu13 | 2.29.7 | 2.30.7 |
| nvidia-nvjitlink | 13.0.88 | 13.3.33 |
| nvidia-nvshmem-cu13 | 3.4.5 | 3.7.1 |
| nvidia-nvtx | 13.0.85 | 13.3.29 |
| pydantic_core | 2.46.4 | 2.47.0 |
| regex | 2026.5.9 | 2026.6.28 |
| rpds-py | 2026.5.1 | 2026.6.3 |
| scipy | 1.17.1 | 1.18.0 |
| tokenizers | 0.22.2 | 0.23.1 |
| transformers | 4.57.6 | 5.12.1 |
| typer | 0.26.7 | 0.26.8 |

### Notes
- **Major breaking changes detected**: `huggingface_hub` 0.36.2 → 1.21.0 (incompatible with `transformers` 4.57.6 which requires `<1.0`), `transformers` 4.57.6 → 5.12.1 (major version bump)
- **CUDA stack**: Many nvidia-* packages have updates but `torch 2.12.1` pins specific versions (cuda-toolkit==13.0.2, nvidia-cublas<=13.1.1.3, nvidia-cudnn-cu13==9.20.0.48)
- **Dependency conflicts**: Updating CUDA stack packages would break torch compatibility
- **Recommended approach**: Hold CUDA stack at current versions; update only safe packages (cyclopts, griffelib, joserfc, pydantic_core, regex, rpds-py, scipy, tokenizers, typer)

### Issues
No open issues found to label.

### CI/CD
No GitHub Actions workflows configured in this repository. CI check skipped.

### Action Required
Create a focused dependency update PR for non-breaking updates only. The CUDA stack and major version bumps (huggingface_hub, transformers) require compatibility testing.

---


## Maintenance Run: 2026-07-05 02:11:14
Outdated dependencies found: 36 packages.
Dependency update skipped due to network issues (IncompleteRead errors).
Labeled new issues with 'triage': none found.
--
