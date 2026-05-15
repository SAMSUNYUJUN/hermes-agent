#!/usr/bin/env node

const startedAt = Date.now();

function jsonOut(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function pushTrace(trace, event, data = {}) {
  trace.push({ t: Date.now() - startedAt, event, ...data });
}

function parseArg() {
  if (!process.argv[2]) throw new Error("Missing JSON argument in process.argv[2]");
  const arg = JSON.parse(process.argv[2]);
  if (!arg || typeof arg !== "object") throw new Error("Argument must be a JSON object");
  if (typeof arg.query !== "string" || !arg.query.trim()) throw new Error("Missing required string key: query");
  return {
    site_url: typeof arg.site_url === "string" ? arg.site_url : "https://www.beachcamera.com",
    query: arg.query.trim(),
    expected_terms: Array.isArray(arg.expected_terms) ? arg.expected_terms.map(String).filter(Boolean) : [],
    max_candidates: Math.max(1, Math.min(20, Number.isFinite(Number(arg.max_candidates)) ? Number(arg.max_candidates) : 5)),
  };
}

function timeoutSignal(ms) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(new Error(`Timeout after ${ms}ms`)), ms);
  return { signal: controller.signal, cancel: () => clearTimeout(id) };
}

async function fetchText(url, trace, label, timeoutMs = 12000) {
  const t = timeoutSignal(timeoutMs);
  const started = Date.now();
  try {
    pushTrace(trace, "fetch_start", { label, url });
    const res = await fetch(url, {
      signal: t.signal,
      redirect: "follow",
      headers: {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (compatible; HermesDTCSearch/1.0; +https://www.beachcamera.com)",
      },
    });
    const text = await res.text();
    pushTrace(trace, "fetch_done", { label, url: res.url || url, status: res.status, ms: Date.now() - started, bytes: text.length });
    if (!res.ok) {
      const err = new Error(`HTTP ${res.status} for ${url}`);
      err.status = res.status;
      err.body = text.slice(0, 500);
      throw err;
    }
    return { text, finalUrl: res.url || url, status: res.status, contentType: res.headers.get("content-type") || "" };
  } finally {
    t.cancel();
  }
}

async function fetchJson(url, trace, label, timeoutMs = 9000) {
  const t = timeoutSignal(timeoutMs);
  const started = Date.now();
  try {
    pushTrace(trace, "fetch_start", { label, url });
    const res = await fetch(url, {
      signal: t.signal,
      redirect: "follow",
      headers: {
        "accept": "application/json,text/javascript,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (compatible; HermesDTCSearch/1.0; +https://www.beachcamera.com)",
      },
    });
    const text = await res.text();
    pushTrace(trace, "fetch_done", { label, url: res.url || url, status: res.status, ms: Date.now() - started, bytes: text.length });
    if (!res.ok) {
      const err = new Error(`HTTP ${res.status} for ${url}`);
      err.status = res.status;
      err.body = text.slice(0, 500);
      throw err;
    }
    return { json: JSON.parse(text), finalUrl: res.url || url, status: res.status };
  } finally {
    t.cancel();
  }
}

function decodeEntities(s) {
  if (!s) return "";
  const named = {
    amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'", nbsp: " ",
    reg: "®", copy: "©", trade: "™", ndash: "–", mdash: "—",
  };
  return String(s)
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)))
    .replace(/&([a-zA-Z][a-zA-Z0-9]+);/g, (_, n) => named[n] ?? `&${n};`);
}

function stripTags(html) {
  return decodeEntities(String(html || "")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim());
}

function attrValue(tag, name) {
  const re = new RegExp(`\\b${name}\\s*=\\s*("([^"]*)"|'([^']*)'|([^\\s"'=<>` + "`" + `]+))`, "i");
  const m = tag.match(re);
  return m ? decodeEntities(m[2] ?? m[3] ?? m[4] ?? "") : "";
}

function absoluteUrl(href, base = "https://www.beachcamera.com") {
  try {
    return new URL(decodeEntities(href), base).href;
  } catch {
    return "";
  }
}

function cleanProductUrl(url) {
  try {
    const u = new URL(url, "https://www.beachcamera.com");
    if (!/beachcamera\.com$/i.test(u.hostname)) return "";
    const m = u.pathname.match(/^\/products\/([^/?#]+)/i);
    if (!m) return "";
    u.protocol = "https:";
    u.hostname = "www.beachcamera.com";
    u.pathname = `/products/${m[1]}`;
    u.search = "";
    u.hash = "";
    return u.href;
  } catch {
    return "";
  }
}

function productHandleFromUrl(url) {
  try {
    const m = new URL(url).pathname.match(/^\/products\/([^/?#]+)/i);
    return m ? m[1] : "";
  } catch {
    return "";
  }
}

function normalizeText(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokenSet(s) {
  return new Set(normalizeText(s).split(" ").filter(Boolean));
}

function makeQueries(query, expectedTerms) {
  const out = [];
  const add = (q) => {
    q = String(q || "").replace(/[()[\]{},;:|]+/g, " ").replace(/\s+/g, " ").trim();
    if (q && !out.some(x => normalizeText(x) === normalizeText(q))) out.push(q);
  };
  add(query);
  add(query.replace(/[^\p{L}\p{N}\s.-]+/gu, " "));
  const modelish = [...query.matchAll(/\b[A-Z0-9][A-Z0-9._-]{2,}\b/g)].map(m => m[0]);
  if (modelish.length) add(modelish.join(" "));
  if (expectedTerms.length) add(expectedTerms.join(" "));
  return out.slice(0, 4);
}

function searchUrlFor(q) {
  const params = new URLSearchParams();
  params.set("q", q);
  return `https://www.beachcamera.com/search?${params.toString().replace(/%20/g, "+")}`;
}

function metaContent(html, key) {
  const esc = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re1 = new RegExp(`<meta\\b(?=[^>]*(?:property|name)=["']${esc}["'])[^>]*\\bcontent=["']([^"']*)["'][^>]*>`, "i");
  const re2 = new RegExp(`<meta\\b(?=[^>]*\\bcontent=["']([^"']*)["'])(?=[^>]*(?:property|name)=["']${esc}["'])[^>]*>`, "i");
  const m = html.match(re1) || html.match(re2);
  return m ? decodeEntities(m[1]).trim() : "";
}

function extractProductLinksFromSearch(html) {
  const found = new Map();

  const anchorRe = /<a\b[^>]*\bhref\s*=\s*("([^"]*\/products\/[^"]*)"|'([^']*\/products\/[^']*)'|([^>\s"'=]*\/products\/[^>\s"'=]*))[^>]*>[\s\S]*?<\/a>/gi;
  let m;
  while ((m = anchorRe.exec(html))) {
    const tag = m[0].match(/^<a\b[^>]*>/i)?.[0] || "";
    const href = m[2] ?? m[3] ?? m[4] ?? "";
    const cleanUrl = cleanProductUrl(absoluteUrl(href));
    if (!cleanUrl) continue;

    const inner = m[0].replace(/^<a\b[^>]*>/i, "").replace(/<\/a>$/i, "");
    const textCandidates = [
      attrValue(tag, "title"),
      attrValue(tag, "aria-label"),
      attrValue(inner.match(/<img\b[^>]*>/i)?.[0] || "", "alt"),
      stripTags(inner),
    ].map(x => x.trim()).filter(Boolean);

    const title = textCandidates
      .filter(x => normalizeText(x).length > 2)
      .sort((a, b) => b.length - a.length)[0] || "";

    const idx = m.index;
    const context = html.slice(Math.max(0, idx - 1500), Math.min(html.length, idx + m[0].length + 2500));
    const current = found.get(cleanUrl) || { url: cleanUrl, rawUrl: absoluteUrl(href), titles: [], contexts: [], firstIndex: idx };
    if (title) current.titles.push(title);
    current.contexts.push(context);
    current.firstIndex = Math.min(current.firstIndex, idx);
    found.set(cleanUrl, current);
  }

  const urlRe = /https?:\/\/(?:www\.)?beachcamera\.com\/products\/[A-Za-z0-9][^"'<>\s)]*|\/products\/[A-Za-z0-9][^"'<>\s)]*/gi;
  while ((m = urlRe.exec(html))) {
    const cleanUrl = cleanProductUrl(absoluteUrl(m[0]));
    if (!cleanUrl) continue;
    const idx = m.index;
    const context = html.slice(Math.max(0, idx - 1500), Math.min(html.length, idx + 2500));
    const current = found.get(cleanUrl) || { url: cleanUrl, rawUrl: absoluteUrl(m[0]), titles: [], contexts: [], firstIndex: idx };
    current.contexts.push(context);
    current.firstIndex = Math.min(current.firstIndex, idx);
    found.set(cleanUrl, current);
  }

  return [...found.values()].sort((a, b) => a.firstIndex - b.firstIndex).map((x, i) => {
    const contextText = stripTags(x.contexts.join(" ").slice(0, 12000));
    const bestTitle = [...new Set(x.titles)]
      .filter(Boolean)
      .sort((a, b) => {
        const genericA = /^(view|quick|add|sale|sold out|details)$/i.test(a.trim()) ? 1 : 0;
        const genericB = /^(view|quick|add|sale|sold out|details)$/i.test(b.trim()) ? 1 : 0;
        return genericA - genericB || b.length - a.length;
      })[0] || "";
    return {
      url: x.url,
      raw_url: x.rawUrl,
      search_rank: i + 1,
      search_title: bestTitle,
      search_context_text: contextText.slice(0, 1200),
    };
  });
}

function extractJsonLdProducts(html) {
  const products = [];
  const re = /<script\b[^>]*type\s*=\s*["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  const visit = (node) => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) return node.forEach(visit);
    const type = node["@type"];
    const types = Array.isArray(type) ? type.map(String) : [String(type || "")];
    if (types.some(t => /product/i.test(t))) products.push(node);
    if (node["@graph"]) visit(node["@graph"]);
    if (node.itemListElement) visit(node.itemListElement);
    if (node.item) visit(node.item);
  };
  while ((m = re.exec(html))) {
    try {
      visit(JSON.parse(decodeEntities(m[1]).trim()));
    } catch {}
  }
  return products;
}

function inferAvailability(text, explicit) {
  const s = normalizeText(`${explicit || ""} ${text || ""}`);
  if (/\bsold out\b|\bout of stock\b|\bunavailable\b/.test(s)) return "sold_out";
  if (/\bin stock\b|\bavailable\b/.test(s)) return "available";
  return "";
}

function scoreCandidate(c, query, expectedTerms) {
  const hay = normalizeText([
    c.title, c.brand, c.sku, c.url, c.search_title, c.handle,
  ].filter(Boolean).join(" "));
  const queryTokens = [...tokenSet(query)];
  const expectedTokens = [...tokenSet(expectedTerms.join(" "))];
  let score = 0;
  if (c.title && normalizeText(c.title) === normalizeText(query)) score += 100;
  if (c.search_title && normalizeText(c.search_title) === normalizeText(query)) score += 70;
  for (const tok of queryTokens) {
    if (hay.includes(tok)) score += tok.length >= 4 ? 4 : 2;
  }
  for (const tok of expectedTokens) {
    if (hay.includes(tok)) score += tok.length >= 4 ? 6 : 3;
  }
  const modelTokens = [...new Set(`${query} ${expectedTerms.join(" ")}`.match(/\b[A-Z0-9][A-Z0-9._-]{2,}\b/g) || [])];
  for (const mt of modelTokens) {
    if (hay.includes(normalizeText(mt))) score += 20;
  }
  score += Math.max(0, 12 - Number(c.search_rank || 99));
  return score;
}

function cleanTitle(t) {
  return String(t || "")
    .replace(/\s*[|–-]\s*Beach Camera\s*$/i, "")
    .replace(/\s+/g, " ")
    .trim();
}

async function enrichCandidate(base, trace) {
  const handle = productHandleFromUrl(base.url);
  const out = {
    title: cleanTitle(base.search_title),
    url: base.url,
    brand: "",
    sku: "",
    availability: "",
    image: "",
    handle,
    source: "site_search",
    search_rank: base.search_rank,
    search_title: cleanTitle(base.search_title),
    evidence: {
      search_result_url: base.raw_url,
      search_rank: base.search_rank,
      search_context: base.search_context_text,
    },
  };

  if (handle) {
    try {
      const jsUrl = `https://www.beachcamera.com/products/${encodeURIComponent(handle)}.js`;
      const { json } = await fetchJson(jsUrl, trace, "shopify_product_json", 8500);
      if (json && typeof json === "object") {
        out.title = cleanTitle(json.title || out.title);
        out.brand = String(json.vendor || "").trim();
        out.image = absoluteUrl(json.featured_image || json.images?.[0] || "");
        out.availability = json.available === true ? "available" : json.available === false ? "sold_out" : out.availability;
        const variants = Array.isArray(json.variants) ? json.variants : [];
        const sku = variants.map(v => v && v.sku).find(Boolean);
        if (sku) out.sku = String(sku).trim();
        out.evidence.shopify_product_json = {
          id: json.id,
          handle: json.handle,
          vendor: json.vendor,
          available: json.available,
          variant_count: variants.length,
        };
        return out;
      }
    } catch (e) {
      pushTrace(trace, "product_json_unavailable", { url: base.url, error: String(e.message || e).slice(0, 240) });
    }
  }

  try {
    const { text, finalUrl } = await fetchText(base.url, trace, "product_page", 10000);
    const ldProducts = extractJsonLdProducts(text);
    const ld = ldProducts[0] || {};
    out.title = cleanTitle(ld.name || metaContent(text, "og:title") || text.match(/<h1\b[^>]*>([\s\S]*?)<\/h1>/i)?.[1] && stripTags(text.match(/<h1\b[^>]*>([\s\S]*?)<\/h1>/i)[1]) || out.title);
    out.brand = String((typeof ld.brand === "object" ? ld.brand.name : ld.brand) || metaContent(text, "product:brand") || out.brand || "").trim();
    out.sku = String(ld.sku || text.match(/\bSKU\b\s*[:#]?\s*<\/?[^>]*>\s*([A-Z0-9._-]{3,})/i)?.[1] || text.match(/"sku"\s*:\s*"([^"]+)"/i)?.[1] || out.sku || "").trim();
    out.image = absoluteUrl((Array.isArray(ld.image) ? ld.image[0] : ld.image) || metaContent(text, "og:image") || out.image || "");
    out.availability = inferAvailability(text.slice(0, 80000), ld.offers?.availability || out.availability);
    out.evidence.product_page_url = finalUrl;
    out.evidence.json_ld_product_count = ldProducts.length;
  } catch (e) {
    pushTrace(trace, "product_page_unavailable", { url: base.url, error: String(e.message || e).slice(0, 240) });
  }

  return out;
}

async function main() {
  const trace = [];
  try {
    const arg = parseArg();
    const evidence = {
      site: "https://www.beachcamera.com",
      input_site_url: arg.site_url,
      strategy: "direct Beach Camera www search URL; extract /products/ links from search DOM; verify extracted handles via Shopify product JSON or product page",
      searched_urls: [],
    };

    pushTrace(trace, "start", {
      site_url: arg.site_url,
      normalized_site: "https://www.beachcamera.com",
      query: arg.query,
      expected_terms_count: arg.expected_terms.length,
      max_candidates: arg.max_candidates,
    });

    const queries = makeQueries(arg.query, arg.expected_terms);
    evidence.queries = queries;

    const allLinks = new Map();
    let searchFailures = 0;

    for (const q of queries) {
      const url = searchUrlFor(q);
      evidence.searched_urls.push(url);
      try {
        const { text, finalUrl } = await fetchText(url, trace, "search_results", 12000);
        const links = extractProductLinksFromSearch(text);
        pushTrace(trace, "search_parsed", { query: q, finalUrl, product_links: links.length });
        for (const link of links) {
          const prev = allLinks.get(link.url);
          if (!prev) {
            link.matched_query = q;
            allLinks.set(link.url, link);
          } else {
            prev.search_context_text = `${prev.search_context_text} ${link.search_context_text}`.slice(0, 1800);
            if (!prev.search_title && link.search_title) prev.search_title = link.search_title;
          }
        }
      } catch (e) {
        searchFailures++;
        pushTrace(trace, "search_failed", { query: q, url, error: String(e.message || e).slice(0, 300) });
      }
      if (allLinks.size >= arg.max_candidates * 4) break;
    }

    if (!allLinks.size && searchFailures === queries.length) {
      throw new Error("All Beach Camera search requests failed");
    }

    const rawLinks = [...allLinks.values()].slice(0, Math.max(arg.max_candidates * 3, arg.max_candidates));
    const enriched = [];
    for (const link of rawLinks) {
      const c = await enrichCandidate(link, trace);
      c.score = scoreCandidate(c, arg.query, arg.expected_terms);
      enriched.push(c);
    }

    const candidates = enriched
      .sort((a, b) => b.score - a.score || a.search_rank - b.search_rank)
      .slice(0, arg.max_candidates)
      .map(c => {
        const obj = {
          title: c.title || c.search_title || "",
          url: c.url,
          brand: c.brand || "",
          sku: c.sku || "",
          availability: c.availability || "",
          image: c.image || "",
          score: c.score,
          source: c.source,
          search_rank: c.search_rank,
          evidence: c.evidence,
        };
        Object.keys(obj).forEach(k => {
          if (obj[k] === "" || obj[k] == null) delete obj[k];
        });
        return obj;
      });

    evidence.product_links_found = allLinks.size;
    evidence.candidates_returned = candidates.length;
    pushTrace(trace, "complete", { candidates: candidates.length });

    jsonOut({ success: true, candidates, evidence, trace });
  } catch (e) {
    pushTrace(trace, "fatal", { error: String(e && e.message ? e.message : e).slice(0, 500) });
    jsonOut({ success: false, error: String(e && e.message ? e.message : e), fatal: true, trace });
  }
}

await main();
