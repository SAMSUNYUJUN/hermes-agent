const DEFAULT_TIMEOUT_MS = 8000;
const CANONICAL_ORIGIN = "https://www.gamechest.gg";

function now() {
  return new Date().toISOString();
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

function uniq(values) {
  return [...new Set(values.filter(Boolean))];
}

function tokenize(value) {
  const stop = new Set([
    "a", "an", "and", "or", "the", "to", "of", "in", "on", "for", "with", "by",
    "from", "new", "bundle", "bundled", "set", "kit", "pack", "pcs", "pc", "exp"
  ]);
  return uniq(
    normalizeText(value)
      .split(" ")
      .filter((t) => t && t.length >= 2 && !stop.has(t) && !/^\d+$/.test(t))
  );
}

function productUrl(handle) {
  return `${CANONICAL_ORIGIN}/products/${handle}`;
}

function absoluteProductUrl(urlOrHandle) {
  const s = String(urlOrHandle ?? "");
  if (!s) return "";
  if (/^https?:\/\//i.test(s)) return s.replace("https://gamechest.gg", CANONICAL_ORIGIN);
  if (s.startsWith("/products/")) return `${CANONICAL_ORIGIN}${s}`;
  return productUrl(s.replace(/^\/+/, ""));
}

function variantText(product) {
  const variants = Array.isArray(product?.variants) ? product.variants : [];
  return variants
    .map((v) => [
      v.title,
      v.option1,
      v.option2,
      v.option3,
      v.sku,
      v.barcode,
      v.price,
      v.available === false ? "sold out" : ""
    ].filter(Boolean).join(" "))
    .join(" ");
}

function productHaystack(product) {
  const tags = Array.isArray(product?.tags) ? product.tags.join(" ") : String(product?.tags ?? "");
  return normalizeText([
    product?.title,
    product?.handle,
    product?.vendor,
    product?.product_type,
    tags,
    variantText(product)
  ].filter(Boolean).join(" "));
}

function buildSearchTerms(query, expectedTerms) {
  const phrases = [];
  if (query) phrases.push(query);
  for (const t of expectedTerms || []) phrases.push(t);

  const normalizedPhrases = uniq(
    phrases
      .map(normalizeText)
      .filter((p) => p.length >= 2)
  );

  const phraseParts = [];
  for (const p of normalizedPhrases) {
    if (p.includes(" ")) phraseParts.push(p);
  }

  const tokens = uniq([
    ...tokenize(query),
    ...(expectedTerms || []).flatMap(tokenize)
  ]);

  return {
    normalizedPhrases: uniq([...normalizedPhrases, ...phraseParts]),
    normalizedTokens: tokens
  };
}

function scoreProduct(product, terms) {
  const hay = productHaystack(product);
  const title = normalizeText(product?.title);
  const handle = normalizeText(product?.handle);
  const matched = [];
  let score = 0;

  for (const phrase of terms.normalizedPhrases) {
    if (!phrase || phrase.length < 3) continue;
    if (hay.includes(phrase)) {
      matched.push(phrase);
      score += phrase.includes(" ") ? 12 : 5;
      if (title.includes(phrase)) score += 8;
      if (handle.includes(phrase)) score += 4;
    }
  }

  const genericLowWeight = new Set(["camera", "case", "cover", "brown", "black", "white", "red", "blue", "green", "pink", "yellow", "game", "switch"]);
  for (const token of terms.normalizedTokens) {
    if (!token || token.length < 2) continue;
    const re = new RegExp(`(^| )${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}( |$)`);
    if (re.test(hay)) {
      matched.push(token);
      score += genericLowWeight.has(token) ? 1 : 4;
      if (re.test(title)) score += genericLowWeight.has(token) ? 1 : 3;
      if (re.test(handle)) score += 1;
    }
  }

  const matchedTerms = uniq(matched);
  return { score, matchedTerms, haystack: hay };
}

async function fetchJson(url, trace, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const started = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let status = 0;
  let ok = false;
  let text = "";
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: {
        "accept": "application/json,text/plain,*/*",
        "user-agent": "Hermes-DTC-SiteSearch/1.0"
      }
    });
    status = res.status;
    ok = res.ok;
    text = await res.text();
    const data = text ? JSON.parse(text) : null;
    trace.push({
      at: now(),
      action: "fetch_json",
      url,
      status,
      ok,
      ms: Date.now() - started,
      bytes: text.length,
      parse_ok: true
    });
    return { ok, status, data, text };
  } catch (err) {
    trace.push({
      at: now(),
      action: "fetch_json",
      url,
      status,
      ok,
      ms: Date.now() - started,
      bytes: text.length,
      parse_ok: false,
      error: err?.name === "AbortError" ? "timeout" : String(err?.message || err)
    });
    return { ok: false, status, data: null, text, error: err?.name === "AbortError" ? "timeout" : String(err?.message || err) };
  } finally {
    clearTimeout(timer);
  }
}

async function fetchText(url, trace, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const started = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let status = 0;
  let ok = false;
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: {
        "accept": "text/html,*/*",
        "user-agent": "Hermes-DTC-SiteSearch/1.0"
      }
    });
    status = res.status;
    ok = res.ok;
    const text = await res.text();
    trace.push({ at: now(), action: "fetch_text", url, status, ok, ms: Date.now() - started, bytes: text.length });
    return { ok, status, text };
  } catch (err) {
    trace.push({
      at: now(),
      action: "fetch_text",
      url,
      status,
      ok,
      ms: Date.now() - started,
      error: err?.name === "AbortError" ? "timeout" : String(err?.message || err)
    });
    return { ok: false, status, text: "", error: err?.name === "AbortError" ? "timeout" : String(err?.message || err) };
  } finally {
    clearTimeout(timer);
  }
}

function extractProducts(data) {
  if (Array.isArray(data?.products)) return data.products;
  if (Array.isArray(data)) return data;
  return [];
}

function extractPredictiveProducts(data) {
  const products =
    data?.resources?.results?.products ||
    data?.resources?.results?.product ||
    data?.products ||
    [];
  return Array.isArray(products) ? products : [];
}

function candidateFromProduct(product, scoring, source) {
  const handle = product?.handle || String(product?.url || "").split("/products/").pop()?.split(/[?#]/)[0] || "";
  const variants = Array.isArray(product?.variants) ? product.variants : [];
  const firstVariant = variants[0] || {};
  const images = Array.isArray(product?.images) ? product.images : [];
  const image = product?.featured_image || images[0]?.src || images[0] || "";
  return {
    title: String(product?.title || "").trim(),
    url: absoluteProductUrl(handle || product?.url),
    handle,
    vendor: product?.vendor || null,
    product_type: product?.product_type || null,
    price: firstVariant?.price ?? product?.price ?? null,
    available: firstVariant?.available ?? product?.available ?? null,
    image: typeof image === "string" ? image : image?.src || null,
    score: scoring.score,
    matched_terms: scoring.matchedTerms,
    source,
    evidence: {
      matched_terms: scoring.matchedTerms,
      searched_fields: ["title", "handle", "vendor", "product_type", "tags", "variants"]
    }
  };
}

function mergeCandidate(map, candidate) {
  if (!candidate?.url && !candidate?.handle && !candidate?.title) return;
  const key = candidate.handle || candidate.url || normalizeText(candidate.title);
  const existing = map.get(key);
  if (!existing || (candidate.score || 0) > (existing.score || 0)) {
    map.set(key, existing ? { ...existing, ...candidate, source: uniq([existing.source, candidate.source].join(",").split(",")) .join(",") } : candidate);
  } else if (existing) {
    existing.source = uniq([existing.source, candidate.source].join(",").split(",")).join(",");
    existing.matched_terms = uniq([...(existing.matched_terms || []), ...(candidate.matched_terms || [])]);
    existing.evidence = existing.evidence || {};
    existing.evidence.matched_terms = existing.matched_terms;
  }
}

function predictiveQueries(query, terms) {
  const distinctive = terms.normalizedTokens.filter((t) => !["game", "switch"].includes(t));
  const qs = [
    query,
    ...terms.normalizedPhrases.filter((p) => p.includes(" ")).slice(0, 5),
    ...distinctive.slice(0, 8)
  ];

  if (terms.normalizedTokens.includes("camera")) qs.push("camera");
  if (terms.normalizedPhrases.some((p) => p.includes("film camera"))) qs.push("film camera");
  if (terms.normalizedPhrases.some((p) => p.includes("half frame"))) qs.push("half frame");

  return uniq(qs.map((q) => String(q || "").trim()).filter(Boolean)).slice(0, 12);
}

async function main() {
  const trace = [];
  try {
    const arg = process.argv[2];
    if (!arg) throw new Error("Missing JSON argument in process.argv[2]");
    const input = JSON.parse(arg);

    const siteUrl = input.site_url || "https://gamechest.gg";
    const query = String(input.query || "");
    const expectedTerms = Array.isArray(input.expected_terms) ? input.expected_terms.map(String) : [];
    const maxCandidates = Math.max(0, Math.min(50, Number(input.max_candidates || 5)));

    trace.push({ at: now(), action: "start", site_url: siteUrl, query, max_candidates: maxCandidates });

    const terms = buildSearchTerms(query, expectedTerms);
    const evidence = {
      site_url: siteUrl,
      canonical_site_url: CANONICAL_ORIGIN,
      strategy: "Shopify catalog JSON enumeration, backup all-products endpoint if needed, and predictive search confirmation including broad fallback terms.",
      searched_fields: ["title", "handle", "vendor", "product_type", "tags", "variants"],
      query,
      expected_terms: expectedTerms,
      normalized_phrases: terms.normalizedPhrases,
      normalized_tokens: terms.normalizedTokens,
      catalog_counts: {},
      endpoint_errors: [],
      catalog_products_enumerated: 0,
      catalog_matches: 0,
      predictive_search_queries: [],
      predictive_matches: 0,
      page_verifications: []
    };

    const endpoints = [
      `${CANONICAL_ORIGIN}/products.json?limit=250`,
      `${CANONICAL_ORIGIN}/collections/all/products.json?limit=250`
    ];

    let products = [];
    const seenProductHandles = new Set();

    for (const endpoint of endpoints) {
      const res = await fetchJson(endpoint, trace);
      if (!res.ok || !res.data) {
        evidence.endpoint_errors.push({ url: endpoint, status: res.status, error: res.error || "fetch_or_parse_failed" });
        continue;
      }
      const batch = extractProducts(res.data);
      evidence.catalog_counts[endpoint] = batch.length;

      for (const p of batch) {
        const key = p?.handle || p?.id || normalizeText(p?.title);
        if (key && !seenProductHandles.has(key)) {
          seenProductHandles.add(key);
          products.push(p);
        }
      }

      if (batch.length >= 1 && endpoint.includes("/products.json")) break;
    }

    evidence.catalog_products_enumerated = products.length;

    const candidatesByKey = new Map();

    for (const product of products) {
      const scoring = scoreProduct(product, terms);
      if (scoring.score > 0 && scoring.matchedTerms.length > 0) {
        mergeCandidate(candidatesByKey, candidateFromProduct(product, scoring, "catalog"));
      }
    }

    evidence.catalog_matches = candidatesByKey.size;

    const queries = predictiveQueries(query, terms);
    evidence.predictive_search_queries = queries;

    for (const q of queries) {
      const url = `${CANONICAL_ORIGIN}/search/suggest.json?q=${encodeURIComponent(q)}&resources[type]=product&resources[limit]=10`;
      const res = await fetchJson(url, trace, 6000);
      const predicted = res.ok && res.data ? extractPredictiveProducts(res.data) : [];
      trace.push({ at: now(), action: "predictive_search_results", query: q, count: predicted.length });

      for (const p of predicted) {
        const normalizedPredictiveProduct = {
          ...p,
          handle: p.handle || String(p.url || "").split("/products/").pop()?.split(/[?#]/)[0],
          title: p.title,
          vendor: p.vendor,
          product_type: p.product_type,
          tags: p.tags,
          variants: p.variants
        };
        const scoring = scoreProduct(normalizedPredictiveProduct, terms);
        if (scoring.score <= 0) {
          scoring.score = 1;
          scoring.matchedTerms = [normalizeText(q)].filter(Boolean);
        }
        mergeCandidate(candidatesByKey, candidateFromProduct(normalizedPredictiveProduct, scoring, "predictive_search"));
      }
    }

    evidence.predictive_matches = [...candidatesByKey.values()].filter((c) => String(c.source || "").includes("predictive_search")).length;

    let candidates = [...candidatesByKey.values()]
      .filter((c) => c.title || c.url)
      .sort((a, b) => (b.score || 0) - (a.score || 0) || String(a.title).localeCompare(String(b.title)))
      .slice(0, maxCandidates);

    for (const candidate of candidates.slice(0, Math.min(5, maxCandidates))) {
      if (!candidate.url) continue;
      const page = await fetchText(candidate.url, trace, 6000);
      const titleMatch = page.text.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
      const h1Match = page.text.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i);
      const clean = (s) => String(s || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
      const pageEvidence = {
        url: candidate.url,
        status: page.status,
        ok: page.ok,
        title: clean(titleMatch?.[1]),
        h1: clean(h1Match?.[1])
      };
      evidence.page_verifications.push(pageEvidence);
      candidate.evidence = {
        ...(candidate.evidence || {}),
        page_title: pageEvidence.title || null,
        page_h1: pageEvidence.h1 || null
      };
    }

    trace.push({ at: now(), action: "complete", candidates: candidates.length });

    process.stdout.write(JSON.stringify({
      success: true,
      candidates,
      evidence,
      trace
    }));
  } catch (err) {
    trace.push({ at: now(), action: "fatal_error", error: String(err?.message || err) });
    process.stdout.write(JSON.stringify({
      success: false,
      error: String(err?.message || err),
      fatal: true,
      trace
    }));
  }
}

await main();
