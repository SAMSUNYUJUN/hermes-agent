const DEFAULT_SITE = "https://medicube.us";
const PAGE_LIMIT = 250;
const MAX_PAGES = 20;
const FETCH_TIMEOUT_MS = 9000;
const VERIFY_TIMEOUT_MS = 7000;

function safeJsonPrint(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function makeTrace(trace, step, data = {}) {
  trace.push({ step, ...data });
}

function normalizeText(value) {
  return String(value ?? "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function stripHtml(value) {
  return String(value ?? "")
    .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, " ")
    .replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, " and ")
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/\s+/g, " ")
    .trim();
}

function unique(arr) {
  return [...new Set(arr.filter(Boolean))];
}

const STOPWORDS = new Set([
  "the", "and", "for", "with", "from", "this", "that", "your", "you", "are",
  "was", "were", "has", "have", "had", "but", "not", "all", "new", "set",
  "kit", "duo", "bundle", "bundles", "pack", "packs", "combo", "official",
  "authentic", "medicube", "medicubeus", "skin", "care", "skincare", "product",
  "products", "plus", "more", "free", "gift"
]);

function expandToken(token) {
  const t = normalizeText(token);
  if (!t) return [];
  const out = [t];
  if (t === "turmeric") out.push("tumeric");
  if (t === "tumeric") out.push("turmeric");
  if (t === "night") out.push("overnight");
  if (t === "overnight") out.push("night");
  if (t === "vitamin") out.push("vita");
  if (t === "vita") out.push("vitamin");
  if (t === "brighten") out.push("brightening");
  if (t === "brightening") out.push("brighten");
  return out;
}

function buildSearchTerms(query, expectedTerms) {
  const rawExpected = Array.isArray(expectedTerms)
    ? expectedTerms
    : typeof expectedTerms === "string"
      ? expectedTerms.split(/[,|;]/)
      : [];

  const phrases = unique(
    [query, ...rawExpected]
      .map(normalizeText)
      .filter((x) => x && x.length >= 3)
  );

  const tokens = [];
  for (const phrase of phrases) {
    for (const part of phrase.split(/\s+/)) {
      if (part.length >= 3 && !STOPWORDS.has(part)) {
        tokens.push(...expandToken(part));
      }
    }
  }

  return {
    phrases,
    tokens: unique(tokens).filter((t) => !STOPWORDS.has(t))
  };
}

function isCloneProduct(product) {
  const hay = normalizeText([
    product.title,
    product.handle,
    product.product_type,
    Array.isArray(product.tags) ? product.tags.join(" ") : product.tags
  ].join(" "));
  return /\b(subscr|subscription|gift|free gift|freegift|sample|samples)\b/.test(hay) ||
    /^\s*\[(subscr|gift)\]/i.test(String(product.title ?? ""));
}

function productHaystack(product) {
  return normalizeText([
    product.title,
    product.handle,
    product.product_type,
    Array.isArray(product.tags) ? product.tags.join(" ") : product.tags,
    stripHtml(product.body_html),
    Array.isArray(product.variants) ? product.variants.map((v) => [v.title, v.sku, v.option1, v.option2, v.option3].join(" ")).join(" ") : ""
  ].join(" "));
}

function scoreProduct(product, terms, query) {
  const title = normalizeText(product.title);
  const handle = normalizeText(product.handle);
  const body = normalizeText(stripHtml(product.body_html));
  const tags = normalizeText(Array.isArray(product.tags) ? product.tags.join(" ") : product.tags);
  const variants = normalizeText(Array.isArray(product.variants) ? product.variants.map((v) => v.title).join(" ") : "");
  const hay = [title, handle, body, tags, variants].join(" ");

  const matchedTerms = [];
  let score = 0;

  for (const token of terms.tokens) {
    const re = new RegExp(`(^|\\s)${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\s|$)`);
    if (re.test(title)) {
      score += 4;
      matchedTerms.push(token);
    } else if (re.test(handle)) {
      score += 3;
      matchedTerms.push(token);
    } else if (re.test(tags) || re.test(variants)) {
      score += 2;
      matchedTerms.push(token);
    } else if (re.test(body) || hay.includes(token)) {
      score += 1;
      matchedTerms.push(token);
    }
  }

  const normalizedQuery = normalizeText(query);
  if (normalizedQuery && title.includes(normalizedQuery)) score += 20;
  if (normalizedQuery && handle.includes(normalizedQuery.replace(/\s+/g, " "))) score += 12;

  for (const phrase of terms.phrases) {
    if (phrase.length >= 8) {
      if (title.includes(phrase)) score += 10;
      else if (handle.includes(phrase.replace(/\s+/g, " "))) score += 8;
      else if (hay.includes(phrase)) score += 3;
    }
  }

  const clone = isCloneProduct(product);
  if (clone) score -= 12;

  const distinctMatched = unique(matchedTerms);
  const coverage = terms.tokens.length ? distinctMatched.length / terms.tokens.length : 0;
  if (distinctMatched.length >= 2) score += Math.round(coverage * 8);

  return {
    score,
    matched_terms: distinctMatched,
    clone
  };
}

async function fetchJson(url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error("timeout")), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "GET",
      headers: {
        "accept": "application/json,text/javascript,*/*;q=0.8",
        "user-agent": "Hermes-DTC-site-search/1.0"
      },
      signal: controller.signal,
      redirect: "follow"
    });
    const text = await res.text();
    if (!res.ok) {
      throw new Error(`HTTP ${res.status} for ${url}`);
    }
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(`Invalid JSON for ${url}`);
    }
  } finally {
    clearTimeout(timer);
  }
}

function siteOrigin(input) {
  try {
    const u = new URL(input || DEFAULT_SITE);
    return `${u.protocol}//${u.host}`;
  } catch {
    return DEFAULT_SITE;
  }
}

function absoluteProductUrl(origin, handle) {
  return `${origin.replace(/\/+$/, "")}/products/${encodeURIComponent(handle).replace(/%2F/gi, "/")}`;
}

function verificationUrl(origin, handle) {
  return `${absoluteProductUrl(origin, handle)}.js`;
}

function summarizeVerified(data) {
  if (!data || typeof data !== "object") return null;
  return {
    id: data.id ?? null,
    title: data.title ?? null,
    handle: data.handle ?? null,
    type: data.type ?? data.product_type ?? null,
    vendor: data.vendor ?? null,
    available: typeof data.available === "boolean" ? data.available : null,
    tags: Array.isArray(data.tags) ? data.tags.slice(0, 30) : data.tags ?? null,
    variants: Array.isArray(data.variants)
      ? data.variants.slice(0, 12).map((v) => ({
          id: v.id ?? null,
          title: v.title ?? null,
          option1: v.option1 ?? null,
          option2: v.option2 ?? null,
          option3: v.option3 ?? null,
          available: typeof v.available === "boolean" ? v.available : null,
          sku: v.sku ?? null
        }))
      : []
  };
}

async function main() {
  const trace = [];
  let args;

  try {
    args = JSON.parse(process.argv[2] || "{}");
  } catch {
    safeJsonPrint({
      success: false,
      error: "process.argv[2] must be a JSON object",
      fatal: true,
      trace
    });
    return;
  }

  try {
    const origin = siteOrigin(args.site_url || DEFAULT_SITE);
    const query = String(args.query ?? "");
    const expectedTerms = args.expected_terms ?? [];
    const requestedMax = Number(args.max_candidates);
    const maxCandidates = Number.isFinite(requestedMax) && requestedMax > 0
      ? Math.min(Math.floor(requestedMax), 20)
      : 8;

    const terms = buildSearchTerms(query, expectedTerms);
    makeTrace(trace, "input", {
      site_url: origin,
      query,
      expected_terms_count: Array.isArray(expectedTerms) ? expectedTerms.length : (expectedTerms ? 1 : 0),
      max_candidates: maxCandidates,
      token_count: terms.tokens.length
    });

    const products = [];
    for (let page = 1; page <= MAX_PAGES; page++) {
      const url = `${origin}/products.json?limit=${PAGE_LIMIT}&page=${page}`;
      makeTrace(trace, "fetch_catalog_page_start", { page, url });
      const json = await fetchJson(url, FETCH_TIMEOUT_MS);
      const pageProducts = Array.isArray(json.products) ? json.products : [];
      makeTrace(trace, "fetch_catalog_page_done", { page, count: pageProducts.length });
      if (pageProducts.length === 0) break;
      products.push(...pageProducts);
      if (pageProducts.length < PAGE_LIMIT) break;
    }

    makeTrace(trace, "catalog_complete", { total_products: products.length });

    const scored = products
      .map((product) => {
        const s = scoreProduct(product, terms, query);
        return { product, ...s };
      })
      .filter((x) => x.score > 0 && x.matched_terms.length > 0)
      .sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        if (a.clone !== b.clone) return a.clone ? 1 : -1;
        return String(a.product.title ?? "").localeCompare(String(b.product.title ?? ""));
      });

    const nonCloneCount = scored.filter((x) => !x.clone).length;
    const preferredPool = nonCloneCount > 0 ? scored.filter((x) => !x.clone) : scored;
    const selected = preferredPool.slice(0, maxCandidates);

    makeTrace(trace, "filter_and_rank", {
      scored_count: scored.length,
      non_clone_count: nonCloneCount,
      selected_count: selected.length
    });

    const candidates = [];
    const evidence = {
      strategy: "shopify_catalog_json_then_product_js_verification",
      catalog_endpoint_pattern: `${origin}/products.json?limit=250&page=N`,
      verification_endpoint_pattern: `${origin}/products/{handle}.js`,
      query_terms: terms.tokens,
      products_seen: products.length,
      clone_policy: "subscription/gift/free-gift listings are penalized and excluded when normal retail matches exist"
    };

    for (const item of selected) {
      const p = item.product;
      const handle = p.handle;
      const verifyUrl = verificationUrl(origin, handle);
      let verified = null;
      let verificationError = null;

      try {
        makeTrace(trace, "verify_product_start", { handle, url: verifyUrl });
        verified = summarizeVerified(await fetchJson(verifyUrl, VERIFY_TIMEOUT_MS));
        makeTrace(trace, "verify_product_done", { handle, ok: true });
      } catch (err) {
        verificationError = err && err.message ? err.message : String(err);
        makeTrace(trace, "verify_product_done", { handle, ok: false, error: verificationError });
      }

      candidates.push({
        url: absoluteProductUrl(origin, handle),
        verification_url: verifyUrl,
        title: p.title ?? null,
        handle,
        score: item.score,
        matched_terms: item.matched_terms,
        clone: item.clone,
        product_type: p.product_type ?? null,
        tags: Array.isArray(p.tags) ? p.tags.slice(0, 30) : p.tags ?? null,
        variants: Array.isArray(p.variants)
          ? p.variants.slice(0, 12).map((v) => ({
              title: v.title ?? null,
              sku: v.sku ?? null,
              available: typeof v.available === "boolean" ? v.available : null
            }))
          : [],
        verified,
        verification_error: verificationError
      });
    }

    safeJsonPrint({
      success: true,
      candidates,
      evidence,
      trace
    });
  } catch (err) {
    safeJsonPrint({
      success: false,
      error: err && err.message ? err.message : String(err),
      fatal: true,
      trace
    });
  }
}

await main();
