---
name: dtc-site-beachcamera-com-02b60e670104
site: https://www.beachcamera.com
---

## When To Use

Use this skill to find product candidates on Beach Camera (`www.beachcamera.com`). The site is a Shopify-style catalog where search is useful, including for sold-out products.

## Minimal Successful Path

1. Go directly to the durable search URL on the `www` host:
   - `https://www.beachcamera.com/search?q=<url-encoded-query>`
   - Build the query from the exact product title, brand, and especially model numbers or numeric identifiers.
2. Inspect the search results DOM for product cards and product links.
3. Prioritize exact model-number/title matches before broader brand/category matches.
4. Open the matching `/products/...` URL from the search result, or extract the href directly if UI clicking fails.
5. If using the homepage instead, use the main search box only to reach autocomplete/search results; open the result href directly when needed.

## Do Not Do

- Do not keep searching or browsing the non-`www` surface (`https://beachcamera.com`); it redirects to `https://www.beachcamera.com/`.
- Do not start by directly navigating to guessed product-page URLs; direct product-page entry may trigger Cloudflare verification.
- Do not depend on clicking autocomplete results; the autocomplete may show the correct product but not navigate reliably. Extract/open the href instead.
- Do not over-expand into broad brand searches unless exact model/identifier search fails.
- Do not treat all same-brand search results as useful; results may include unrelated products.
- Do not treat sold-out or price-unavailable status as search failure; sold-out product pages/results can still be valid candidates.

## Product Discovery Shortcuts

- Search URL pattern:
  - `https://www.beachcamera.com/search?q=<query-with-plus-signs-or-url-encoding>`
- Exact model numbers and manufacturer identifiers work well in search.
- Search results can expose useful product data without needing a product-page load:
  - title
  - product URL
  - brand
  - sold-out/availability status
  - image URL
- Product URLs usually follow:
  - `https://www.beachcamera.com/products/<slug>`
  - Search-result links may include tracking params such as `_pos`, `_sid`, `_ss`; these are not required for identity.

## Verification Hints

Verify candidates using:

- Exact title/model or identifier match.
- Brand shown on result or product page.
- Site SKU when present on the product page.
- Availability status, including sold out / price unavailable.
- Condition words in title such as open box, refurbished, or warranty variants.