const DEFAULT_SITE = "https://www.gamechest.gg";
const PRIMARY_CATALOG = "https://www.gamechest.gg/products.json?limit=250";
const BACKUP_CATALOG = "https://www.gamechest.gg/collections/all/products.json?limit=250";
const PRODUCT_BASE = "https://www.gamechest.gg/products/";

const STOPWORDS = new Set([
  "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it",
  "new", "of", "on", "or", "the", "to", "with", "version", "edition"
]);

function nowIso() {
  return new Date().toISOString();
}

function normalizeText(value) {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/&amp;/gi, " and ")
    .replace(/[^a-zA-Z0-9]+/g, " ")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
}

function uniqueArray(values) {
  return [...new Set(values.filter(Boolean))];
}

function tokenize(value) {
  return uniqueArray(
    normalizeText(value)
      .split(" ")
      .filter((t) => t.length >= 2 && !STOPWORDS.has(t))
  );
}

function phraseList(input) {
  const out = [];
  const push = (v) => {
    const n = normalizeText(v);
    if (n && n.length >= 2) out.push(n);
  };

  push(input.query);

  if (Array.isArray(input.expected_terms)) {
    for (const term of input.expected_terms) push(term);
  } else if (input.expected_terms) {
    push(input.expected_terms);
  }

  for (const phrase of [...out]) {
    if (phrase.includes("ps5")) out.push("playstation 5");
    if (phrase.includes("playstation 5")) out.push("ps5");
    if (phrase.includes("switch") && !phrase.includes("nintendo switch")) out.push("nintendo switch");
  }

  return uniqueArray(out).sort((a, b) => b.length - a.length);
}

function buildSearchTerms(input) {
  const phrases = phraseList(input);
  const tokens = uniqueArray([
    ...tokenize(input.query),
    ...(Array.isArray(input.expected_terms) ? input.expected_terms.flatMap(tokenize) : tokenize(input.expected_terms || ""))
  ]);

  const expanded = new Set(tokens);
  const joined = ` ${phrases.join(" ")} `;
  if (joined.includes(" ps5 ")) expanded.add("playstation");
  if (joined.includes("playstation 5")) expanded.add("ps5");

  return {
    phrases,
    tokens: [...expanded].filter((t) => t && t.length >= 2)
  };
}

function productUrl(product) {
  if (product?.handle) return `${PRODUCT_BASE}${encodeURIComponent(product.handle).replace(/%2F/g, "/")}`;
  if (product?.url) return new URL(product.url, DEFAULT_SITE).toString();
  return null;
}

function searchableFields(product) {
  const tags = Array.isArray(product.tags) ? product.tags.join(" ") : String(product.tags ?? "");
  const variants = Array.isArray(product.variants)
    ? product.variants.map((v) => [v.title, v.sku, v.option1, v.option2, v.option3].filter(Boolean).join(" ")).join(" ")
    : "";
  return {
    title: normalizeText(product.title),
    handle: normalizeText(product.handle),
    vendor: normalizeText(product.vendor),
    product_type: normalizeText(product.product_type),
    tags: normalizeText(tags),
    variants: normalizeText(variants)
  };
}

function scoreProduct(product, search) {
  const fields = searchableFields(product);
  const weightedHaystack = {
    title: fields.title,
    handle: fields.handle,
    tags: fields.tags,
    vendor: fields.vendor,
    product_type: fields.product_type,
    variants: fields.variants
  };

  let score = 0;
  const matchedTerms = new Set();
  const matchedPhrases = new Set();

  for (const phrase of search.phrases) {
    if (!phrase) continue;
    if (weightedHaystack.title.includes(phrase)) {
      score += Math.min(80, 28 + phrase.length);
      matchedTerms.add(phrase);
      matchedPhrases.add(phrase);
    }
    if (weightedHaystack.handle.includes(phrase.replace(/\s+/g, " ")) || weightedHaystack.handle.includes(phrase.replace(/\s+/g, "-"))) {
      score += Math.min(60, 18 + phrase.length);
      matchedTerms.add(phrase);
      matchedPhrases.add(phrase);
    }
    if (weightedHaystack.tags.includes(phrase)) {
      score += 22;
      matchedTerms.add(phrase);
      matchedPhrases.add(phrase);
    }
    if (weightedHaystack.variants.includes(phrase)) {
      score += 14;
      matchedTerms.add(phrase);
      matchedPhrases.add(phrase);
    }
    if (weightedHaystack.vendor.includes(phrase) || weightedHaystack.product_type.includes(phrase)) {
      score += 8;
      matchedTerms.add(phrase);
      matchedPhrases.add(phrase);
    }
  }

  for (const token of search.tokens) {
    const re = new RegExp(`(?:^|\\s)${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:\\s|$)`);
    if (re.test(weightedHaystack.title)) {
      score += 8;
      matchedTerms.add(token);
    }
    if (re.test(weightedHaystack.handle)) {
      score += 5;
      matchedTerms.add(token);
    }
    if (re.test(weightedHaystack.tags)) {
      score += 4;
      matchedTerms.add(token);
    }
    if (re.test(weightedHaystack.variants)) {
      score += 3;
      matchedTerms.add(token);
    }
    if (re.test(weightedHaystack.vendor) || re.test(weightedHaystack.product_type)) {
      score += 2;
      matchedTerms.add(token);
    }
  }

  const tokenMatchCount = [...matchedTerms].filter((t) => search.tokens.includes(t)).length;
  const hasStrongPhrase = matchedPhrases.size > 0;
  const plausible = hasStrongPhrase || tokenMatchCount >= Math.min(2, Math.max(1, search.tokens.length));

  return {
    score,
    plausible,
    matched_terms: [...matchedTerms].sort((a, b) => b.length - a.length)
  };
}

function makeCandidate(product, scoreInfo, source) {
  const url = productUrl(product);
  const variants = Array.isArray(product.variants)
    ? product.variants.slice(0, 8).map((v) => ({
        id: v.id,
        title: v.title,
        sku: v.sku,
        available: typeof v.available === "boolean" ? v.available : undefined,
        price: v.price
      }))
    : [];

  return {
    title: product.title || null,
    url,
    handle: product.handle || null,
    vendor: product.vendor || null,
    product_type: product.product_type || null,
    tags: Array.isArray(product.tags) ? product.tags : (product.tags ? String(product.tags).split(",").map((s) => s.trim()).filter(Boolean) : []),
    score: scoreInfo.score,
    matched_terms: scoreInfo.matched_terms,
    source,
    variants
  };
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error(`timeout after ${timeoutMs}ms`)), timeoutMs);
  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        "accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "user-agent": "HermesDtcSiteSearch/1.0 (+bounded-fetch)",
        ...(options.headers || {})
      }
    });
  } finally {
    clearTimeout(timer);
  }
}

async function fetchJson(url, trace, timeoutMs = 8000) {
  const started = Date.now();
  try {
    const response = await fetchWithTimeout(url, {}, timeoutMs);
    const text = await response.text();
    let json = null;
    let parseError = null;
    try {
      json = JSON.parse(text);
    } catch (e) {
      parseError = e.message;
    }

    trace.push({
      at: nowIso(),
      action: "fetch_json",
      url,
      status: response.status,
      ok: response.ok,
      ms: Date.now() - started,
      bytes: text.length,
      parse_ok: !!json,
      parse_error: parseError || undefined
    });

    if (!response.ok || !json) return { ok: false, status: response.status, json: null, error: parseError || `HTTP ${response.status}` };
    return { ok: true, status: response.status, json };
  } catch (e) {
    trace.push({
      at: nowIso(),
      action: "fetch_json",
      url,
      ok: false,
      ms: Date.now() - started,
      error: e?.name === "AbortError" ? "timeout" : String(e?.message || e)
    });
    return { ok: false, status: 0, json: null, error: e?.name === "AbortError" ? "timeout" : String(e?.message || e) };
  }
}

async function fetchHtmlTitle(url, trace, timeoutMs = 6000) {
  const started = Date.now();
  try {
    const response = await fetchWithTimeout(url, { headers: { accept: "text/html,*/*;q=0.8" } }, timeoutMs);
    const text = await response.text();
    const title =
      (text.match(/<meta\s+property=["']og:title["']\s+content=["']([^"']+)["']/i) || [])[1] ||
      (text.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [])[1] ||
      null;

    trace.push({
      at: nowIso(),
      action: "verify_product_page",
      url,
      status: response.status,
      ok: response.ok,
      ms: Date.now() - started,
      title: title ? decodeHtml(title).trim().slice(0, 200) : null
    });

    return { ok: response.ok, title: title ? decodeHtml(title).trim() : null };
  } catch (e) {
    trace.push({
      at: nowIso(),
      action: "verify_product_page",
      url,
      ok: false,
      ms: Date.now() - started,
      error: e?.name === "AbortError" ? "timeout" : String(e?.message || e)
    });
    return { ok: false, title: null };
  }
}

function decodeHtml(value) {
  return String(value)
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ");
}

function extractSuggestProducts(json) {
  const products = json?.resources?.results?.products;
  return Array.isArray(products) ? products : [];
}

function productFromSuggest(item) {
  const url = item.url ? new URL(item.url, DEFAULT_SITE).toString() : null;
  const handle = item.handle || (url ? url.split("/products/")[1]?.split(/[?#]/)[0] : null);
  return {
    title: item.title,
    handle,
    vendor: item.vendor,
    product_type: item.type || item.product_type,
    tags: item.tags || [],
    variants: [],
    url
  };
}

function predictiveQueries(input, search) {
  const out = [];
  if (input.query) out.push(String(input.query));
  for (const p of search.phrases) {
    if (p.length >= 4 && p.length <= 80) out.push(p);
  }
  const distinctive = search.tokens.filter((t) => !["white", "black", "disc", "free"].includes(t)).slice(0, 4);
  if (distinctive.length) out.push(distinctive.join(" "));
  return uniqueArray(out).slice(0, 6);
}

async function main() {
  const trace = [];
  let input;

  try {
    input = JSON.parse(process.argv[2] || "");
  } catch (e) {
    return {
      success: false,
      error: `Invalid JSON argument: ${e.message}`,
      fatal: true,
      trace
    };
  }

  const maxCandidates = Math.max(0, Math.min(50, Number.isFinite(Number(input.max_candidates)) ? Number(input.max_candidates) : 10));
  const search = buildSearchTerms(input);
  const evidence = {
    site_url: input.site_url || "https://gamechest.gg",
    canonical_site_url: DEFAULT_SITE,
    strategy: "Shopify catalog JSON enumeration, with backup all-products endpoint if needed and predictive search confirmation when no catalog match is found.",
    searched_fields: ["title", "handle", "vendor", "product_type", "tags", "variants"],
    query: input.query || "",
    expected_terms: input.expected_terms ?? [],
    normalized_phrases: search.phrases,
    normalized_tokens: search.tokens,
    catalog_counts: {},
    endpoint_errors: []
  };

  trace.push({
    at: nowIso(),
    action: "start",
    site_url: input.site_url || null,
    query: input.query || "",
    max_candidates: maxCandidates
  });

  const productMap = new Map();

  const primary = await fetchJson(PRIMARY_CATALOG, trace, 8000);
  if (primary.ok && Array.isArray(primary.json?.products)) {
    evidence.catalog_counts[PRIMARY_CATALOG] = primary.json.products.length;
    for (const p of primary.json.products) {
      if (p?.handle) productMap.set(p.handle, { product: p, sources: new Set(["products.json"]) });
    }
  } else {
    evidence.endpoint_errors.push({ url: PRIMARY_CATALOG, error: primary.error || "unavailable" });
  }

  const shouldUseBackup = !primary.ok || !Array.isArray(primary.json?.products) || primary.json.products.length === 0 || primary.json.products.length >= 250;
  if (shouldUseBackup) {
    const backup = await fetchJson(BACKUP_CATALOG, trace, 8000);
    if (backup.ok && Array.isArray(backup.json?.products)) {
      evidence.catalog_counts[BACKUP_CATALOG] = backup.json.products.length;
      for (const p of backup.json.products) {
        if (!p?.handle) continue;
        if (!productMap.has(p.handle)) productMap.set(p.handle, { product: p, sources: new Set() });
        productMap.get(p.handle).sources.add("collections/all/products.json");
      }
    } else {
      evidence.endpoint_errors.push({ url: BACKUP_CATALOG, error: backup.error || "unavailable" });
    }
  }

  let candidates = [];
  for (const { product, sources } of productMap.values()) {
    const scoreInfo = scoreProduct(product, search);
    if (scoreInfo.plausible && scoreInfo.score > 0) {
      candidates.push(makeCandidate(product, scoreInfo, [...sources].join("+")));
    }
  }

  candidates.sort((a, b) => b.score - a.score || String(a.title).localeCompare(String(b.title)));

  evidence.catalog_products_enumerated = productMap.size;
  evidence.catalog_matches = candidates.length;

  if (candidates.length === 0) {
    const suggestQueries = predictiveQueries(input, search);
    evidence.predictive_search_queries = suggestQueries;
    for (const q of suggestQueries) {
      const url = `https://www.gamechest.gg/search/suggest.json?q=${encodeURIComponent(q)}&resources[type]=product&resources[limit]=10`;
      const suggest = await fetchJson(url, trace, 7000);
      if (!suggest.ok) {
        evidence.endpoint_errors.push({ url, error: suggest.error || "unavailable" });
        continue;
      }

      const products = extractSuggestProducts(suggest.json).map(productFromSuggest);
      trace.push({
        at: nowIso(),
        action: "predictive_search_results",
        query: q,
        count: products.length
      });

      for (const p of products) {
        const scoreInfo = scoreProduct(p, search);
        if (!scoreInfo.plausible || scoreInfo.score <= 0) continue;
        const c = makeCandidate(p, scoreInfo, `predictive_search:${q}`);
        if (!c.handle && c.url) c.handle = c.url.split("/products/")[1]?.split(/[?#]/)[0] || null;
        const key = c.handle || c.url || c.title;
        if (!candidates.some((x) => (x.handle || x.url || x.title) === key)) candidates.push(c);
      }
    }
    candidates.sort((a, b) => b.score - a.score || String(a.title).localeCompare(String(b.title)));
    evidence.predictive_matches = candidates.length;
  }

  candidates = candidates.slice(0, maxCandidates);

  for (const c of candidates.slice(0, Math.min(5, candidates.length))) {
    if (!c.url) continue;
    const verification = await fetchHtmlTitle(c.url, trace, 6000);
    c.page_verified = verification.ok;
    if (verification.title) c.page_title = verification.title;
  }

  trace.push({
    at: nowIso(),
    action: "complete",
    candidates: candidates.length
  });

  return {
    success: true,
    candidates,
    evidence,
    trace
  };
}

main()
  .then((result) => {
    process.stdout.write(JSON.stringify(result));
  })
  .catch((e) => {
    process.stdout.write(JSON.stringify({
      success: false,
      error: String(e?.stack || e?.message || e),
      fatal: true,
      trace: [{
        at: nowIso(),
        action: "unhandled_error",
        error: String(e?.message || e)
      }]
    }));
  });
