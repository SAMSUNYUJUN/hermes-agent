# TikTok SKU Product-Detail Tool

## 2026-05-12

- Added `dev/price_comparison_requirements.txt` with the required internal
  dependencies: `bytedtbase`, `bytedeuler`, `python-snappy`, and `zstandard`.
  These are intentionally not in `pyproject.toml` because the internal
  Bytedance dependency chain conflicts with Hermes' public `all` extra lock
  resolution through `daytona`/`urllib3`/`pyjwt`.
- Added `tools/price_comparison_sku_tool.py`.
- Registered `tiktok_sku_lookup` in the `tiktok_sku` toolset and the
  shared Hermes core tool list.
- The tool accepts a numeric TikTok `sku_id`.
- Full raw feature dicts are cached at:
  `$HERMES_HOME/cache/price_comparison_sku_features/<sku_id>.json`.
- The agent-facing response intentionally includes only:
  `sku_id`, `title`, `description`, and `sku_images`.
- Purpose: extract TikTok-side product title, description, and SKU images for
  later same-item matching against other websites. The tool does not perform
  price comparison and does not decide whether products match.
- The fetch module defaults to:
  `/mnt/bn/zhangwendong-nas06/xianyang/agent_price_comparison/util/fetch_index_feature.py`.
  It can be overridden with `PRICE_COMPARISON_FETCH_INDEX_FEATURE`.
