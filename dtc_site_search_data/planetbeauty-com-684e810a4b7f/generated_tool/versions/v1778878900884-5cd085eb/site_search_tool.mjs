const DEFAULT_SITE = "https://www.planetbeauty.com/";

function nowIso() {
  return new Date().toISOString();
}

function safeJsonParse(s) {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

function normalizePlanetBeautyBase(input) {
  try {
    const u = new URL(input || DEFAULT_SITE);
    return `${u.protocol === "http:" ? "https:" : u.protocol}//www.planetbeauty.com`;
  } catch {
    return "https://www.planetbeauty.com";
  }
}

function asArray(v) {
  if (Array.isArray(v)) return v.filter((x) => x != null).map(String).filter(Boolean);
  if (typeof v === "string" && v.trim()) return [v.trim()];
  return [];
}

function decodeHtml(s) {
  if (!s) return "";
  const named = {
    amp: "&",
    lt: "<",
    gt: ">",
    quot: "\"",
    apos: "'",
    nbsp: " ",
    rsquo: "’",
    lsquo: "‘",
    rdquo: "”",
    ldquo: "“",
    hellip: "…",
  };
  return String(s)
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
    .replace(/&#x([0-9a-fA-F]+);/g, (_, n) => String.fromCharCode(parseInt(n, 16)))
    .replace(/&([a-zA-Z]+);/g, (m, n) => (Object.prototype.hasOwnProperty.call(named, n) ? named[n] : m));
}

function stripTags(s) {
  return decodeHtml(String(s || "").replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " "))
    .replace(/\s+/g, " ")
    .trim();
}

function norm(s) {
  return String(s || "")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[’‘]/g, "'")
    .replace(/[^a-z0-9.'$%]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokenSetFrom(strings) {
  const stop = new Set(["and", "the", "for", "with", "a", "an", "of", "to", "in", "on", "by"]);
  const out = [];
  const seen = new Set();
  for (const s of strings) {
    for (const t of norm(s).split(/\s+/)) {
      if (!t || t.length < 2 || stop.has(t)) continue;
      if (!seen.has(t)) {
        seen.add(t);
        out.push(t);
      }
    }
  }
  return out;
}

function absoluteUrl(base, href) {
  try {
    return new URL(href, base).toString();
  } catch {
    return null;
  }
}

function canonicalProductUrl(base, raw) {
  const abs = absoluteUrl(base, raw);
  if (!abs) return null;
  const u = new URL(abs);
  const m = u.pathname.match(/^\/products\/([^/?#]+)/);
  if (!m) return null;
  const variant = u.searchParams.get("variant");
  const out = new URL(`${u.origin}/products/${m[1]}`);
  if (variant) out.searchParams.set("variant", variant);
  return out.toString();
}

function productHandleFromUrl(url) {
  try {
    const u = new URL(url);
    const m = u.pathname.match(/^\/products\/([^/?#]+)/);
    return m ? m[1] : "";
  } catch {
    return "";
  }
}

async function fetchWithTimeout(url, opts = {}) {
  const timeoutMs = opts.timeoutMs || 9000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: opts.method || "GET",
      headers: {
        "accept": opts.accept || "*/*",
        "user-agent": "Mozilla/5.0 compatible; HermesDTCSearch/1.0",
        ...(opts.headers || {}),
      },
      redirect: "follow",
      signal: controller.signal,
    });
    const text = await res.text();
    return { ok: res.ok, status: res.status, url: res.url, text, headers: res.headers };
  } finally {
    clearTimeout(timer);
  }
}

async function getJson(url, trace, label) {
  try {
    const r = await fetchWithTimeout(url, { accept: "application/json,text/javascript,*/*", timeoutMs: 8500 });
    trace.push({ step: label, url, status: r.status, ok: r.ok, final_url: r.url });
    if (!r.ok) return null;
    return safeJsonParse(r.text);
  } catch (e) {
    trace.push({ step: label, url, ok: false, error: String(e && e.message ? e.message : e) });
    return null;
  }
}

async function getText(url, trace, label) {
  try {
    const r = await fetchWithTimeout(url, { accept: "text/html,*/*", timeoutMs: 8500 });
    trace.push({ step: label, url, status: r.status, ok: r.ok, final_url: r.url });
    if (!r.ok) return "";
    return r.text || "";
  } catch (e) {
    trace.push({ step: label, url, ok: false, error: String(e && e.message ? e.message : e) });
    return "";
  }
}

function candidateFromPredictiveProduct(base, p) {
  const url = canonicalProductUrl(base, p.url || p.href || (p.handle ? `/products/${p.handle}` : ""));
  if (!url) return null;
  const variants = Array.isArray(p.variants) ? p.variants : [];
  const variant = variants[0] || {};
  return {
    url,
    title: stripTags(p.title || p.name || p.product_title || ""),
    brand: stripTags(p.vendor || p.brand || ""),
    price: p.price || p.price_min || variant.price || null,
    image: p.image || p.featured_image || null,
    handle: p.handle || productHandleFromUrl(url),
    source: "shopify_predictive_search",
    raw_variant_id: variant.id ? String(variant.id) : undefined,
  };
}

function extractPredictiveCandidates(base, json) {
  const out = [];
  const paths = [
    json && json.resources && json.resources.results && json.resources.results.products,
    json && json.resources && json.resources.products,
    json && json.products,
    json && json.results && json.results.products,
  ];
  for (const arr of paths) {
    if (!Array.isArray(arr)) continue;
    for (const p of arr) {
      const c = candidateFromPredictiveProduct(base, p || {});
      if (c) out.push(c);
    }
  }
  return out;
}

function extractHtmlCandidates(base, html) {
  const out = [];
  const seen = new Set();
  const anchorRe = /<a\b[^>]*href\s*=\s*["']([^"']*\/products\/[^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = anchorRe.exec(html))) {
    const url = canonicalProductUrl(base, decodeHtml(m[1]));
    if (!url || seen.has(url)) continue;
    seen.add(url);
    let title = stripTags(m[2]);
    if (!title || title.length < 2 || title.length > 180) {
      const around = html.slice(Math.max(0, m.index - 800), Math.min(html.length, m.index + 1600));
      const titleMatch =
        around.match(/class=["'][^"']*(?:product[^"']*title|card[^"']*heading|title)[^"']*["'][^>]*>([\s\S]{1,300}?)<\/[^>]+>/i) ||
        around.match(/aria-label=["']([^"']{2,180})["']/i) ||
        around.match(/title=["']([^"']{2,180})["']/i);
      title = titleMatch ? stripTags(titleMatch[1]) : "";
    }
    out.push({
      url,
      title,
      brand: "",
      price: null,
      image: null,
      handle: productHandleFromUrl(url),
      source: "search_html",
    });
  }

  const hrefRe = /href\s*=\s*["']([^"']*\/products\/[^"']+)["']/gi;
  while ((m = hrefRe.exec(html))) {
    const url = canonicalProductUrl(base, decodeHtml(m[1]));
    if (!url || seen.has(url)) continue;
    seen.add(url);
    out.push({
      url,
      title: "",
      brand: "",
      price: null,
      image: null,
      handle: productHandleFromUrl(url),
      source: "search_html_href",
    });
  }
  return out;
}

function extractLdJsonProducts(base, html) {
  const out = [];
  const re = /<script\b[^>]*type\s*=\s*["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    const parsed = safeJsonParse(decodeHtml(m[1]).trim());
    const nodes = [];
    if (Array.isArray(parsed)) nodes.push(...parsed);
    else if (parsed) nodes.push(parsed);
    for (const n of nodes) {
      const graph = Array.isArray(n && n["@graph"]) ? n["@graph"] : [n];
      for (const item of graph) {
        if (!item || !/Product/i.test(String(item["@type"] || ""))) continue;
        const url = canonicalProductUrl(base, item.url || "");
        if (!url) continue;
        out.push({
          url,
          title: stripTags(item.name || ""),
          brand: stripTags((item.brand && (item.brand.name || item.brand)) || ""),
          price: item.offers && (Array.isArray(item.offers) ? item.offers[0] : item.offers).price ? String((Array.isArray(item.offers) ? item.offers[0] : item.offers).price) : null,
          image: Array.isArray(item.image) ? item.image[0] : item.image || null,
          handle: productHandleFromUrl(url),
          source: "product_ld_json",
        });
      }
    }
  }
  return out;
}

function scoreCandidate(c, query, expectedTerms) {
  const hay = norm([c.title, c.brand, c.url, c.description, c.variant_title].filter(Boolean).join(" "));
  const terms = expectedTerms.length ? expectedTerms : tokenSetFrom([query]);
  const queryTokens = tokenSetFrom([query]);
  const matched = [];
  let score = 0;

  for (const term of terms) {
    const nt = norm(term);
    if (!nt) continue;
    if (hay.includes(nt)) {
      matched.push(term);
      score += nt.includes(" ") ? 8 : 4;
    } else {
      const toks = tokenSetFrom([nt]);
      const hits = toks.filter((t) => hay.includes(t));
      if (hits.length) {
        matched.push(term);
        score += Math.min(6, hits.length * 2);
      }
    }
  }

  for (const t of queryTokens) {
    if (hay.includes(t)) score += 1;
  }

  if (/\/products\/[^/?#]+/.test(c.url || "")) score += 5;
  if (c.title && norm(c.title) === norm(query)) score += 20;
  if (c.title && norm(query).includes(norm(c.title)) && norm(c.title).length > 8) score += 8;

  return { score, matched_terms: [...new Set(matched)] };
}

function mergeCandidates(candidates) {
  const map = new Map();
  for (const c of candidates) {
    if (!c || !c.url) continue;
    const key = productHandleFromUrl(c.url) || c.url;
    const prev = map.get(key);
    if (!prev) {
      map.set(key, { ...c });
      continue;
    }
    map.set(key, {
      ...prev,
      ...Object.fromEntries(Object.entries(c).filter(([, v]) => v !== undefined && v !== null && v !== "")),
      sources: [...new Set([...(prev.sources || [prev.source].filter(Boolean)), c.source].filter(Boolean))],
      source: prev.source || c.source,
      url: prev.url.includes("?variant=") ? prev.url : c.url,
    });
  }
  return [...map.values()];
}

async function enrichWithShopifyProductJson(base, candidates, trace) {
  const enriched = [];
  for (const c of candidates) {
    const handle = c.handle || productHandleFromUrl(c.url);
    if (!handle) {
      enriched.push(c);
      continue;
    }
    const productJsonUrl = `${base}/products/${encodeURIComponent(handle)}.js`;
    const json = await getJson(productJsonUrl, trace, "product_json");
    if (!json || !json.title) {
      enriched.push(c);
      continue;
    }

    const variants = Array.isArray(json.variants) ? json.variants : [];
    let selectedVariantId = null;
    try {
      selectedVariantId = new URL(c.url).searchParams.get("variant");
    } catch {}
    let selectedVariant = selectedVariantId ? variants.find((v) => String(v.id) === String(selectedVariantId)) : null;
    if (!selectedVariant && variants.length) selectedVariant = variants.find((v) => v.available) || variants[0];

    let url = `${base}/products/${handle}`;
    if (selectedVariant && selectedVariant.id) url += `?variant=${encodeURIComponent(String(selectedVariant.id))}`;

    enriched.push({
      ...c,
      url,
      title: stripTags(json.title || c.title || ""),
      brand: stripTags(json.vendor || c.brand || ""),
      product_type: json.type || undefined,
      price: selectedVariant && selectedVariant.price != null ? selectedVariant.price : c.price,
      variant_id: selectedVariant && selectedVariant.id ? String(selectedVariant.id) : undefined,
      variant_title: selectedVariant && selectedVariant.title ? stripTags(selectedVariant.title) : undefined,
      available: selectedVariant && typeof selectedVariant.available === "boolean" ? selectedVariant.available : undefined,
      image: (json.featured_image && (json.featured_image.startsWith("//") ? `https:${json.featured_image}` : json.featured_image)) || c.image || null,
      description: stripTags(json.description || "").slice(0, 800),
      handle,
      sources: [...new Set([...(c.sources || [c.source].filter(Boolean)), "shopify_product_json"])],
    });
  }
  return enriched;
}

async function main() {
  const trace = [];
  const started_at = nowIso();

  let input;
  try {
    input = JSON.parse(process.argv[2] || "{}");
  } catch (e) {
    return {
      success: false,
      error: `Invalid JSON argument: ${String(e && e.message ? e.message : e)}`,
      fatal: true,
      trace,
    };
  }

  const base = normalizePlanetBeautyBase(input.site_url || DEFAULT_SITE);
  const query = String(input.query || "").trim();
  const expected_terms = asArray(input.expected_terms);
  const maxCandidatesRaw = Number(input.max_candidates);
  const max_candidates = Number.isFinite(maxCandidatesRaw) && maxCandidatesRaw > 0 ? Math.min(25, Math.floor(maxCandidatesRaw)) : 5;

  trace.push({
    step: "init",
    site_url: input.site_url || "",
    base,
    query,
    expected_terms,
    max_candidates,
    note: "Planet Beauty skill: use www host, site search, extract /products/{handle} links; no cart/login/forms.",
  });

  if (!query) {
    return {
      success: true,
      candidates: [],
      evidence: {
        site: base,
        search_query: query,
        reason: "empty query",
        product_url_pattern: `${base}/products/{product-handle}`,
        started_at,
        completed_at: nowIso(),
      },
      trace,
    };
  }

  const all = [];
  const limit = Math.max(max_candidates, 10);
  const suggestUrls = [
    `${base}/search/suggest.json?q=${encodeURIComponent(query)}&resources[type]=product&resources[limit]=${encodeURIComponent(String(limit))}&resources[options][unavailable_products]=last&resources[options][fields]=title,product_type,variants.title,vendor`,
    `${base}/search/suggest.json?q=${encodeURIComponent(query)}&resources[type]=product&resources[limit]=${encodeURIComponent(String(limit))}`,
  ];

  for (const url of suggestUrls) {
    const json = await getJson(url, trace, "predictive_search");
    const extracted = extractPredictiveCandidates(base, json);
    trace.push({ step: "predictive_search_extract", count: extracted.length });
    all.push(...extracted);
  }

  const htmlUrls = [
    `${base}/search?q=${encodeURIComponent(query)}&type=product`,
    `${base}/search?type=product&q=${encodeURIComponent(query)}`,
  ];

  for (const url of htmlUrls) {
    const html = await getText(url, trace, "search_html");
    if (!html) continue;
    const htmlCandidates = extractHtmlCandidates(base, html);
    const ldCandidates = extractLdJsonProducts(base, html);
    trace.push({ step: "search_html_extract", count: htmlCandidates.length, ld_json_count: ldCandidates.length });
    all.push(...htmlCandidates, ...ldCandidates);
  }

  let merged = mergeCandidates(all).slice(0, Math.max(20, max_candidates * 3));
  trace.push({ step: "dedupe_before_enrich", count: merged.length });

  merged = await enrichWithShopifyProductJson(base, merged.slice(0, Math.max(12, max_candidates * 2)), trace);

  const scoringTerms = expected_terms.length ? expected_terms : tokenSetFrom([query]);
  const scored = merged
    .map((c) => {
      const s = scoreCandidate(c, query, scoringTerms);
      return {
        url: c.url,
        title: c.title || "",
        brand: c.brand || "",
        price: c.price ?? null,
        variant_id: c.variant_id,
        variant_title: c.variant_title,
        available: c.available,
        image: c.image || null,
        score: s.score,
        matched_terms: s.matched_terms,
        source: c.sources || [c.source].filter(Boolean),
        evidence_text: [c.brand, c.title, c.variant_title, c.product_type].filter(Boolean).join(" | "),
      };
    })
    .sort((a, b) => b.score - a.score || String(a.title).localeCompare(String(b.title)))
    .slice(0, max_candidates);

  return {
    success: true,
    candidates: scored,
    evidence: {
      site: base,
      search_query: query,
      expected_terms,
      strategy: [
        "Shopify predictive search JSON endpoint",
        "Planet Beauty search results HTML fallback",
        "Shopify /products/{handle}.js enrichment",
      ],
      product_url_pattern: `${base}/products/{product-handle}?variant={variant_id}`,
      raw_candidate_count: all.length,
      deduped_candidate_count: merged.length,
      returned_candidate_count: scored.length,
      started_at,
      completed_at: nowIso(),
    },
    trace,
  };
}

main()
  .then((result) => {
    process.stdout.write(JSON.stringify(result));
  })
  .catch((e) => {
    const trace = [{ step: "fatal", error: String(e && e.stack ? e.stack : e) }];
    process.stdout.write(JSON.stringify({
      success: false,
      error: String(e && e.message ? e.message : e),
      fatal: true,
      trace,
    }));
  });
