---
name: dtc-site-gamechest-gg-72328a0e1daf
site: https://gamechest.gg
---

## When To Use

Use this skill to search GameChest (`gamechest.gg`) for DTC product candidates. The site is a small Shopify catalog focused on Nintendo/Switch games and gaming accessories/controllers.

## Minimal Successful Path

1. Go directly to the Shopify catalog JSON:
   - `https://www.gamechest.gg/products.json?limit=250`
2. Enumerate the returned products; the catalog is small enough to inspect in one page.
3. Match against product `title`, `handle`, `vendor`, `product_type`, and `tags`.
4. For any plausible candidate, open its product page:
   - `https://www.gamechest.gg/products/<handle>`
5. If the catalog response looks incomplete, cross-check:
   - `https://www.gamechest.gg/collections/all/products.json?limit=250`
6. If no catalog title/handle/tag match exists, verify absence with predictive search:
   - `https://www.gamechest.gg/search/suggest.json?q=<term>&resources[type]=product&resources[limit]=10`

## Do Not Do

- Do not rely on the visual site search page as the primary route.
- Do not treat products displayed beneath a “No results found” search message as query matches; they may be generic/default listings.
- Do not keep browsing unrelated collection grids after the JSON catalog has been enumerated.
- Do not use loose description-only keyword hits as candidates; game descriptions can contain misleading words unrelated to the target product category.
- Do not assume GameChest carries broad electronics or Apple/audio products; verify through the Shopify catalog.

## Product Discovery Shortcuts

- Best entry point: `https://www.gamechest.gg/products.json?limit=250`
- Backup all-products endpoint: `https://www.gamechest.gg/collections/all/products.json?limit=250`
- Predictive search endpoint:
  - `https://www.gamechest.gg/search/suggest.json?q=<term>&resources[type]=product&resources[limit]=10`
- Product URL pattern:
  - `https://www.gamechest.gg/products/<handle>`
- Observed catalog size is small, so full JSON enumeration is faster and safer than UI browsing.

## Verification Hints

- Confirm candidate relevance from the product page title and Shopify handle.
- Check variant/title fields in JSON when the product page has multiple options.
- If site search says “No results found,” ignore any unrelated products shown below that message unless they also appear as actual JSON/search matches.
- If no relevant title/handle/tag appears in the full catalog JSON, treat the site as having no useful candidate for that target.