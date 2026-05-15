# DTC Site Search Skill/Tool Rollout Architecture

## 背景与目标

Hermes 的 DTC site search 会在多次成功搜索后沉淀站点级搜索策略：

- 先把成功搜索记录清洗成可复用经验。
- 达到 N 次成功记录后生成一个 site skill。
- 有 skill 后，每新增 X 次成功记录就 review skill 是否需要修改。
- 连续 Y 次 review 都不修改 skill，说明 skill 基本稳定，再生成站点专用 search tool。

在线上部署后，同一个 DTC site 可能同时有多个搜索任务在跑。系统必须保证：

1. 并发搜索任务不会互相阻塞。
2. skill/tool 更新不会污染正在执行的搜索任务。
3. 新 skill/tool 不能生成后立刻全量上线，必须经过 A/B 测试。
4. A/B 测试通过后还有明确上线环节，且上线对象要有版本控制。
5. agent 在需要 skill/tool 时能立刻看到最新“已发布版本”，但不会读到半成品。
6. tool 失败后可 fallback 到 skill，并把失败 case 用于后台 repair，但 repair 后仍需重新测试再上线。

本文描述最终推荐架构，不要求一次性实现完所有模块。现有逻辑可以作为最小版本继续迭代。

## 核心原则

### 读写分离

前台搜索任务只读取已发布版本：

- `published skill`
- `published tool`
- `published site index`

后台学习任务只能写候选版本：

- `candidate skill`
- `candidate tool`
- `candidate evaluation report`

候选版本不能被主 agent 自动使用，除非通过 A/B 测试并完成 publish。

### 版本不可变

每个 skill/tool 版本一旦生成，不再原地修改。修改会产生新版本。

推荐版本号格式：

```text
v<unix_ms>-<short_hash>
```

示例：

```text
skills/dtc-site-search/dtc-site-revolve-com-d2ddbcf72258/versions/v1770000000000-a1b2c3/SKILL.md
dtc_site_search_data/revolve-com-d2ddbcf72258/generated_tool/versions/v1770000000000-d4e5f6/site_search_tool.mjs
```

发布指针单独维护：

```json
{
  "site_key": "revolve-com-d2ddbcf72258",
  "published_skill_version": "v1770000000000-a1b2c3",
  "published_tool_version": "v1770000000000-d4e5f6"
}
```

前台只读发布指针指向的版本。

### 原子发布

候选版本写入完成后，不能直接覆盖线上文件。发布只做一次原子指针切换：

1. 写 candidate 文件到临时路径。
2. 完成语法检查、运行测试、A/B 测试。
3. 写 publish manifest 的临时文件。
4. `rename`/`replace` 原子切换 manifest。

这样正在运行的任务要么读旧版本，要么读新版本，不会读到半写入文件。

### 单站点串行写，多任务并发读

同一个 `site_key` 的后台写操作需要串行：

- skill create/update
- tool generation
- tool repair
- publish

不同 `site_key` 可以并发。

读操作不加重锁，只读取已发布 manifest。必要时用短 TTL cache。

## 角色划分

### Foreground Search Agent

负责用户请求下的实际搜索。

流程：

1. 调用 `tiktok_sku_lookup` 获取 SKU 信息。
2. 调用 `dtc_site_search_context(site_url)`。
3. 如果 `has_tool=true`，先调用 `dtc_site_search_tool`。
4. 如果 tool 成功返回候选，继续做 same-item 判断，不加载 skill。
5. 如果 tool 失败或无候选，fallback 到已发布 skill。
6. 如果 skill 也没有，再做原始探索。
7. 搜索结束调用 `dtc_site_search_record`。

Foreground agent 不应该生成、修改或发布 skill/tool。

### Background Learner

负责把搜索记录转成候选 skill/tool。

输入：

- raw search record
- cleaned search chain
- existing published skill/tool
- historical evaluation reports

输出：

- candidate skill version
- candidate tool version
- evaluation report
- publish proposal

### Evaluator / A-B Runner

负责评估候选版本是否值得上线。

它不直接上线，只产出结构化报告：

```json
{
  "site_key": "revolve-com-d2ddbcf72258",
  "candidate_type": "skill",
  "candidate_version": "v...",
  "baseline_version": "v...",
  "cases": [...],
  "pass": true,
  "metrics": {
    "same_output_rate": 1.0,
    "token_reduction_rate": 0.42,
    "latency_delta_ms": -800,
    "tool_call_reduction": 3
  },
  "decision_reason": "Candidate preserved output and reduced tokens."
}
```

### Publisher

负责把通过评估的候选版本切换为 published 版本。

Publisher 是唯一能修改发布指针的组件。

## 数据模型

### Site State

运行状态仍可保留在：

```text
dtc_site_search_data/<site_key>/state.json
```

建议拆分逻辑字段：

```json
{
  "site_url": "https://revolve.com",
  "site_key": "revolve-com-d2ddbcf72258",
  "success_count": 6,
  "last_skill_update_success_count": 6,
  "stable_skill_update_count": 2,
  "published": {
    "skill_version": "v...",
    "tool_version": "v..."
  },
  "candidates": {
    "skill_version": "v...",
    "tool_version": "v..."
  },
  "jobs": {
    "skill_review": {
      "status": "idle",
      "job_id": null
    },
    "tool_generation": {
      "status": "idle",
      "job_id": null,
      "started_at": null,
      "last_error": null
    }
  }
}
```

`state.json` 可以是运行时状态，不一定进入 git。

### Published Manifest

发布指针应进入 git 或集中配置存储：

```text
dtc_site_search_data/generated_tools_index.json
skills/dtc-site-search-index/SKILL.md
```

未来建议新增：

```text
dtc_site_search_data/published_sites.json
```

示例：

```json
{
  "sites": {
    "revolve-com-d2ddbcf72258": {
      "site_url": "https://revolve.com",
      "published_skill_version": "v1770000000000-a1b2c3",
      "published_tool_version": "v1770000000000-d4e5f6",
      "updated_at": "2026-05-14T02:35:25Z"
    }
  }
}
```

### Candidate Metadata

每个候选版本都应有元数据：

```json
{
  "version": "v1770000000000-d4e5f6",
  "site_key": "revolve-com-d2ddbcf72258",
  "type": "tool",
  "created_at": "2026-05-14T02:35:25Z",
  "source_records": [
    "20260514T023251Z-4ead3f7c"
  ],
  "base_skill_version": "v1770000000000-a1b2c3",
  "status": "candidate",
  "evaluation_report": null
}
```

## 现有 N/X/Y 逻辑如何接入

### N: 生成初始 Skill

当前逻辑：

- 同一 site 成功搜索记录达到 `N=2` 后，生成 skill。

推荐线上语义：

1. 达到 N 后生成 `candidate skill v1`。
2. 用最近 N 条成功 case 做离线验证。
3. 如果 candidate skill 的搜索输出和 baseline 原始探索一致，并降低 token/工具调用，则 publish。
4. publish 后前台任务才能看到 `has_skill=true`。

Baseline 可以是：

- 不使用 skill 的原始搜索策略。
- 或上一版 published skill。

### X: Skill Review Window

当前逻辑：

- 有 skill 后，每 X 次成功记录触发一次 review。
- 默认 `X=1`。

推荐线上语义：

1. 每新增 X 条成功记录，后台创建 `skill_review` job。
2. review 先判断是否有流程级错误。
3. 没有原则性错误则不生成新版本，只增加 `stable_skill_update_count`。
4. 有流程级错误才生成 `candidate skill v_next`。
5. 新 candidate skill 必须 A/B 测试通过后 publish。

关键点：

- review 不应该因为措辞、格式、小优化产生新版本。
- 只有搜索流程确实错误或漏了稳定路径时才产生 candidate。

### Y: Tool Generation Stable Updates

当前逻辑：

- 连续 Y 次 skill review 未修改 skill 后，生成 tool。
- 默认 `Y=2`。

推荐线上语义：

1. `stable_skill_update_count >= Y` 时创建 `tool_generation` job。
2. tool 生成后先是 `candidate tool`。
3. 运行 smoke test：
   - Node 可执行。
   - 输入/输出 JSON 协议正确。
   - 不 login、不 checkout、不写入外部站点。
   - bounded timeout。
4. 运行 A/B test：
   - 同一批 test cases 上，tool 输出候选与 skill 路线输出候选一致或等价。
   - token 消耗低于 skill baseline。
   - latency/tool calls 不显著恶化。
5. 通过后 publish tool。
6. 前台再看到 `has_tool=true`。

## A/B 测试设计

### 触发时机

每个候选版本生成后触发 A/B：

- candidate skill vs baseline no-skill 或 old skill
- candidate tool vs published skill
- repaired tool vs previous published tool + skill fallback

### 测试输入

测试 case 应来自真实历史搜索，而不是人工构造的 `"self test"`。

每个 case 包含：

```json
{
  "sku_id": "1731948310021902592",
  "site_url": "https://revolve.com",
  "query": "Marc Jacobs The Suede Small Tote Bag Copper MARJ-WY828",
  "expected_terms": ["Marc Jacobs", "Suede Small Tote", "MARJ-WY828"],
  "accepted_candidate_urls": [
    "https://www.revolve.com/.../dp/MARJ-WY828/"
  ],
  "baseline_trace": "...",
  "baseline_final_answer": "..."
}
```

### 输出等价判定

“输出相同”不应要求文本逐字一致，应使用结构化等价：

- candidate URL 集合相同或包含 baseline 最终候选。
- 关键 evidence 一致：
  - title/brand/style number/image URL/description snippet。
- final same-item 判断不变。
- 如果 baseline 是“未找到”，candidate 也不能凭空返回无关候选。

推荐判定：

```text
pass if:
  same_item_decision_equal
  AND candidate_recall >= baseline_recall
  AND no new high-confidence false positive
```

### 成本指标

记录每个分支：

- input tokens
- output tokens
- API call count
- browser/tool call count
- elapsed time
- fallback count
- failure count

上线门槛示例：

```text
same_output_rate >= 0.95
false_positive_rate == 0
token_reduction_rate >= 0.20
hard_failure_rate <= baseline_hard_failure_rate
```

对于工具：

```text
tool_candidate_recall >= skill_baseline_recall
AND token_reduction_rate >= 0.50
AND median_latency <= skill_baseline_latency
```

## 并发与锁设计

### 锁粒度

推荐每个 site 一个写锁：

```text
dtc_site_search_data/<site_key>/.lock
```

锁保护：

- state mutation
- candidate version creation
- publish pointer update
- pending failure queue mutation

不保护：

- 前台读取 published manifest
- 前台执行 published tool
- A/B runner 读取 immutable candidate files

### Job Deduplication

每类 job 需要幂等键：

```text
<site_key>:skill_review:<last_success_record_id>
<site_key>:tool_generation:<published_skill_version>:<stable_count>
<site_key>:tool_repair:<failure_hash>:<fallback_record_id>
```

如果已有同 key job 正在运行，不重复启动。

### 防止阻塞

后台 job 必须满足：

- 所有 LLM 调用有硬 timeout。
- 所有 Node/script 执行有 timeout。
- 外部 fetch 有 timeout。
- job 失败只写状态，不阻塞 foreground search。
- foreground search 不等待 skill/tool 生成完成。

### 防止旧任务覆盖新任务

每个 job 启动时记录 base version：

```json
{
  "base_skill_version": "v1",
  "base_tool_version": "v3"
}
```

发布前重新检查：

- 当前 published version 是否仍等于 base version。
- 如果不一致，candidate 需要重新评估或丢弃。

这可以避免旧后台任务慢返回后覆盖新版本。

## 热更新策略

### Foreground Agent 如何看到新版本

`dtc_site_search_context(site_url)` 不应把所有网站 tool 都塞进 prompt。

它应该：

1. 规范化 `site_url`。
2. 查 published manifest。
3. 只返回当前 site 的：
   - `has_skill`
   - `skill_name`
   - `skill_version`
   - `has_tool`
   - `tool_name`
   - `tool_version`
   - `tool_intro`

这样主 agent 每次处理一个 site 前都会读取最新发布指针。

### 运行中任务是否切换版本

推荐：一个 foreground search task 在开始时绑定版本，整个任务内不切换。

例如：

```json
{
  "site_key": "revolve-com-d2ddbcf72258",
  "skill_version": "v10",
  "tool_version": "v5"
}
```

理由：

- 保证 trace 可复现。
- 避免同一个任务前半段用旧 skill，后半段用新 tool。
- 新版本对下一个任务立刻生效即可。

## Tool 失败与 Repair 流程

### 正常调用

1. `dtc_site_search_context` 返回 `has_tool=true`。
2. agent 调 `dtc_site_search_tool`。
3. tool 返回候选：
   - 成功。
   - 清空该 site 的旧 pending failures。
   - 不加载 skill。
4. tool 失败或无候选：
   - 记录 pending failure。
   - 返回 fallback instruction。
   - agent 加载 published skill。
   - 搜索完成后 record 成功 case。

### Repair 触发

只有同时满足以下条件才 repair：

- 有 pending tool failure。
- 后续 fallback skill 搜索成功。
- 成功 case 的 site 与 failure 的 site 相同。
- failure 仍对应当前 published tool version。

Repair 生成的是 `candidate repaired tool`，不能直接覆盖 published tool。

Repair 后也必须：

1. smoke test。
2. A/B test against fallback-success case。
3. publish。

## Git 与版本控制

应进入 git 的内容：

- generated tool source code
- generated tool index / publish manifest
- generated skill versions
- evaluation reports
- migration/config docs

不应进入 git 的内容：

- raw search records
- cleaned records with possible user/session details
- transient state
- lock files
- temporary job files
- local Node runtime `.hermes-node/`

当前 `.gitignore` 已应允许：

```text
dtc_site_search_data/generated_tools_index.json
dtc_site_search_data/<site_key>/generated_tool/**
```

并继续忽略：

```text
dtc_site_search_data/<site_key>/raw/**
dtc_site_search_data/<site_key>/cleaned/**
dtc_site_search_data/<site_key>/state.json
```

## 推荐目录结构

```text
dtc_site_search_data/
  generated_tools_index.json
  published_sites.json
  <site_key>/
    state.json                    # runtime only, ignored by git
    raw/                          # ignored
    cleaned/                      # ignored
    generated_tool/
      current -> versions/v.../   # optional symlink or manifest pointer
      versions/
        v1770000000000-d4e5f6/
          site_search_tool.mjs
          metadata.json
          evaluation.json
```

Skill:

```text
skills/dtc-site-search/
  dtc-site-revolve-com-d2ddbcf72258/
    SKILL.md                      # optional compatibility copy of published
    versions/
      v1770000000000-a1b2c3/
        SKILL.md
        metadata.json
        evaluation.json
```

## 状态机

### Skill Candidate

```text
not_enough_data
  -> candidate_generating
  -> candidate_generated
  -> ab_testing
  -> approved
  -> published

failure states:
  -> generation_failed
  -> ab_failed
  -> superseded
```

### Tool Candidate

```text
not_stable
  -> generating
  -> smoke_testing
  -> ab_testing
  -> approved
  -> published

failure states:
  -> generation_failed
  -> smoke_test_failed
  -> ab_failed
  -> superseded
```

## Implementation Roadmap

### Phase 1: Harden Current Flow

- Add per-site job status fields.
- Add hard timeout around every auxiliary LLM call.
- Write `generating` before tool generation starts.
- Write failure reason on every early return.
- Clear stale pending failures when tool succeeds.
- Keep generated tool files tracked by git.

### Phase 2: Immutable Versions

- Stop overwriting `SKILL.md` and `site_search_tool.mjs` directly.
- Write new versions under `versions/<version>/`.
- Maintain compatibility copies or symlinks for current code.
- Add metadata for source records and base versions.

### Phase 3: A/B Runner

- Implement background evaluator.
- Add structured case format.
- Compare:
  - no-skill vs skill
  - skill vs tool
  - old tool vs repaired tool
- Store `evaluation.json`.
- Expose `POST /api/dtc-site-search/ab` so external load tests submit cases
  to the running Hermes process instead of creating isolated agent processes.
  This keeps provider, credential pool, model routing, and concurrency behavior
  identical to production.
- Keep `scripts/dtc_site_search_ab_runner.py` as a dataset client. For real
  rollout validation, call it with `--api-url <hermes-api-base>` and
  `--api-token <dashboard-session-token>` so candidate/baseline agents run
  inside Hermes.

### Phase 4: Publisher

- Add publish manifest.
- Add atomic publish operation.
- Add rollback operation.
- Make `dtc_site_search_context` read only published manifest.

### Phase 5: Distributed Deployment

- Replace local file locks with DB/distributed locks if multiple machines write.
- Store published manifest in strongly consistent storage.
- Ship generated tool versions through normal deployment artifact or git sync.
- Add observability:
  - generation latency
  - A/B pass rate
  - fallback rate
  - repair rate
  - token savings

## Operational Rules

1. Foreground search never blocks on generation.
2. Foreground search never reads candidate versions.
3. Same site writes are serialized.
4. Different site jobs can run in parallel.
5. A/B tests run asynchronously and can use unlimited agent/API concurrency.
6. Publish is atomic and auditable.
7. Rollback changes only the published pointer.
8. Tool repair does not overwrite published tool until evaluated.
9. A running foreground task pins versions for its own trace.
10. New published versions are visible to the next `dtc_site_search_context` call.

## Open Questions

1. A/B 的“输出相同”是否以最终 same-item 判断为准，还是以候选列表 recall 为准？
2. 每个 site 的 A/B case 数量最少需要多少？建议初期 3-5，稳定后提高。
3. 是否允许 tool 返回“低置信候选”并由 agent 判断，还是 tool 必须只返回高置信候选？
4. generated tool 是否允许依赖 Playwright？如果允许，需要 runtime dependency 和 sandbox 策略。
5. 发布是否必须走 git commit/PR，还是可由后台直接写 publish manifest？

## Recommended Default Policy

初期推荐保守策略：

- N = 2
- X = 1
- Y = 2
- candidate skill 只有流程级错误才产生新版本
- candidate tool 必须在至少 3 个历史 case 上与 skill baseline 等价
- tool 只要无候选，就 fallback skill
- repair 后不自动上线，必须重新 A/B
- 发布后只影响新任务，不影响正在运行的任务
