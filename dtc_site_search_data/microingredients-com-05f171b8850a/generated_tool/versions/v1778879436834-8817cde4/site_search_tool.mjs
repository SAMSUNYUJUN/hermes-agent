#!/usr/bin/env node

const DEFAULT_TIMEOUT_MS = 10000;
const CANONICAL_ORIGIN = "https://www.microingredients.com";

function nowIso() {
  return new Date().toISOString();
}

function pushTrace(trace, step, data = {}) {
  trace.push({ t: nowIso(), step, ...data });
}

function stdout(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function htmlDecode(s) {
  if (!s) return "";
  return String(s)
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#x2F;/g, "/")
    .replace(/&#x27;/g, "'")
    .replace(/&#(\d+);/g, (_, n) => {
      try {
        return String.fromCharCode(Number(n));
      } catch {
        return _;
      }
    });
}

function stripTags(s) {
  return htmlDecode(String(s || "").replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ").replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();
}

function cleanUrl(raw) {
  if (!raw) return null;
  let u = htmlDecode(String(raw).trim());
  if (!u || u.startsWith("#") || /^javascript:/i.test(u) || /^mailto:/i.test(u)) return null;
  try {
    const url = new URL(u, CANONICAL_ORIGIN);
    if (!/microingredients\.com$/i.test(url.hostname)) return null;
    url.protocol = "https:";
    url.hostname = "www.microingredients.com";
    url.hash = "";
    return url.toString();
  } catch {
    return null;
  }
}

function cleanProductUrl(url) {
  const u = cleanUrl(url);
  if (!u) return null;
  const parsed = new URL(u);
  const m = parsed.pathname.match(/^\/products\/([^/?#]+)(?:\.js)?\/?$/i);
  if (!m) return null;
  return `${CANONICAL_ORIGIN}/products/${m[1]}`;
}

function handleFromProductUrl(url) {
  const u = cleanProductUrl(url);
  if (!u) return null;
  return new URL(u).pathname.split("/").filter(Boolean).pop();
}

async function fetchWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error("timeout")), timeoutMs);
  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal,
      redirect: "follow",
      headers: {
        "accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "user-agent": "HermesDTCSearchTool/1.0",
        ...(options.headers || {})
      }
    });
    return res;
  } finally {
    clearTimeout(timer);
  }
}

async function fetchText(url, trace, label) {
  const started = Date.now();
  try {
    const res = await fetchWithTimeout(url);
    const text = await res.text();
    pushTrace(trace, label, { url, status: res.status, ok: res.ok, bytes: text.length, ms: Date.now() - started });
    return { ok: res.ok, status: res.status, text, finalUrl: res.url };
  } catch (e) {
    pushTrace(trace, `${label}_failed`, { url, error: String(e && e.message ? e.message : e), ms: Date.now() - started });
    return { ok: false, status: 0, text: "", error: String(e && e.message ? e.message : e) };
  }
}

async function fetchJson(url, trace, label) {
  const started = Date.now();
  try {
    const res = await fetchWithTimeout(url, { headers: { "accept": "application/json,text/javascript,*/*;q=0.8" } });
    const text = await res.text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch (e) {
      pushTrace(trace, `${label}_json_parse_failed`, { url, status: res.status, ok: res.ok, bytes: text.length, error: String(e && e.message ? e.message : e), ms: Date.now() - started });
      return { ok: false, status: res.status, json: null, text, finalUrl: res.url };
    }
    pushTrace(trace, label, { url, status: res.status, ok: res.ok, bytes: text.length, ms: Date.now() - started });
    return { ok: res.ok, status: res.status, json, text, finalUrl: res.url };
  } catch (e) {
    pushTrace(trace, `${label}_failed`, { url, error: String(e && e.message ? e.message : e), ms: Date.now() - started });
    return { ok: false, status: 0, json: null, text: "", error: String(e && e.message ? e.message : e) };
  }
}

function tokenize(s) {
  return Array.from(new Set(String(s || "").toLowerCase().replace(/[+]/g, " plus ").match(/[a-z0-9]+/g) || []))
    .filter(t => t.length > 1 && !STOPWORDS.has(t));
}

const STOPWORDS = new Set([
  "the", "and", "for", "with", "from", "micro", "ingredients", "ingredient", "supplement", "supplements",
  "advanced", "highly", "bioavailable", "made", "oil", "organic", "pure", "powder", "capsule", "capsules",
  "softgel", "softgels", "veggie", "serving", "servings", "pack", "packs", "count", "ct", "mg", "g", "oz",
  "lb", "lbs", "equivalent"
]);

function makeSearchQueries(query, expectedTerms) {
  const out = [];
  const add = q => {
    q = String(q || "").replace(/\s+/g, " ").trim();
    if (q && !out.some(x => x.toLowerCase() === q.toLowerCase())) out.push(q);
  };

  add(query);
  if (Array.isArray(expectedTerms) && expectedTerms.length) add(expectedTerms.join(" "));

  let q = String(query || "")
    .replace(/\bMicro\s+Ingredients\b/ig, " ")
    .replace(/[™®]/g, " ")
    .replace(/\b\d+(?:[,.]\d+)?\s*(?:mg|mcg|g|kg|oz|lb|lbs|iu|fl\s*oz)\b/ig, " ")
    .replace(/\b\d{3,}\b/g, " ")
    .replace(/[(),:;|/]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  add(q);

  const words = String(q || query || "").split(/\s+/).filter(Boolean);
  const formWords = words.filter(w => /^(softgels?|capsules?|caps?|tablets?|powder|gummies|drops|liquid|bundle|pack)$/i.test(w));
  const meaningful = words.filter(w => !/^\d+$/.test(w) && !STOPWORDS.has(w.toLowerCase().replace(/[^a-z0-9]/g, "")));
  if (meaningful.length || formWords.length) add([...meaningful.slice(0, 4), ...formWords.slice(0, 2)].join(" "));

  if (Array.isArray(expectedTerms)) {
    for (const term of expectedTerms.slice(0, 3)) add(term);
  }

  return out.slice(0, 5);
}

function extractProductLinksFromSearchHtml(html) {
  const found = new Map();

  const hrefRe = /<a\b[^>]*href\s*=\s*["']([^"']*\/products\/[^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = hrefRe.exec(html))) {
    const url = cleanProductUrl(m[1]);
    if (!url) continue;
    const title = stripTags(m[2]);
    if (!found.has(url)) found.set(url, { url, title: title || null, source: "search_html_anchor" });
  }

  const anyProductRe = /(?:href|data-url|url)\s*=\s*["']([^"']*\/products\/[^"']+)["']/gi;
  while ((m = anyProductRe.exec(html))) {
    const url = cleanProductUrl(m[1]);
    if (!url) continue;
    if (!found.has(url)) found.set(url, { url, title: null, source: "search_html_attr" });
  }

  const jsonUrlRe = /\\?["'](?:url|href|path)\\?["']\s*:\s*\\?["']([^"']*\/products\/[^"'\\]+)\\?["']/gi;
  while ((m = jsonUrlRe.exec(html))) {
    const url = cleanProductUrl(m[1].replace(/\\\//g, "/"));
    if (!url) continue;
    if (!found.has(url)) found.set(url, { url, title: null, source: "search_embedded_json" });
  }

  return Array.from(found.values());
}

function extractPredictiveProducts(json) {
  const out = [];
  const seen = new Set();

  function visit(node) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const x of node) visit(x);
      return;
    }

    const url = cleanProductUrl(node.url || node.href || node.handle && `/products/${node.handle}`);
    if (url && !seen.has(url)) {
      seen.add(url);
      out.push({
        url,
        title: stripTags(node.title || node.product_title || node.name || ""),
        source: "predictive_search_json"
      });
    }

    for (const v of Object.values(node)) {
      if (v && typeof v === "object") visit(v);
    }
  }

  visit(json);
  return out;
}

function productJsonUrl(productUrl) {
  return `${cleanProductUrl(productUrl)}.js`;
}

function normalizeProductJson(productUrl, json) {
  const handle = json && (json.handle || handleFromProductUrl(productUrl));
  const variants = Array.isArray(json && json.variants) ? json.variants : [];
  const firstAvailable = variants.find(v => v && v.available) || variants[0] || null;
  const priceCents = firstAvailable && (typeof firstAvailable.price === "number" ? firstAvailable.price : Number(firstAvailable.price));
  const images = [];
  if (Array.isArray(json && json.images)) {
    for (const img of json.images.slice(0, 6)) {
      if (typeof img === "string") images.push(img.startsWith("//") ? `https:${img}` : img);
      else if (img && typeof img.src === "string") images.push(img.src.startsWith("//") ? `https:${img.src}` : img.src);
    }
  }

  return {
    title: String((json && json.title) || "").trim(),
    handle,
    url: cleanProductUrl(productUrl),
    product_json_url: productJsonUrl(productUrl),
    available: variants.length ? variants.some(v => Boolean(v && v.available)) : undefined,
    price: Number.isFinite(priceCents) ? (priceCents / 100).toFixed(2) : undefined,
    sku: firstAvailable && firstAvailable.sku ? String(firstAvailable.sku) : undefined,
    variant_title: firstAvailable && firstAvailable.title ? String(firstAvailable.title) : undefined,
    vendor: json && json.vendor ? String(json.vendor) : undefined,
    product_type: json && json.type ? String(json.type) : undefined,
    description_text: stripTags(json && (json.description || json.body_html || "")),
    images
  };
}

function scoreCandidate(candidate, query, expectedTerms) {
  const hay = [
    candidate.title,
    candidate.handle,
    candidate.description_text,
    candidate.sku,
    candidate.variant_title,
    candidate.product_type
  ].filter(Boolean).join(" ").toLowerCase();

  const q = String(query || "").toLowerCase().trim();
  let score = 0;
  const matched = [];

  if (q && hay.includes(q)) score += 50;

  const expected = Array.isArray(expectedTerms) ? expectedTerms : [];
  for (const term of expected) {
    const t = String(term || "").toLowerCase().trim();
    if (!t) continue;
    if (hay.includes(t)) {
      score += Math.max(8, Math.min(30, t.length));
      matched.push(term);
    } else {
      const toks = tokenize(t);
      const hits = toks.filter(tok => hay.includes(tok));
      if (hits.length) {
        score += hits.length * 4;
        matched.push(...hits);
      }
    }
  }

  const qTokens = tokenize(query);
  for (const tok of qTokens) {
    if (hay.includes(tok)) {
      score += 3;
      matched.push(tok);
    }
  }

  if (candidate.available === true) score += 2;
  if (/bundle|kit|pack/i.test(String(query)) && /bundle|kit|pack/i.test(hay)) score += 10;
  if (candidate.title && /micro ingredients/i.test(candidate.title)) score += 1;

  return { score, matched_terms: Array.from(new Set(matched.map(String))).slice(0, 25) };
}

async function main() {
  const trace = [];
  try {
    if (!process.argv[2]) throw new Error("Missing JSON argument at process.argv[2]");
    let input;
    try {
      input = JSON.parse(process.argv[2]);
    } catch (e) {
      throw new Error(`Invalid JSON argument: ${e && e.message ? e.message : String(e)}`);
    }

    const query = String(input.query || "").trim();
    if (!query) throw new Error("Input query is required");
    const expectedTerms = Array.isArray(input.expected_terms) ? input.expected_terms.map(x => String(x)).filter(Boolean) : [];
    const maxCandidatesRaw = Number(input.max_candidates);
    const maxCandidates = Number.isFinite(maxCandidatesRaw) && maxCandidatesRaw > 0 ? Math.min(Math.floor(maxCandidatesRaw), 20) : 5;

    pushTrace(trace, "start", {
      requested_site_url: input.site_url || null,
      canonical_origin: CANONICAL_ORIGIN,
      query,
      expected_terms_count: expectedTerms.length,
      max_candidates: maxCandidates
    });

    const searchQueries = makeSearchQueries(query, expectedTerms);
    pushTrace(trace, "search_queries_built", { search_queries: searchQueries });

    const discovered = new Map();
    const evidence = {
      canonical_origin: CANONICAL_ORIGIN,
      strategy: "Micro Ingredients Shopify direct search URL plus optional predictive JSON and product .js verification",
      search_urls: [],
      predictive_urls: [],
      product_json_urls: []
    };

    for (const sq of searchQueries) {
      const searchUrl = `${CANONICAL_ORIGIN}/search?q=${encodeURIComponent(sq)}`;
      evidence.search_urls.push(searchUrl);
      const page = await fetchText(searchUrl, trace, "fetch_search_html");
      if (page.text) {
        const products = extractProductLinksFromSearchHtml(page.text);
        pushTrace(trace, "search_html_products_extracted", { query: sq, count: products.length });
        for (const p of products) {
          if (!discovered.has(p.url)) discovered.set(p.url, { ...p, search_query: sq });
        }
      }

      const predictiveUrl = `${CANONICAL_ORIGIN}/search/suggest.json?q=${encodeURIComponent(sq)}&resources[type]=product&resources[limit]=10`;
      evidence.predictive_urls.push(predictiveUrl);
      const pred = await fetchJson(predictiveUrl, trace, "fetch_predictive_json");
      if (pred.json) {
        const products = extractPredictiveProducts(pred.json);
        pushTrace(trace, "predictive_products_extracted", { query: sq, count: products.length });
        for (const p of products) {
          if (!discovered.has(p.url)) discovered.set(p.url, { ...p, search_query: sq });
        }
      }

      if (discovered.size >= Math.max(maxCandidates * 2, 10)) break;
    }

    pushTrace(trace, "discovery_complete", { discovered_count: discovered.size });

    const candidates = [];
    const productEntries = Array.from(discovered.values()).slice(0, Math.max(maxCandidates * 3, 12));

    for (const entry of productEntries) {
      const jsonUrl = productJsonUrl(entry.url);
      evidence.product_json_urls.push(jsonUrl);
      const pj = await fetchJson(jsonUrl, trace, "fetch_product_js");
      let candidate;
      if (pj.ok && pj.json && typeof pj.json === "object") {
        candidate = normalizeProductJson(entry.url, pj.json);
      } else {
        candidate = {
          title: entry.title || "",
          handle: handleFromProductUrl(entry.url),
          url: cleanProductUrl(entry.url),
          product_json_url: jsonUrl,
          description_text: "",
          images: []
        };
      }

      if (!candidate.title && entry.title) candidate.title = entry.title;
      candidate.source = entry.source;
      candidate.search_query = entry.search_query;

      const scored = scoreCandidate(candidate, query, expectedTerms);
      candidate.score = scored.score;
      candidate.matched_terms = scored.matched_terms;

      delete candidate.description_text;
      candidates.push(candidate);
    }

    candidates.sort((a, b) => {
      if ((b.score || 0) !== (a.score || 0)) return (b.score || 0) - (a.score || 0);
      return String(a.title || a.handle || "").localeCompare(String(b.title || b.handle || ""));
    });

    const finalCandidates = candidates.slice(0, maxCandidates).map(c => {
      const out = {};
      for (const [k, v] of Object.entries(c)) {
        if (v !== undefined && v !== null && !(Array.isArray(v) && v.length === 0)) out[k] = v;
      }
      return out;
    });

    pushTrace(trace, "complete", { candidate_count: finalCandidates.length });

    stdout({
      success: true,
      candidates: finalCandidates,
      evidence,
      trace
    });
  } catch (e) {
    pushTrace(trace, "fatal_error", { error: String(e && e.message ? e.message : e) });
    stdout({
      success: false,
      error: String(e && e.message ? e.message : e),
      fatal: true,
      trace
    });
  }
}

main();
