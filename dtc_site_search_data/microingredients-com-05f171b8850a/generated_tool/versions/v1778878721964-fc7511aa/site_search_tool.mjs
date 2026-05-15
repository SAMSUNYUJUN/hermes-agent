const DEFAULT_TIMEOUT_MS = 8000;
const MAX_FETCHES = 24;

let fetchCount = 0;

function nowIso() {
  return new Date().toISOString();
}

function safeJsonPrint(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function asArray(value) {
  if (Array.isArray(value)) return value.filter((v) => typeof v === "string" && v.trim()).map((v) => v.trim());
  if (typeof value === "string" && value.trim()) return [value.trim()];
  return [];
}

function stripHtml(s) {
  return String(s || "")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&#39;/g, "'")
    .replace(/&quot;/gi, '"')
    .replace(/\s+/g, " ")
    .trim();
}

function decodeEntities(s) {
  return String(s || "")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">");
}

function normalizeText(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9+]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function significantTokens(s) {
  const stop = new Set([
    "micro", "ingredients", "ingredient", "the", "and", "with", "for", "plus", "advanced",
    "powder", "capsules", "capsule", "veggie", "organic", "natural", "supplement", "bundle"
  ]);
  return normalizeText(s)
    .split(" ")
    .filter((t) => t.length >= 3 && !stop.has(t));
}

function slugify(s) {
  return normalizeText(s)
    .replace(/\+/g, " ")
    .split(" ")
    .filter(Boolean)
    .join("-");
}

function absoluteUrl(href, base) {
  try {
    return new URL(decodeEntities(href), base).toString();
  } catch {
    return null;
  }
}

function canonicalProductUrl(url) {
  try {
    const u = new URL(url);
    const m = u.pathname.match(/^\/products\/([^/?#]+?)(?:\.js)?\/?$/);
    if (!m) return null;
    return `https://www.microingredients.com/products/${m[1]}`;
  } catch {
    return null;
  }
}

function handleFromProductUrl(url) {
  try {
    const u = new URL(url);
    const m = u.pathname.match(/^\/products\/([^/?#]+?)(?:\.js)?\/?$/);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

async function fetchWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  if (fetchCount >= MAX_FETCHES) {
    throw new Error(`fetch limit exceeded (${MAX_FETCHES})`);
  }
  fetchCount += 1;

  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      redirect: "follow",
      ...options,
      signal: controller.signal,
      headers: {
        "accept": options.accept || "text/html,application/json;q=0.9,*/*;q=0.8",
        "user-agent": "Mozilla/5.0 HermesDTCSearch/1.0 (+https://www.microingredients.com)",
        ...(options.headers || {})
      }
    });
    const text = await res.text();
    return {
      ok: res.ok,
      status: res.status,
      url: res.url,
      text,
      contentType: res.headers.get("content-type") || ""
    };
  } finally {
    clearTimeout(id);
  }
}

async function fetchJson(url, trace, label) {
  const step = { step: label, url, at: nowIso() };
  try {
    const res = await fetchWithTimeout(url, { headers: { accept: "application/json,text/plain;q=0.9,*/*;q=0.8" } });
    step.status = res.status;
    step.final_url = res.url;
    step.ok = res.ok;
    trace.push(step);
    if (!res.ok) return null;
    return JSON.parse(res.text);
  } catch (e) {
    step.error = e && e.message ? e.message : String(e);
    trace.push(step);
    return null;
  }
}

async function fetchText(url, trace, label) {
  const step = { step: label, url, at: nowIso() };
  try {
    const res = await fetchWithTimeout(url);
    step.status = res.status;
    step.final_url = res.url;
    step.ok = res.ok;
    step.bytes = res.text.length;
    trace.push(step);
    return res.ok ? res.text : "";
  } catch (e) {
    step.error = e && e.message ? e.message : String(e);
    trace.push(step);
    return "";
  }
}

function extractProductLinksFromHtml(html, base) {
  const found = new Map();

  const anchorRe = /<a\b([^>]*?)href\s*=\s*["']([^"']*\/products\/[^"']+)["']([^>]*)>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = anchorRe.exec(html))) {
    const url = canonicalProductUrl(absoluteUrl(m[2], base) || "");
    if (!url) continue;
    const title = stripHtml(m[4]);
    if (!found.has(url)) found.set(url, { url, html_title: title || null, sources: ["search_html_anchor"] });
  }

  const hrefRe = /href\s*=\s*["']([^"']*\/products\/[^"']+)["']/gi;
  while ((m = hrefRe.exec(html))) {
    const url = canonicalProductUrl(absoluteUrl(m[1], base) || "");
    if (!url) continue;
    if (!found.has(url)) found.set(url, { url, html_title: null, sources: ["search_html_href"] });
  }

  return [...found.values()];
}

function extractProductsFromSuggestJson(json) {
  const products = [];
  const containers = [
    json && json.resources && json.resources.results && json.resources.results.products,
    json && json.products,
    json && json.results && json.results.products
  ];

  for (const arr of containers) {
    if (!Array.isArray(arr)) continue;
    for (const p of arr) {
      const rawUrl = p.url || p.path || p.online_store_url || (p.handle ? `/products/${p.handle}` : null);
      if (!rawUrl) continue;
      const url = canonicalProductUrl(absoluteUrl(rawUrl, "https://www.microingredients.com/") || "");
      if (!url) continue;
      products.push({
        url,
        html_title: stripHtml(p.title || p.name || ""),
        image: p.image || (p.featured_image && (p.featured_image.url || p.featured_image)) || null,
        sources: ["search_suggest_json"]
      });
    }
  }
  return products;
}

function productFromJs(js, url, fallbackTitle, sources) {
  if (!js || typeof js !== "object") return null;
  const handle = js.handle || handleFromProductUrl(url);
  const productUrl = `https://www.microingredients.com/products/${handle}`;
  const variants = Array.isArray(js.variants)
    ? js.variants.map((v) => ({
        id: v.id,
        title: v.title,
        available: typeof v.available === "boolean" ? v.available : undefined,
        price: v.price,
        sku: v.sku || undefined
      }))
    : [];

  const images = Array.isArray(js.images)
    ? js.images.slice(0, 8).map((img) => (typeof img === "string" ? img : (img && (img.src || img.url)))).filter(Boolean)
    : [];

  return {
    title: stripHtml(js.title || fallbackTitle || ""),
    handle,
    url: productUrl,
    js_url: `${productUrl}.js`,
    vendor: js.vendor || undefined,
    product_type: js.type || js.product_type || undefined,
    description: stripHtml(js.description || "").slice(0, 1200),
    variants,
    images,
    sources: [...new Set(sources || [])]
  };
}

function scoreCandidate(candidate, query, expectedTerms) {
  const hay = normalizeText([
    candidate.title,
    candidate.handle,
    candidate.description,
    candidate.product_type,
    candidate.vendor,
    (candidate.variants || []).map((v) => v.title).join(" ")
  ].filter(Boolean).join(" "));

  const qNorm = normalizeText(query);
  const expected = expectedTerms.length ? expectedTerms : [];
  const matchedTerms = [];
  let score = 0;

  if (qNorm && hay.includes(qNorm)) score += 80;

  for (const term of expected) {
    const n = normalizeText(term);
    if (!n) continue;
    if (hay.includes(n)) {
      matchedTerms.push(term);
      score += Math.max(12, Math.min(35, n.length));
    } else {
      const toks = significantTokens(n);
      const hit = toks.filter((t) => hay.includes(t));
      if (hit.length) {
        matchedTerms.push(term);
        score += Math.round((hit.length / Math.max(1, toks.length)) * 18);
      }
    }
  }

  const qTokens = significantTokens(query);
  const qHits = qTokens.filter((t) => hay.includes(t));
  score += qHits.length * 6;
  if (qTokens.length) score += Math.round((qHits.length / qTokens.length) * 30);

  const titleNorm = normalizeText(candidate.title);
  if (titleNorm && qNorm && (titleNorm.includes(qNorm) || qNorm.includes(titleNorm))) score += 30;

  if (!normalizeText(query).includes("bundle") && titleNorm.includes("bundle")) score -= 20;
  if (candidate.sources && candidate.sources.includes("search_suggest_json")) score += 4;
  if (candidate.sources && candidate.sources.includes("search_html_anchor")) score += 3;

  return { score, matched_terms: [...new Set(matchedTerms)], query_token_matches: [...new Set(qHits)] };
}

async function main() {
  const trace = [];
  try {
    if (!process.argv[2]) throw new Error("missing JSON argument at process.argv[2]");
    let input;
    try {
      input = JSON.parse(process.argv[2]);
    } catch (e) {
      throw new Error(`invalid JSON argument: ${e.message}`);
    }

    const query = typeof input.query === "string" ? input.query.trim() : "";
    if (!query) throw new Error("query must be a non-empty string");

    const expectedTerms = asArray(input.expected_terms);
    const maxCandidates = Math.max(0, Math.min(20, Number.isFinite(Number(input.max_candidates)) ? Number(input.max_candidates) : 5));
    const base = "https://www.microingredients.com";
    const evidence = {
      requested_site_url: input.site_url || null,
      canonical_host: base,
      strategy: "Micro Ingredients Shopify exact search via /search?q=, product links, predictive search JSON fallback, and /products/<handle>.js verification.",
      search_urls: [],
      product_json_attempts: []
    };

    const discovered = new Map();

    const searchUrl = `${base}/search?q=${encodeURIComponent(query)}`;
    evidence.search_urls.push(searchUrl);
    const html = await fetchText(searchUrl, trace, "search_html");
    for (const item of extractProductLinksFromHtml(html, searchUrl)) {
      discovered.set(item.url, item);
    }

    const suggestUrl = `${base}/search/suggest.json?q=${encodeURIComponent(query)}&resources[type]=product&resources[limit]=10&resources[options][unavailable_products]=last`;
    evidence.search_urls.push(suggestUrl);
    const suggest = await fetchJson(suggestUrl, trace, "search_suggest_json");
    for (const item of extractProductsFromSuggestJson(suggest)) {
      const prior = discovered.get(item.url);
      if (prior) {
        prior.sources = [...new Set([...(prior.sources || []), ...(item.sources || [])])];
        prior.html_title = prior.html_title || item.html_title;
        prior.image = prior.image || item.image;
      } else {
        discovered.set(item.url, item);
      }
    }

    const guessedHandle = slugify(query.replace(/^micro ingredients\s*/i, ""));
    if (guessedHandle) {
      const guessedUrl = `${base}/products/${guessedHandle}`;
      if (!discovered.has(guessedUrl)) {
        discovered.set(guessedUrl, { url: guessedUrl, html_title: null, sources: ["query_handle_guess"] });
      }
    }

    const verified = [];
    const pool = [...discovered.values()].slice(0, Math.max(maxCandidates * 4, 8));
    for (const item of pool) {
      const handle = handleFromProductUrl(item.url);
      if (!handle) continue;
      const jsUrl = `${base}/products/${handle}.js`;
      evidence.product_json_attempts.push(jsUrl);
      const js = await fetchJson(jsUrl, trace, "product_js");
      const product = productFromJs(js, item.url, item.html_title, item.sources);
      if (!product) continue;
      if (item.image && !product.images.includes(item.image)) product.images.push(item.image);
      const scoring = scoreCandidate(product, query, expectedTerms);
      verified.push({ ...product, ...scoring });
    }

    const byUrl = new Map();
    for (const c of verified) {
      const prior = byUrl.get(c.url);
      if (!prior || c.score > prior.score) byUrl.set(c.url, c);
    }

    const candidates = [...byUrl.values()]
      .sort((a, b) => b.score - a.score || a.title.localeCompare(b.title))
      .slice(0, maxCandidates)
      .map((c, idx) => ({
        rank: idx + 1,
        title: c.title,
        url: c.url,
        js_url: c.js_url,
        handle: c.handle,
        score: c.score,
        matched_terms: c.matched_terms,
        query_token_matches: c.query_token_matches,
        vendor: c.vendor,
        product_type: c.product_type,
        variants: c.variants,
        images: c.images,
        description: c.description,
        sources: c.sources
      }));

    evidence.discovered_product_urls = [...discovered.keys()];
    evidence.fetch_count = fetchCount;

    safeJsonPrint({
      success: true,
      candidates,
      evidence,
      trace
    });
  } catch (e) {
    safeJsonPrint({
      success: false,
      error: e && e.message ? e.message : String(e),
      fatal: true,
      trace
    });
  }
}

await main();
