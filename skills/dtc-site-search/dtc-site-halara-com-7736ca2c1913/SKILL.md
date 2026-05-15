---
name: dtc-site-halara-com-7736ca2c1913
site: https://www.halara.com
---

## When To Use

Use this skill to find product candidate pages on Halara (`https://www.halara.com/`) from a product title or distinctive apparel description.

## Minimal Successful Path

1. Open `https://www.halara.com/`.
2. Close any signup, lucky-wheel, or promotional popup before interacting with the page.
3. Open the site search from the header.
4. Search with the distinctive core product title:
   - Remove the leading brand name if present.
   - Remove hashtags, campaign text, and social/marketplace noise.
   - Keep fabric/line names, neckline/waist/style terms, inseam/length, pocket, and dress/shorts/skirt/pants terms.
5. Open the matching search result product page.
6. If search does not surface the item, go directly to the relevant collection and extract product links from the DOM; for shorts use:
   - `https://www.halara.com/collections/shorts-1`
7. Match candidate product titles from collection/search results, then open the product page.
8. On the product page, inspect structured data / JSON-LD / variant data for color, price, availability, and variant URLs.

## Do Not Do

- Do not use direct URL search as the main route:
  - `https://www.halara.com/search?q=...`
  - It can load as a mostly blank or limited page.
- Do not click the header search while a signup or lucky-wheel popup is open; close the popup first.
- Do not rely on the first loaded product image or default `currentSkc` as the desired color/variant.
- Do not manually scroll through long collection or product snapshots if DOM extraction or structured data is available.
- Do not keep broad-browsing homepage menus when a distinctive title search or relevant collection URL is available.
- Do not keep tracking parameters from search result URLs unless needed; the durable product URL pattern is enough.

## Product Discovery Shortcuts

- Product URL pattern:
  - `https://www.halara.com/products/{product-slug}?currentSkc={variant_id}`
- `currentSkc` controls the selected variant and may change color/size context.
- Useful direct collection observed:
  - Shorts: `https://www.halara.com/collections/shorts-1`
- Site search can work when using a cleaned, distinctive product title through the header search UI.
- If search fails or is blocked, collection DOM extraction is faster than visual browsing.

## Verification Hints

- Confirm the product page title closely matches the target product title after removing marketplace/social noise.
- Check variant data, not just the default page image, for the needed color.
- Use JSON-LD / structured data / embedded variant data to verify:
  - Variant URL or `currentSkc`
  - Color name
  - Price
  - In-stock sizes or availability
- Search result metadata may show useful color options, but verify variants again on the product page.