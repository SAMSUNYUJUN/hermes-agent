---
name: dtc-site-microingredients-com-05f171b8850a
site: https://microingredients.com
---

## When To Use

Use this skill to find products or bundles on the Micro Ingredients DTC Shopify site.

## Minimal Successful Path

1. Go directly to the canonical host:
   - `https://www.microingredients.com/`
2. Search with the most specific product or bundle name available, preferably by direct URL:
   - `https://www.microingredients.com/search?q=<encoded query>`
3. Open the matching product result by its title/link.
4. If browser clicks are unreliable, navigate directly to the product URL pattern:
   - `https://www.microingredients.com/products/<handle>`
5. For structured product data, append `.js` to the product URL:
   - `https://www.microingredients.com/products/<handle>.js`

## Do Not Do

- Do not keep using `https://microingredients.com` after it redirects; use `https://www.microingredients.com/` directly.
- Do not rely on homepage featured tiles or bundle merchandising; they may show related but non-identical products.
- Do not browse broad categories or search only broad terms when an exact product or bundle phrase is available.
- Do not treat individual component products as the final result when the target is a bundle.
- Do not assume search-result image clicks will navigate correctly; use the result title/link or direct product URL.
- Dismiss newsletter/SMS popups if they block search, clicks, or inspection.

## Product Discovery Shortcuts

- Search URL pattern:
  - `https://www.microingredients.com/search?q=<encoded query>`
- Product URL pattern:
  - `https://www.microingredients.com/products/<handle>`
- Shopify product JSON pattern:
  - `https://www.microingredients.com/products/<handle>.js`
- Use exact bundle/product names plus distinctive component terms in the query when available.

## Verification Hints

- Confirm the product page title matches the intended product or bundle.
- For bundles, verify the listed included products/components rather than relying only on images.
- Check product images for matching package lineup and layout, but treat images as supporting evidence.
- Use the `.js` endpoint for fast structured confirmation of title, handle, description, variants, and images.