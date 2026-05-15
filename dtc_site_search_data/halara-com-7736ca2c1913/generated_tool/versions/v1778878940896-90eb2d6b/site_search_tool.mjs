const DEFAULT_TIMEOUT_MS = 8000;
const USER_AGENT =
  "Mozilla/5.0 (compatible; HermesDTCSearch/1.0; +https://www.halara.com/)";

function nowIso() {
  return new Date().toISOString();
}

function pushTrace(trace, step, data = {}) {
  trace.push({ t: nowIso(), step, ...data });
}

function safeJsonParse(s) {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

function decodeEntities(s) {
  if (!s) return "";
  return String(s)
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&nbsp;/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&#x2F;/g, "/")
    .replace(/&#(\d+);/g, (_, n) => {
      const code = Number(n);
      return Number.isFinite(code) ? String.fromCharCode(code) : _;
    });
}

function stripTags(html) {
  return decodeEntities(String(html || "").replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " "))
    .replace(/\s+/g, " ")
    .trim();
}

function cleanQuery(q) {
  return String(q || "")
    .replace(/#[\p{L}\p{N}_-]+/gu, " ")
    .replace(/\bHalara\b/gi, " ")
    .replace(/\bTikTok\b|\bAmazon\b|\bSHEIN\b|\bTemu\b/gi, " ")
    .replace(/\bSwimWeek\b/gi, " ")
    .replace(/[™®©]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeForMatch(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/[™®©]/g, "")
    .replace(/&/g, " and ")
    .replace(/(\d)\s*['’′]{2}/g, "$1 inch ")
    .replace(/(\d)\s*["”]/g, "$1 inch ")
    .replace(/2\s*-\s*in\s*-\s*1/g, "2 in 1")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const STOPWORDS = new Set([
  "a",
  "an",
  "and",
  "are",
  "as",
  "at",
  "by",
  "for",
  "from",
  "in",
  "is",
  "it",
  "of",
  "on",
  "or",
  "set",
  "the",
  "to",
  "with",
  "women",
  "womens",
  "woman",
  "halara",
  "piece",
  "pieces",
]);

function tokenSet(parts) {
  const tokens = [];
  for (const part of parts.filter(Boolean)) {
    const norm = normalizeForMatch(part);
    for (const tok of norm.split(" ")) {
      if (tok.length >= 2 && !STOPWORDS.has(tok)) tokens.push(tok);
    }
  }
  return [...new Set(tokens)];
}

function phrasesFrom(parts) {
  const out = [];
  for (const part of parts.filter(Boolean)) {
    const norm = normalizeForMatch(part);
    if (norm.length > 2) out.push(norm);
  }
  return [...new Set(out)];
}

function baseOrigin(siteUrl) {
  try {
    const u = new URL(siteUrl || "https://www.halara.com");
    return `${u.protocol}//${u.hostname.replace(/^halara\.com$/i, "www.halara.com")}`;
  } catch {
    return "https://www.halara.com";
  }
}

function absoluteUrl(href, origin) {
  try {
    return new URL(decodeEntities(href), origin).toString();
  } catch {
    return null;
  }
}

function normalizeProductUrl(url, origin) {
  const abs = absoluteUrl(url, origin);
  if (!abs) return null;
  let u;
  try {
    u = new URL(abs);
  } catch {
    return null;
  }
  if (!/halara\.com$/i.test(u.hostname) && !/\.halara\.com$/i.test(u.hostname)) return null;
  if (!/^\/products\//i.test(u.pathname)) return null;
  u.hostname = "www.halara.com";
  u.hash = "";
  const currentSkc = u.searchParams.get("currentSkc");
  u.search = "";
  if (currentSkc) u.searchParams.set("currentSkc", currentSkc);
  return u.toString();
}

function productSlug(url) {
  try {
    const u = new URL(url);
    return decodeURIComponent(u.pathname.split("/products/")[1] || "").replace(/\/+$/, "");
  } catch {
    return "";
  }
}

async function fetchText(url, trace, label, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const started = Date.now();
  const res = await fetch(url, {
    method: "GET",
    redirect: "follow",
    signal: AbortSignal.timeout(timeoutMs),
    headers: {
      "user-agent": USER_AGENT,
      accept: "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
      "accept-language": "en-US,en;q=0.9",
    },
  });
  const text = await res.text();
  pushTrace(trace, "fetch", {
    label,
    url,
    status: res.status,
    bytes: text.length,
    ms: Date.now() - started,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return text;
}

async function fetchJsonMaybe(url, trace, label, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const started = Date.now();
  const res = await fetch(url, {
    method: "GET",
    redirect: "follow",
    signal: AbortSignal.timeout(timeoutMs),
    headers: {
      "user-agent": USER_AGENT,
      accept: "application/json,text/plain,*/*",
      "accept-language": "en-US,en;q=0.9",
    },
  });
  const text = await res.text();
  pushTrace(trace, "fetch", {
    label,
    url,
    status: res.status,
    bytes: text.length,
    ms: Date.now() - started,
  });
  if (!res.ok) return null;
  return safeJsonParse(text);
}

function addCandidate(map, rawUrl, origin, source, title = "", extra = {}) {
  const url = normalizeProductUrl(rawUrl, origin);
  if (!url) return;
  const key = url;
  const existing = map.get(key) || {
    url,
    title: "",
    sources: [],
    raw: {},
  };
  if (title && (!existing.title || title.length > existing.title.length)) {
    existing.title = stripTags(title);
  }
  if (!existing.sources.includes(source)) existing.sources.push(source);
  Object.assign(existing.raw, extra);
  map.set(key, existing);
}

function extractProductsFromHtml(html, origin, source, map) {
  const anchorRe = /<a\b([^>]*?)href\s*=\s*["']([^"']*\/products\/[^"']+)["']([^>]*)>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = anchorRe.exec(html))) {
    const attrs = `${m[1] || ""} ${m[3] || ""}`;
    const body = m[4] || "";
    const aria = (attrs.match(/\baria-label\s*=\s*["']([^"']+)["']/i) || [])[1] || "";
    const titleAttr = (attrs.match(/\btitle\s*=\s*["']([^"']+)["']/i) || [])[1] || "";
    const title = titleAttr || aria || stripTags(body);
    addCandidate(map, m[2], origin, source, title);
  }

  const hrefRe = /["']([^"']*\/products\/[^"'\s<>]+)["']/gi;
  while ((m = hrefRe.exec(html))) addCandidate(map, m[1], origin, source, "");
}

function walkJsonForProducts(node, origin, source, map) {
  if (node == null) return;
  if (typeof node === "string") {
    if (node.includes("/products/")) addCandidate(map, node, origin, source, "");
    return;
  }
  if (Array.isArray(node)) {
    for (const item of node) walkJsonForProducts(item, origin, source, map);
    return;
  }
  if (typeof node !== "object") return;

  const title =
    node.title ||
    node.name ||
    node.productTitle ||
    node.product_name ||
    node.productName ||
    "";
  const possible =
    node.url ||
    node.href ||
    node.link ||
    node.productUrl ||
    node.product_url ||
    node.path ||
    node.handle ||
    node.slug;
  if (typeof possible === "string") {
    let href = possible;
    if (!href.includes("/products/") && /^[A-Za-z0-9][A-Za-z0-9-_]+$/.test(href)) {
      href = `/products/${href}`;
    }
    if (href.includes("/products/")) addCandidate(map, href, origin, source, title, node);
  }

  for (const v of Object.values(node)) walkJsonForProducts(v, origin, source, map);
}

function extractEmbeddedJsonProducts(html, origin, source, map) {
  const nextData = html.match(/<script[^>]+id=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)<\/script>/i);
  if (nextData) {
    const json = safeJsonParse(decodeEntities(nextData[1]));
    if (json) walkJsonForProducts(json, origin, `${source}:__NEXT_DATA__`, map);
  }

  const scriptRe = /<script[^>]*type=["']application\/json["'][^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = scriptRe.exec(html))) {
    const json = safeJsonParse(decodeEntities(m[1]));
    if (json) walkJsonForProducts(json, origin, `${source}:json-script`, map);
  }
}

function extractSitemapProducts(xml, origin, source, map) {
  const locRe = /<loc>\s*([^<]*\/products\/[^<]+)\s*<\/loc>/gi;
  let m;
  while ((m = locRe.exec(xml))) addCandidate(map, m[1], origin, source, "");
}

function collectionPathsForQuery(query) {
  const q = normalizeForMatch(query);
  const paths = new Set();

  if (/\bshorts?\b|skorts?\b/.test(q)) paths.add("/collections/shorts-1");
  if (/\bleggings?\b|tights?\b/.test(q)) {
    paths.add("/collections/leggings");
    paths.add("/collections/workout-leggings");
    paths.add("/collections/pants");
  }
  if (/\bdresses?\b/.test(q)) paths.add("/collections/dresses");
  if (/\bskirts?\b|skorts?\b/.test(q)) paths.add("/collections/skirts");
  if (/\bpants?\b|trousers?\b|joggers?\b/.test(q)) {
    paths.add("/collections/pants");
    paths.add("/collections/joggers");
  }
  if (/\btops?\b|bras?\b|shirts?\b|tank\b/.test(q)) {
    paths.add("/collections/tops");
    paths.add("/collections/sports-bras");
  }

  paths.add("/collections/best-sellers");
  paths.add("/collections/new-arrivals");
  return [...paths].slice(0, 8);
}

function scoreCandidate(c, queryParts, terms) {
  const hayTitle = normalizeForMatch(c.title || "");
  const haySlug = normalizeForMatch(productSlug(c.url).replace(/-/g, " "));
  const hay = `${hayTitle} ${haySlug}`;
  let score = 0;
  const matched = [];

  for (const tok of terms) {
    const re = new RegExp(`\\b${tok.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i");
    if (re.test(hay)) {
      matched.push(tok);
      score += hayTitle.match(re) ? 3 : 2;
    }
  }

  for (const phrase of phrasesFrom(queryParts)) {
    if (phrase.length >= 8 && hay.includes(phrase)) score += 12;
    else {
      const phraseTokens = phrase.split(" ").filter((t) => !STOPWORDS.has(t));
      if (phraseTokens.length >= 2) {
        const hitCount = phraseTokens.filter((t) => matched.includes(t)).length;
        if (hitCount >= Math.ceil(phraseTokens.length * 0.7)) score += 4;
      }
    }
  }

  if (/\/products\//.test(c.url)) score += 1;
  if (c.url.includes("currentSkc=")) score += 1;
  return { score, matched_terms: [...new Set(matched)] };
}

function extractJsonLd(html) {
  const out = [];
  const re = /<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    const txt = decodeEntities(m[1]).trim();
    const json = safeJsonParse(txt);
    if (json) out.push(json);
  }
  return out;
}

function findProductJsonLd(jsonLd) {
  const products = [];
  const visit = (n) => {
    if (!n) return;
    if (Array.isArray(n)) {
      n.forEach(visit);
      return;
    }
    if (typeof n !== "object") return;
    const type = n["@type"];
    const types = Array.isArray(type) ? type : [type];
    if (types.some((t) => String(t).toLowerCase() === "product")) products.push(n);
    if (n["@graph"]) visit(n["@graph"]);
  };
  jsonLd.forEach(visit);
  return products[0] || null;
}

function metaContent(html, prop) {
  const esc = prop.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re1 = new RegExp(`<meta[^>]+(?:property|name)=["']${esc}["'][^>]+content=["']([^"']*)["'][^>]*>`, "i");
  const re2 = new RegExp(`<meta[^>]+content=["']([^"']*)["'][^>]+(?:property|name)=["']${esc}["'][^>]*>`, "i");
  return decodeEntities((html.match(re1) || html.match(re2) || [])[1] || "");
}

function extractProductDetails(html, url) {
  const jsonLd = extractJsonLd(html);
  const productLd = findProductJsonLd(jsonLd);

  let title =
    (productLd && (productLd.name || productLd.headline)) ||
    metaContent(html, "og:title") ||
    metaContent(html, "twitter:title") ||
    stripTags((html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i) || [])[1] || "") ||
    stripTags((html.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [])[1] || "");

  title = title.replace(/\s*\|\s*Halara.*$/i, "").trim();

  let price = "";
  let availability = "";
  if (productLd && productLd.offers) {
    const offer = Array.isArray(productLd.offers) ? productLd.offers[0] : productLd.offers;
    if (offer) {
      price = offer.price || offer.lowPrice || "";
      availability = offer.availability || "";
    }
  }
  price = price || metaContent(html, "product:price:amount");

  const colorCandidates = [];
  const colorPatterns = [
    /"color(?:Name)?"\s*:\s*"([^"]{2,60})"/gi,
    /"colour(?:Name)?"\s*:\s*"([^"]{2,60})"/gi,
    /"selectedColor"\s*:\s*"([^"]{2,60})"/gi,
    /"currentColor"\s*:\s*"([^"]{2,60})"/gi,
  ];
  for (const re of colorPatterns) {
    let m;
    while ((m = re.exec(html)) && colorCandidates.length < 10) {
      const c = decodeEntities(m[1]).trim();
      if (c && !colorCandidates.includes(c)) colorCandidates.push(c);
    }
  }

  const currentSkc = (() => {
    try {
      return new URL(url).searchParams.get("currentSkc") || "";
    } catch {
      return "";
    }
  })();

  return {
    title,
    price: price ? String(price) : "",
    availability: availability ? String(availability).split("/").pop() : "",
    currentSkc,
    colors_seen: colorCandidates,
    jsonld_product: Boolean(productLd),
  };
}

async function main() {
  const trace = [];
  try {
    const input = safeJsonParse(process.argv[2] || "");
    if (!input || typeof input !== "object") {
      throw new Error("process.argv[2] must be a JSON object");
    }

    const origin = baseOrigin(input.site_url || "https://www.halara.com");
    const query = cleanQuery(input.query || "");
    const expectedTerms = Array.isArray(input.expected_terms)
      ? input.expected_terms.map(String).filter(Boolean)
      : [];
    const maxCandidates = Math.max(0, Math.min(Number(input.max_candidates) || 5, 20));
    const queryParts = [query, ...expectedTerms];
    const terms = tokenSet(queryParts);
    const found = new Map();

    pushTrace(trace, "start", {
      site: origin,
      query,
      expected_terms_count: expectedTerms.length,
      max_candidates: maxCandidates,
    });

    const collectionPaths = collectionPathsForQuery(query);
    for (const path of collectionPaths) {
      const url = `${origin}${path}`;
      try {
        const html = await fetchText(url, trace, `collection:${path}`, DEFAULT_TIMEOUT_MS);
        extractProductsFromHtml(html, origin, `collection:${path}`, found);
        extractEmbeddedJsonProducts(html, origin, `collection:${path}`, found);
      } catch (e) {
        pushTrace(trace, "nonfatal_fetch_error", {
          label: `collection:${path}`,
          url,
          error: String(e && e.message ? e.message : e),
        });
      }
      if (found.size >= Math.max(maxCandidates * 8, 40)) break;
    }

    const encodedQ = encodeURIComponent(query);
    const searchApiUrls = [
      `${origin}/search/suggest.json?q=${encodedQ}&resources[type]=product&resources[limit]=20`,
      `${origin}/api/search?keyword=${encodedQ}`,
      `${origin}/api/search?q=${encodedQ}`,
      `${origin}/api/product/search?keyword=${encodedQ}`,
    ];
    for (const url of searchApiUrls) {
      try {
        const json = await fetchJsonMaybe(url, trace, "search-api", 6000);
        if (json) walkJsonForProducts(json, origin, "search-api", found);
      } catch (e) {
        pushTrace(trace, "nonfatal_fetch_error", {
          label: "search-api",
          url,
          error: String(e && e.message ? e.message : e),
        });
      }
    }

    if (found.size < Math.max(maxCandidates, 8)) {
      const sitemapUrls = [`${origin}/sitemap.xml`, `${origin}/sitemap_products_1.xml`];
      for (const url of sitemapUrls) {
        try {
          const xml = await fetchText(url, trace, "sitemap", 7000);
          extractSitemapProducts(xml, origin, "sitemap", found);

          const nested = [...xml.matchAll(/<loc>\s*([^<]*sitemap[^<]*\.xml[^<]*)\s*<\/loc>/gi)]
            .map((m) => decodeEntities(m[1]))
            .filter((u) => /product|products/i.test(u))
            .slice(0, 3);
          for (const nestedUrl of nested) {
            try {
              const nestedXml = await fetchText(nestedUrl, trace, "sitemap:nested", 7000);
              extractSitemapProducts(nestedXml, origin, "sitemap:nested", found);
            } catch (e) {
              pushTrace(trace, "nonfatal_fetch_error", {
                label: "sitemap:nested",
                url: nestedUrl,
                error: String(e && e.message ? e.message : e),
              });
            }
          }
        } catch (e) {
          pushTrace(trace, "nonfatal_fetch_error", {
            label: "sitemap",
            url,
            error: String(e && e.message ? e.message : e),
          });
        }
      }
    }

    const scored = [...found.values()]
      .map((c) => ({ ...c, ...scoreCandidate(c, queryParts, terms) }))
      .filter((c) => c.score > 0 || found.size <= maxCandidates)
      .sort((a, b) => b.score - a.score || b.matched_terms.length - a.matched_terms.length)
      .slice(0, Math.max(maxCandidates * 2, maxCandidates));

    pushTrace(trace, "scored_candidates", {
      discovered: found.size,
      scored: scored.length,
    });

    const detailed = [];
    for (const c of scored.slice(0, Math.max(maxCandidates, 1))) {
      try {
        const html = await fetchText(c.url, trace, "product-detail", DEFAULT_TIMEOUT_MS);
        const details = extractProductDetails(html, c.url);
        const mergedTitle = details.title || c.title || productSlug(c.url).replace(/-/g, " ");
        const rescored = scoreCandidate({ ...c, title: mergedTitle }, queryParts, terms);
        detailed.push({
          url: c.url,
          title: mergedTitle,
          score: rescored.score + Math.floor(c.score / 3),
          matched_terms: [...new Set([...(c.matched_terms || []), ...rescored.matched_terms])],
          price: details.price,
          availability: details.availability,
          currentSkc: details.currentSkc,
          colors_seen: details.colors_seen,
          sources: c.sources,
          evidence: {
            slug: productSlug(c.url),
            jsonld_product: details.jsonld_product,
          },
        });
      } catch (e) {
        pushTrace(trace, "nonfatal_fetch_error", {
          label: "product-detail",
          url: c.url,
          error: String(e && e.message ? e.message : e),
        });
        detailed.push({
          url: c.url,
          title: c.title || productSlug(c.url).replace(/-/g, " "),
          score: c.score,
          matched_terms: c.matched_terms || [],
          sources: c.sources,
          evidence: { slug: productSlug(c.url), detail_fetch_failed: true },
        });
      }
    }

    const candidates = detailed
      .sort((a, b) => b.score - a.score || b.matched_terms.length - a.matched_terms.length)
      .slice(0, maxCandidates);

    const evidence = {
      strategy:
        "Halara skill strategy: avoid direct /search?q as primary route; use relevant collection DOM extraction, lightweight search/suggest APIs, sitemap fallback, then product-page structured data.",
      cleaned_query: query,
      expected_terms: expectedTerms,
      collection_paths_checked: collectionPaths,
      discovered_product_links: found.size,
      candidates_returned: candidates.length,
    };

    process.stdout.write(
      JSON.stringify({
        success: true,
        candidates,
        evidence,
        trace,
      })
    );
  } catch (e) {
    pushTrace(trace, "fatal_error", {
      error: String(e && e.message ? e.message : e),
    });
    process.stdout.write(
      JSON.stringify({
        success: false,
        error: String(e && e.message ? e.message : e),
        fatal: true,
        trace,
      })
    );
  }
}

await main();
