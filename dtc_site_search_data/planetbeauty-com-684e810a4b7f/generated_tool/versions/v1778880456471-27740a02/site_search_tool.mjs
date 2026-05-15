const DEFAULT_TIMEOUT_MS = 8000;
const MAX_TOTAL_REQUESTS = 24;

let requestCount = 0;

function out(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function asArray(v) {
  if (Array.isArray(v)) return v.filter(x => x != null).map(String);
  if (v == null) return [];
  return [String(v)];
}

function htmlDecode(s) {
  return String(s || "")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#x2F;/g, "/");
}

function stripTags(s) {
  return htmlDecode(String(s || "").replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();
}

function norm(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokens(s) {
  return norm(s).split(" ").filter(t => t.length > 1);
}

function uniq(arr) {
  return [...new Set(arr.filter(Boolean))];
}

function canonicalBase(siteUrl) {
  try {
    const u = new URL(siteUrl || "https://www.planetbeauty.com/");
    return "https://www.planetbeauty.com";
  } catch {
    return "https://www.planetbeauty.com";
  }
}

function productHandleFromUrl(url) {
  try {
    const u = new URL(url, "https://www.planetbeauty.com");
    const m = u.pathname.match(/\/products\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  } catch {
    const m = String(url || "").match(/\/products\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }
}

function absoluteProductUrl(href, base) {
  try {
    const u = new URL(htmlDecode(href), base);
    const handle = productHandleFromUrl(u.href);
    if (!handle) return null;
    return `${base}/products/${encodeURIComponent(handle).replace(/%2D/g, "-")}`;
  } catch {
    return null;
  }
}

async function fetchText(url, trace, timeoutMs = DEFAULT_TIMEOUT_MS) {
  if (++requestCount > MAX_TOTAL_REQUESTS) throw new Error("request limit exceeded");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const started = Date.now();
  try {
    const res = await fetch(url, {
      method: "GET",
      redirect: "follow",
      signal: controller.signal,
      headers: {
        "accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "user-agent": "HermesDtcSiteSearch/1.0 (+product-search)"
      }
    });
    const text = await res.text();
    trace.push({ step: "fetch", url, status: res.status, ms: Date.now() - started, bytes: text.length });
    if (!res.ok) {
      const e = new Error(`HTTP ${res.status} for ${url}`);
      e.status = res.status;
      e.body = text;
      throw e;
    }
    return text;
  } finally {
    clearTimeout(timer);
  }
}

async function fetchJson(url, trace, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const text = await fetchText(url, trace, timeoutMs);
  return JSON.parse(text);
}

function buildQueries(query, expectedTerms) {
  const rawPieces = [query, ...expectedTerms].map(x => String(x || "").trim()).filter(Boolean);
  const q = String(query || "").trim();
  const all = rawPieces.join(" ");
  const noPunct = norm(q).replace(/\b(and)\b/g, "and");
  const noSizes = tokens(q).filter(t => !/^\d+(\.\d+)?$/.test(t) && !/^(oz|fl|ml|l|liter|litre|ounce|ounces|pack|ct|count|mls)$/.test(t)).join(" ");
  const ts = tokens(q);
  const first2 = ts.slice(0, 2).join(" ");
  const first3 = ts.slice(0, 3).join(" ");
  const first4 = ts.slice(0, 4).join(" ");
  return uniq([q, all, noPunct, noSizes, first4, first3, first2]).filter(x => x && x.length >= 2).slice(0, 7);
}

function extractSuggestProducts(json, base) {
  const products = json?.resources?.results?.products || json?.products || [];
  if (!Array.isArray(products)) return [];
  return products.map(p => {
    const handle = p.handle || productHandleFromUrl(p.url || p.path || "");
    const url = handle ? `${base}/products/${handle}` : absoluteProductUrl(p.url || p.path || "", base);
    return url ? {
      source: "search_suggest_json",
      handle: productHandleFromUrl(url),
      url,
      title: stripTags(p.title || p.name || ""),
      vendor: stripTags(p.vendor || p.brand || ""),
      price: p.price || p.price_min || null,
      image: p.image || p.featured_image || null,
      snippet: stripTags(p.body || p.description || "")
    } : null;
  }).filter(Boolean);
}

function extractProductLinksFromHtml(html, base) {
  const found = [];
  const re = /<a\b[^>]*href\s*=\s*(['"])([^'"]*\/products\/[^'"]+)\1[^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(html))) {
    const url = absoluteProductUrl(m[2], base);
    if (!url) continue;
    const title = stripTags(m[3]);
    found.push({
      source: "search_html",
      handle: productHandleFromUrl(url),
      url,
      title,
      vendor: "",
      snippet: ""
    });
  }

  const jsonUrlRe = /"url"\s*:\s*"([^"]*\/products\/[^"]+)"/gi;
  while ((m = jsonUrlRe.exec(html))) {
    const url = absoluteProductUrl(m[1].replace(/\\\//g, "/"), base);
    if (url) found.push({ source: "search_html_json_fragment", handle: productHandleFromUrl(url), url, title: "", vendor: "", snippet: "" });
  }
  return found;
}

function variantScore(variant, wantedText) {
  const vt = norm([variant?.title, variant?.option1, variant?.option2, variant?.option3, variant?.sku].filter(Boolean).join(" "));
  const wt = norm(wantedText);
  if (!vt || !wt) return 0;
  let s = 0;
  for (const t of tokens(wt)) {
    if (vt.includes(t)) s += 2;
  }
  const sizePatterns = String(wantedText || "").match(/\b\d+(?:\.\d+)?\s*(?:fl\s*)?(?:oz|ml|l|liter|litre)\b/gi) || [];
  for (const p of sizePatterns) {
    const compact = norm(p);
    if (compact && vt.includes(compact)) s += 8;
    const n = compact.match(/\d+(?:\.\d+)?/)?.[0];
    if (n && vt.includes(n)) s += 4;
  }
  return s;
}

function scoreCandidate(c, query, expectedTerms) {
  const hay = norm([
    c.title,
    c.vendor,
    c.handle,
    c.snippet,
    c.product_title,
    c.product_vendor,
    c.description,
    ...(c.variants || []).map(v => v.title || "")
  ].filter(Boolean).join(" "));
  const expected = uniq([...tokens(query), ...expectedTerms.flatMap(tokens)]);
  let matched = [];
  let score = 0;
  for (const t of expected) {
    if (hay.includes(t)) {
      matched.push(t);
      score += t.length <= 2 ? 1 : 3;
    }
  }
  const qn = norm(query);
  if (qn && hay.includes(qn)) score += 30;
  if (c.title && norm(c.title) && qn.includes(norm(c.title))) score += 12;
  if (c.url && /\/products\/[^/?#]+/.test(c.url)) score += 5;
  if (c.available === true) score += 2;
  if (/\bduo|bundle|set|kit\b/.test(hay) && !/\bduo|bundle|set|kit\b/.test(qn)) score -= 8;
  return { score, matched_terms: matched };
}

async function enrichProduct(candidate, base, query, expectedTerms, trace) {
  const handle = candidate.handle || productHandleFromUrl(candidate.url);
  if (!handle) return candidate;

  let enriched = { ...candidate, handle, url: `${base}/products/${handle}` };

  try {
    const product = await fetchJson(`${base}/products/${encodeURIComponent(handle).replace(/%2D/g, "-")}.js`, trace);
    const variants = Array.isArray(product.variants) ? product.variants : [];
    const wantedText = [query, ...expectedTerms].join(" ");
    let bestVariant = null;
    let bestVariantScore = -1;
    for (const v of variants) {
      const s = variantScore(v, wantedText);
      if (s > bestVariantScore) {
        bestVariant = v;
        bestVariantScore = s;
      }
    }
    const useVariant = bestVariant && (bestVariantScore > 0 || variants.length === 1) ? bestVariant : null;
    enriched = {
      ...enriched,
      source: `${enriched.source}+product_json`,
      product_id: product.id || null,
      title: stripTags(product.title || enriched.title || ""),
      product_title: stripTags(product.title || ""),
      vendor: stripTags(product.vendor || enriched.vendor || ""),
      product_vendor: stripTags(product.vendor || ""),
      product_type: product.type || "",
      description: stripTags(product.description || ""),
      tags: Array.isArray(product.tags) ? product.tags : [],
      available: typeof product.available === "boolean" ? product.available : null,
      price: product.price || enriched.price || null,
      variants: variants.slice(0, 12).map(v => ({
        id: v.id,
        title: v.title,
        sku: v.sku || "",
        available: typeof v.available === "boolean" ? v.available : null,
        price: v.price || null,
        option1: v.option1 || null,
        option2: v.option2 || null,
        option3: v.option3 || null
      })),
      selected_variant: useVariant ? {
        id: useVariant.id,
        title: useVariant.title,
        sku: useVariant.sku || "",
        available: typeof useVariant.available === "boolean" ? useVariant.available : null,
        price: useVariant.price || null
      } : null,
      url: useVariant?.id ? `${base}/products/${handle}?variant=${useVariant.id}` : `${base}/products/${handle}`
    };
  } catch (e) {
    trace.push({ step: "product_json_failed", handle, error: String(e.message || e).slice(0, 220) });
    try {
      const html = await fetchText(`${base}/products/${encodeURIComponent(handle).replace(/%2D/g, "-")}`, trace);
      const title = stripTags((html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i) || [])[1] || (html.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [])[1] || enriched.title || "");
      const vendor = stripTags((html.match(/"brand"\s*:\s*"?([^",}]+)"?/i) || [])[1] || enriched.vendor || "");
      enriched = { ...enriched, source: `${enriched.source}+product_html`, title, vendor, html_excerpt: stripTags(html).slice(0, 600) };
    } catch (e2) {
      trace.push({ step: "product_html_failed", handle, error: String(e2.message || e2).slice(0, 220) });
    }
  }

  const scoring = scoreCandidate(enriched, query, expectedTerms);
  enriched.score = scoring.score;
  enriched.matched_terms = scoring.matched_terms;
  return enriched;
}

async function main() {
  const trace = [];
  try {
    const input = JSON.parse(process.argv[2] || "{}");
    const base = canonicalBase(input.site_url);
    const query = String(input.query || "").trim();
    const expectedTerms = asArray(input.expected_terms);
    const maxCandidates = Math.max(1, Math.min(25, Number(input.max_candidates || 10)));

    if (!query) {
      out({ success: false, error: "missing query", fatal: true, trace });
      return;
    }

    trace.push({ step: "start", site: base, strategy: "shopify_search_suggest_then_search_html_then_product_json", query });

    const queries = buildQueries(query, expectedTerms);
    const byHandle = new Map();

    for (const q of queries) {
      if (byHandle.size >= maxCandidates * 2) break;

      const suggestUrl = `${base}/search/suggest.json?q=${encodeURIComponent(q)}&resources%5Btype%5D=product&resources%5Blimit%5D=${Math.min(10, Math.max(maxCandidates, 4))}`;
      try {
        const json = await fetchJson(suggestUrl, trace);
        const products = extractSuggestProducts(json, base);
        trace.push({ step: "search_suggest_results", query: q, count: products.length });
        for (const p of products) {
          if (p.handle && !byHandle.has(p.handle)) byHandle.set(p.handle, { ...p, search_query: q });
        }
      } catch (e) {
        trace.push({ step: "search_suggest_failed", query: q, error: String(e.message || e).slice(0, 220) });
      }

      if (byHandle.size < maxCandidates) {
        const searchUrl = `${base}/search?type=product&q=${encodeURIComponent(q)}`;
        try {
          const html = await fetchText(searchUrl, trace);
          const products = extractProductLinksFromHtml(html, base);
          trace.push({ step: "search_html_results", query: q, count: products.length });
          for (const p of products) {
            if (p.handle && !byHandle.has(p.handle)) byHandle.set(p.handle, { ...p, search_query: q });
          }
        } catch (e) {
          trace.push({ step: "search_html_failed", query: q, error: String(e.message || e).slice(0, 220) });
        }
      }
    }

    let prelim = [...byHandle.values()].map(c => {
      const scoring = scoreCandidate(c, query, expectedTerms);
      return { ...c, score: scoring.score, matched_terms: scoring.matched_terms };
    }).sort((a, b) => b.score - a.score).slice(0, Math.min(maxCandidates * 2, 16));

    const enriched = [];
    for (const c of prelim) {
      if (enriched.length >= maxCandidates) break;
      enriched.push(await enrichProduct(c, base, query, expectedTerms, trace));
    }

    const candidates = enriched
      .sort((a, b) => (b.score || 0) - (a.score || 0))
      .slice(0, maxCandidates)
      .map(c => ({
        url: c.url,
        title: c.title || c.product_title || "",
        brand: c.vendor || c.product_vendor || "",
        handle: c.handle,
        score: c.score || 0,
        matched_terms: c.matched_terms || [],
        available: c.available,
        price: c.price,
        selected_variant: c.selected_variant || null,
        variants: c.variants || undefined,
        evidence: {
          source: c.source,
          search_query: c.search_query,
          product_type: c.product_type || "",
          description_excerpt: c.description ? c.description.slice(0, 500) : (c.snippet || c.html_excerpt || "").slice(0, 500),
          tags: c.tags || []
        }
      }));

    trace.push({ step: "complete", candidate_count: candidates.length, requests: requestCount });

    out({
      success: true,
      candidates,
      evidence: {
        site: base,
        queries_attempted: queries,
        product_url_pattern: `${base}/products/{product-handle}`,
        search_endpoints: ["/search/suggest.json", "/search?type=product&q="]
      },
      trace
    });
  } catch (e) {
    out({
      success: false,
      error: String(e && e.message ? e.message : e),
      fatal: true,
      trace
    });
  }
}

await main();
