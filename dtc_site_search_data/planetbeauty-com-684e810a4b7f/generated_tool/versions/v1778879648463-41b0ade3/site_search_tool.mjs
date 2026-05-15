const DEFAULT_TIMEOUT_MS = 8000;
const MAX_QUERIES = 8;
const MAX_DETAIL_FETCHES = 24;

function output(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function asArray(v) {
  if (Array.isArray(v)) return v.filter(x => x != null).map(String);
  if (v == null) return [];
  return [String(v)];
}

function normalizeText(s) {
  return String(s || "")
    .replace(/&amp;/gi, "&")
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/<[^>]*>/g, " ")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9.%/+\-\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function unique(arr) {
  const seen = new Set();
  const out = [];
  for (const x of arr) {
    const k = normalizeText(x);
    if (!k || seen.has(k)) continue;
    seen.add(k);
    out.push(x);
  }
  return out;
}

function tokenSet(...parts) {
  const stop = new Set(["the", "and", "or", "for", "with", "a", "an", "of", "to", "in", "by", "on", "at"]);
  const toks = [];
  for (const p of parts) {
    for (const t of normalizeText(p).split(/\s+/)) {
      if (t.length >= 2 && !stop.has(t)) toks.push(t);
    }
  }
  return unique(toks);
}

function stripSizeTerms(q) {
  return String(q || "")
    .replace(/\b\d+(\.\d+)?\s*(fl\.?\s*)?(oz|ounce|ounces|ml|milliliter|milliliters|g|gram|grams|lb|lbs|kg)\b/gi, " ")
    .replace(/\b\d+(\.\d+)?\s*(\/|-)\s*\d+(\.\d+)?\s*(fl\.?\s*)?(oz|ml|g)\b/gi, " ")
    .replace(/\b\d+(\.\d+)?\s*(ct|count|pack|pc|pcs)\b/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function buildQueries(query, expectedTerms) {
  const q = String(query || "").trim();
  const stripped = stripSizeTerms(q);
  const toks = tokenSet(q);
  const expected = asArray(expectedTerms).join(" ").trim();
  const expectedStripped = stripSizeTerms(expected);

  const candidates = [
    q,
    stripped,
    expected,
    expectedStripped,
    toks.slice(0, 4).join(" "),
    toks.slice(0, 3).join(" "),
    toks.slice(0, 2).join(" "),
    toks.slice(0, 1).join(" ")
  ];

  return unique(candidates)
    .filter(x => normalizeText(x).length >= 2)
    .slice(0, MAX_QUERIES);
}

async function fetchWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal,
      redirect: "follow",
      headers: {
        "accept": "application/json,text/html,application/xhtml+xml,*/*;q=0.8",
        "user-agent": "Mozilla/5.0 site-search-tool/1.0",
        ...(options.headers || {})
      }
    });
    const text = await res.text();
    return { ok: res.ok, status: res.status, url: res.url, text, contentType: res.headers.get("content-type") || "" };
  } finally {
    clearTimeout(timer);
  }
}

function htmlDecode(s) {
  return String(s || "")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

function absolutize(url, base = "https://www.planetbeauty.com") {
  try {
    return new URL(htmlDecode(url), base).toString();
  } catch {
    return null;
  }
}

function canonicalProductUrl(url) {
  const abs = absolutize(url);
  if (!abs) return null;
  const u = new URL(abs);
  if (!/planetbeauty\.com$/i.test(u.hostname)) return null;
  const m = u.pathname.match(/^\/products\/([^/?#]+)/i);
  if (!m) return null;
  u.hostname = "www.planetbeauty.com";
  u.protocol = "https:";
  u.pathname = `/products/${m[1]}`;
  u.search = "";
  u.hash = "";
  return u.toString();
}

function handleFromUrl(url) {
  const m = String(url || "").match(/\/products\/([^/?#]+)/i);
  return m ? m[1] : "";
}

function moneyFromCents(cents) {
  if (cents == null || cents === "") return undefined;
  const n = Number(cents);
  if (!Number.isFinite(n)) return undefined;
  return `$${(n / 100).toFixed(2)}`;
}

function addCandidate(map, item, source) {
  const rawUrl = item.url || item.href || item.online_store_url || "";
  const baseUrl = canonicalProductUrl(rawUrl);
  if (!baseUrl) return;

  const handle = handleFromUrl(baseUrl);
  const existing = map.get(baseUrl) || {
    url: baseUrl,
    base_url: baseUrl,
    handle,
    title: "",
    brand: "",
    price: undefined,
    available: undefined,
    variant_id: undefined,
    image: undefined,
    sources: []
  };

  const title = htmlDecode(item.title || item.name || item.product_title || "");
  const brand = htmlDecode(item.vendor || item.brand || "");
  if (title && (!existing.title || title.length > existing.title.length)) existing.title = title;
  if (brand && !existing.brand) existing.brand = brand;
  if (item.price != null && existing.price == null) {
    existing.price = typeof item.price === "number" ? moneyFromCents(item.price) : String(item.price);
  }
  if (item.available != null && existing.available == null) existing.available = Boolean(item.available);
  if (item.featured_image && !existing.image) existing.image = item.featured_image.url || item.featured_image;
  if (item.image && !existing.image) existing.image = typeof item.image === "string" ? item.image : item.image.url;
  if (source && !existing.sources.includes(source)) existing.sources.push(source);

  map.set(baseUrl, existing);
}

function extractProductsFromPredictive(json, map, query) {
  const products =
    json?.resources?.results?.products ||
    json?.results?.products ||
    json?.products ||
    [];
  if (Array.isArray(products)) {
    for (const p of products) addCandidate(map, p, `predictive:${query}`);
  }
  return Array.isArray(products) ? products.length : 0;
}

function extractProductsFromHtml(html, map, query) {
  let count = 0;
  const anchorRe = /<a\b[^>]*href\s*=\s*["']([^"']*\/products\/[^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = anchorRe.exec(html))) {
    const href = htmlDecode(m[1]);
    const inner = m[2] || "";
    const titleAttr = (m[0].match(/\btitle\s*=\s*["']([^"']+)["']/i) || [])[1];
    const aria = (m[0].match(/\baria-label\s*=\s*["']([^"']+)["']/i) || [])[1];
    const text = htmlDecode(inner.replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " "));
    addCandidate(map, { url: href, title: titleAttr || aria || text.trim() }, `html-search:${query}`);
    count++;
  }

  const jsonLdRe = /<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  while ((m = jsonLdRe.exec(html))) {
    try {
      const data = JSON.parse(htmlDecode(m[1]).trim());
      const nodes = Array.isArray(data) ? data : [data];
      for (const node of nodes) {
        const graph = Array.isArray(node?.["@graph"]) ? node["@graph"] : [node];
        for (const n of graph) {
          if (String(n?.["@type"] || "").toLowerCase().includes("product") && n.url) {
            addCandidate(map, { url: n.url, title: n.name, brand: typeof n.brand === "string" ? n.brand : n.brand?.name }, `jsonld-search:${query}`);
            count++;
          }
        }
      }
    } catch {}
  }
  return count;
}

async function fetchProductDetails(candidate, trace) {
  const handle = candidate.handle || handleFromUrl(candidate.url);
  if (!handle) return candidate;

  const jsUrl = `https://www.planetbeauty.com/products/${encodeURIComponent(handle)}.js`;
  try {
    const r = await fetchWithTimeout(jsUrl, {}, DEFAULT_TIMEOUT_MS);
    trace.push({ step: "product_json", url: jsUrl, status: r.status, ok: r.ok });
    if (r.ok) {
      const p = JSON.parse(r.text);
      if (p.title) candidate.title = htmlDecode(p.title);
      if (p.vendor) candidate.brand = htmlDecode(p.vendor);
      if (p.handle) candidate.handle = p.handle;
      if (p.featured_image) candidate.image = absolutize(p.featured_image);
      if (p.description) {
        candidate.description_excerpt = normalizeText(p.description).slice(0, 240);
      }
      if (Array.isArray(p.variants) && p.variants.length) {
        const available = p.variants.find(v => v.available) || p.variants[0];
        candidate.variant_id = available.id != null ? String(available.id) : undefined;
        candidate.available = Boolean(available.available);
        candidate.price = moneyFromCents(available.price) || candidate.price;
        if (available.public_title && available.public_title !== "Default Title") {
          candidate.variant_title = htmlDecode(available.public_title);
        } else if (available.title && available.title !== "Default Title") {
          candidate.variant_title = htmlDecode(available.title);
        }
        if (candidate.variant_id) candidate.url = `${candidate.base_url}?variant=${candidate.variant_id}`;
      }
      return candidate;
    }
  } catch (e) {
    trace.push({ step: "product_json_error", url: jsUrl, error: String(e && e.message ? e.message : e).slice(0, 160) });
  }

  try {
    const r = await fetchWithTimeout(candidate.base_url, {}, DEFAULT_TIMEOUT_MS);
    trace.push({ step: "product_html", url: candidate.base_url, status: r.status, ok: r.ok });
    if (r.ok) {
      const title = (r.text.match(/<meta\s+property=["']og:title["']\s+content=["']([^"']+)["']/i) || [])[1]
        || (r.text.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [])[1];
      const image = (r.text.match(/<meta\s+property=["']og:image["']\s+content=["']([^"']+)["']/i) || [])[1];
      if (title && !candidate.title) candidate.title = htmlDecode(title).replace(/\s*[|-]\s*Planet Beauty\s*$/i, "").trim();
      if (image && !candidate.image) candidate.image = absolutize(image);
    }
  } catch (e) {
    trace.push({ step: "product_html_error", url: candidate.base_url, error: String(e && e.message ? e.message : e).slice(0, 160) });
  }

  return candidate;
}

function scoreCandidate(c, query, expectedTerms) {
  const hay = normalizeText([c.brand, c.title, c.handle, c.variant_title, c.description_excerpt, c.url].filter(Boolean).join(" "));
  const qTokens = tokenSet(query);
  const eTokens = tokenSet(asArray(expectedTerms).join(" "));
  const all = unique([...eTokens, ...qTokens]);
  const matched = all.filter(t => hay.includes(t));
  let score = matched.length;

  if (c.brand && qTokens.length && normalizeText(c.brand).includes(qTokens[0])) score += 2;
  if (String(c.url).includes("/products/")) score += 1;
  if (c.variant_id) score += 0.5;
  if (c.available === true) score += 0.25;

  c.score = Number(score.toFixed(2));
  c.matched_terms = matched;
  return c;
}

async function main() {
  const trace = [];
  let input;
  try {
    input = JSON.parse(process.argv[2] || "");
  } catch (e) {
    output({ success: false, error: `Invalid JSON argument: ${e.message}`, fatal: true, trace });
    return;
  }

  try {
    const siteUrl = String(input.site_url || "https://planetbeauty.com");
    const query = String(input.query || "").trim();
    const expectedTerms = input.expected_terms;
    const maxCandidates = Math.max(0, Math.min(50, Number(input.max_candidates || 10) || 10));

    if (!query) {
      output({ success: true, candidates: [], evidence: { site_url: siteUrl, reason: "empty query" }, trace });
      return;
    }

    const base = "https://www.planetbeauty.com";
    const queries = buildQueries(query, expectedTerms);
    const candidateMap = new Map();
    const evidence = {
      site_url: siteUrl,
      normalized_site_url: base,
      strategy: "Planet Beauty Shopify search: predictive JSON first, then product-only HTML search; product pages under /products/{handle}.",
      queries,
      search_results: []
    };

    for (const q of queries) {
      const suggestUrl = `${base}/search/suggest.json?q=${encodeURIComponent(q)}&resources[type]=product&resources[limit]=10&resources[options][unavailable_products]=last&resources[options][fields]=title,product_type,variants.title,vendor`;
      try {
        const r = await fetchWithTimeout(suggestUrl);
        trace.push({ step: "predictive_search", query: q, status: r.status, ok: r.ok });
        let count = 0;
        if (r.ok) {
          try {
            count = extractProductsFromPredictive(JSON.parse(r.text), candidateMap, q);
          } catch (e) {
            trace.push({ step: "predictive_parse_error", query: q, error: String(e.message || e).slice(0, 160) });
          }
        }
        evidence.search_results.push({ query: q, endpoint: "search/suggest.json", count });
      } catch (e) {
        trace.push({ step: "predictive_search_error", query: q, error: String(e && e.message ? e.message : e).slice(0, 160) });
      }

      if (candidateMap.size >= Math.max(maxCandidates, 8)) continue;

      const htmlUrl = `${base}/search?q=${encodeURIComponent(q)}&type=product&options%5Bprefix%5D=last`;
      try {
        const r = await fetchWithTimeout(htmlUrl);
        trace.push({ step: "html_search", query: q, status: r.status, ok: r.ok });
        let count = 0;
        if (r.ok) count = extractProductsFromHtml(r.text, candidateMap, q);
        evidence.search_results.push({ query: q, endpoint: "search html", count });
      } catch (e) {
        trace.push({ step: "html_search_error", query: q, error: String(e && e.message ? e.message : e).slice(0, 160) });
      }
    }

    let prelim = Array.from(candidateMap.values())
      .map(c => scoreCandidate(c, query, expectedTerms))
      .sort((a, b) => b.score - a.score || a.base_url.localeCompare(b.base_url));

    const detailLimit = Math.min(MAX_DETAIL_FETCHES, Math.max(maxCandidates * 3, maxCandidates, 6), prelim.length);
    for (let i = 0; i < detailLimit; i++) {
      prelim[i] = scoreCandidate(await fetchProductDetails(prelim[i], trace), query, expectedTerms);
    }

    const candidates = prelim
      .map(c => scoreCandidate(c, query, expectedTerms))
      .sort((a, b) => b.score - a.score || a.base_url.localeCompare(b.base_url))
      .slice(0, maxCandidates)
      .map(c => {
        const out = {
          url: c.url,
          base_url: c.base_url,
          title: c.title || undefined,
          brand: c.brand || undefined,
          handle: c.handle || undefined,
          variant_id: c.variant_id || undefined,
          variant_title: c.variant_title || undefined,
          price: c.price,
          available: c.available,
          image: c.image,
          score: c.score,
          matched_terms: c.matched_terms,
          sources: c.sources
        };
        Object.keys(out).forEach(k => out[k] === undefined && delete out[k]);
        return out;
      });

    evidence.candidate_count = candidates.length;
    output({ success: true, candidates, evidence, trace });
  } catch (e) {
    output({ success: false, error: String(e && e.stack ? e.stack : e), fatal: true, trace });
  }
}

main();
