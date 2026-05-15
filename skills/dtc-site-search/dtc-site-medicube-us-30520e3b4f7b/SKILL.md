---
name: dtc-site-medicube-us-30520e3b4f7b
site: https://medicube.us
---

## When To Use

Use this skill to find official Medicube US product pages on `medicube.us`, especially for skincare product names, kits, duos, or bundles that may be listed under component products instead of the marketplace bundle title.

## Minimal Successful Path

1. Query the Shopify catalog JSON directly:
   - `https://medicube.us/products.json?limit=250&page=1`
   - Continue with `page=2`, `page=3`, etc. until a page returns no products.
2. Filter catalog products using distinctive title, ingredient, line, product-form, and size terms from the source item.
3. Prefer the normal retail product handle; exclude subscription, gift, and free-gift clones.
4. Open the matching product page:
   - `https://medicube.us/products/{product-slug}`
5. Verify with the product JSON endpoint:
   - `https://medicube.us/products/{product-slug}.js`
6. If the item is a duo, kit, set, or bundle and no official bundle product exists, search/filter for each component product separately and keep each official product page as a separate candidate.

## Do Not Do

- Do not use storefront search or category browsing first; the Shopify catalog JSON is more direct and avoids wasted clicks.
- Do not rely on an external/generated DTC search tool for this site; direct catalog JSON is sufficient.
- Do not choose `[Subscr.]`, `[GIFT]`, free-gift, or other clone products as the primary retail page.
- Do not keep searching an exact marketplace bundle/duo title after it returns no useful catalog match; switch to component product searches.
- Do not assume marketplace wording is the official Medicube US product title; Medicube may use a shorter or different official product name.
- Do not repeatedly click a search-result title if it fails to navigate in the browser snapshot; use or reconstruct the `/products/{product-slug}` URL directly.
- Do not over-interpret search-result imagery; it may show bundles, variants, or alternate styling that differs from the default product page.
- Do not confuse product-page add-on/bundle options with the marketplace bundle unless the component names match.

## Product Discovery Shortcuts

- Product catalog endpoint:
  - `https://medicube.us/products.json?limit=250&page=N`
- Product pages use the durable pattern:
  - `https://medicube.us/products/{product-slug}`
- Product JSON verification endpoint:
  - `https://medicube.us/products/{product-slug}.js`
- Filter catalog JSON by concrete product-name, ingredient, line, product-form, and size terms.
- Exact product-name matching works well for single products.
- For marketplace kits, duos, sets, or bundles, search/filter the component names individually and keep each official product page as a separate candidate if no official bundle page exists.

## Verification Hints

On the product page or `.js` product JSON, confirm candidate identity using:

- Official product title
- Size or volume
- Default option/variant
- Availability
- Product type
- Tags
- Key ingredient callouts
- Product description claims
- Packaging text shown in page images
- Whether any displayed bundle/add-on option contains the same components or a different product combination