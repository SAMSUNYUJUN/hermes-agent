---
name: dtc-site-search-index
description: Index of available DTC independent-site search strategy skills.
version: 1.0.0
metadata:
  hermes:
    category: dtc-site-search
    tags: [dtc, product-search, skill-index]
---

# DTC Site Search Skill Index

## When To Use

Load this skill when the user provides a TikTok SKU and an independent-site URL. It tells you which site-specific DTC search strategy skills are available.

## Search Order

1. Call `tiktok_sku_lookup` for the SKU evidence.
2. Call `dtc_site_search_context(site_url)`. If it returns `has_tool=true`, call `dtc_site_search_tool(site_url, query, expected_terms)` first and use its structured candidates/evidence.
3. If the generated tool returns `success=true`, do not load the site skill; answer from the tool output and record `tool_success=true`. Only if the generated tool returns `success=false`, and `has_skill=true`, call `skill_view(name=skill_view_name)` before browsing.
4. If a site skill is loaded, follow its `Minimal Successful Path` exactly. That path may start at a redirect target, catalog host, category URL, or product listing URL instead of the user-provided URL.
5. Do not repeat any route, click, search box, snapshot pattern, or broad exploration listed in the loaded skill's `Do Not Do` section.
6. Only when neither generated tool nor site skill exists, use `browser_navigate` on the user's DTC URL, then inspect with `browser_snapshot`, `browser_click`, `browser_type`, and `browser_press`.
7. Only use web search after direct browser exploration fails or reveals that the product catalog lives on a related domain.

## Available Site Skills

- `https://beachcamera.com` -> `dtc-site-beachcamera-com-02b60e670104` (success_count=2, path=`/mnt/bn/zhangwendong-nas06/YujunSun/hermes-agent/skills/dtc-site-search/dtc-site-beachcamera-com-02b60e670104/SKILL.md`)
- `https://gamechest.gg` -> `dtc-site-gamechest-gg-72328a0e1daf` (success_count=3, path=`/mnt/bn/zhangwendong-nas06/YujunSun/hermes-agent/skills/dtc-site-search/dtc-site-gamechest-gg-72328a0e1daf/SKILL.md`)
- `https://halara.com` -> `dtc-site-halara-com-7736ca2c1913` (success_count=2, path=`/mnt/bn/zhangwendong-nas06/YujunSun/hermes-agent/skills/dtc-site-search/dtc-site-halara-com-7736ca2c1913/SKILL.md`)
- `https://medicube.us` -> `dtc-site-medicube-us-30520e3b4f7b` (success_count=3, path=`/mnt/bn/zhangwendong-nas06/YujunSun/hermes-agent/skills/dtc-site-search/dtc-site-medicube-us-30520e3b4f7b/SKILL.md`)
- `https://microingredients.com` -> `dtc-site-microingredients-com-05f171b8850a` (success_count=2, path=`/mnt/bn/zhangwendong-nas06/YujunSun/hermes-agent/skills/dtc-site-search/dtc-site-microingredients-com-05f171b8850a/SKILL.md`)
- `https://planetbeauty.com` -> `dtc-site-planetbeauty-com-684e810a4b7f` (success_count=2, path=`/mnt/bn/zhangwendong-nas06/YujunSun/hermes-agent/skills/dtc-site-search/dtc-site-planetbeauty-com-684e810a4b7f/SKILL.md`)

## Visual Matching Reminder

When the user asks whether the TikTok SKU and a site candidate are the same item, use vision to compare product images if both sides have images. If either side has no image, skip visual comparison.
