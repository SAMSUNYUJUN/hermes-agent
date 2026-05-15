const DEFAULT_TIMEOUT_MS = 12000;
const PRODUCT_JSON_TIMEOUT_MS = 7000;
const MAX_PRODUCT_JSON_FETCHES = 8;

function safeJsonParse(s) {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

function asArray(v) {
  if (Array.isArray(v)) return v;
  if (typeof v === "string" && v.trim()) return [v];
  return [];
}

function normalizeSpace(s) {
  return String(s || "").replace(/\s+/g, " ").trim();
}

function decodeHtmlEntities(s) {
  const named = {
    amp: "&",
    lt: "<",
    gt: ">",
    quot: '"',
    apos: "'",
    nbsp: " ",
    ndash: "–",
    mdash: "—",
    reg: "®",
    copy: "©",
    trade: "™",
  };
  return String(s || "").replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);/g, (_, ent) => {
    if (ent[0] === "#") {
      const hex = ent[1] && ent[1].toLowerCase() === "x";
      const n = parseInt(ent.slice(hex ? 2 : 1), hex ? 16 : 10);
      return Number.isFinite(n) ? String.fromCodePoint(n) : _;
    }
    return Object.prototype.hasOwnProperty.call(named, ent) ? named[ent] : _;
  });
}

function stripTags(html) {
  return decodeHtmlEntities(
    String(html || "")
      .replace(/<script\b[\s\S]*?<\/script>/gi, " ")
      .replace(/<style\b[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
  ).replace(/\s+/g, " ").trim();
}

function getAttr(tag, name) {
  const re = new RegExp(`\\b${name}\\s*=\\s*("([^"]*)"|'([^']*)'|([^\\s"'=<>` + "`" + `]+))`, "i");
  const m = String(tag || "").match(re);
  return m ? decodeHtmlEntities(m[2] || m[3] || m[4] || "") : "";
}

function absolutizeProductUrl(href, base) {
  try {
    const u = new URL(decodeHtmlEntities(href), base);
    if (!/\/products\//.test(u.pathname)) return "";
    u.protocol = "https:";
    u.hostname = "www.beachcamera.com";
    u.search = "";
    u.hash = "";
    return u.toString();
  } catch {
    return "";
  }
}

function productJsonUrl(productUrl) {
  const u = new URL(productUrl);
  u.search = "";
  u.hash = "";
  if (!u.pathname.endsWith(".js")) u.pathname = u.pathname.replace(/\/$/, "") + ".js";
  return u.toString();
}

async function fetchWithTimeout(url, opts = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(new Error("timeout")), timeoutMs);
  try {
    const res = await fetch(url, {
      redirect: "follow",
      ...opts,
      signal: ac.signal,
      headers: {
        "user-agent":
          "Mozilla/5.0 (compatible; HermesDtcSiteSearch/1.0; +https://www.beachcamera.com)",
        accept: "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        ...(opts.headers || {}),
      },
    });
    const text = await res.text();
    return {
      ok: res.ok,
      status: res.status,
      url: res.url,
      contentType: res.headers.get("content-type") || "",
      text,
    };
  } finally {
    clearTimeout(t);
  }
}

function textNorm(s) {
  return normalizeSpace(String(s || "").toLowerCase().replace(/[^\p{L}\p{N}.]+/gu, " "));
}

function tokenSet(query, expectedTerms) {
  const raw = [query, ...expectedTerms].join(" ");
  const tokens = textNorm(raw)
    .split(/\s+/)
    .filter((t) => t.length >= 2);
  return [...new Set(tokens)];
}

function extractIdentifiers(s) {
  return [...new Set(String(s || "").match(/[A-Za-z]{0,8}\.?[A-Za-z]*[-.]?\d[A-Za-z0-9.\-]{2,}/g) || [])].map((x) =>
    x.toLowerCase()
  );
}

function inferAvailability(snippet) {
  const t = textNorm(stripTags(snippet));
  if (/\bsold out\b|\bout of stock\b|\bunavailable\b|\bprice unavailable\b/.test(t)) return "sold out / unavailable";
  if (/\bin stock\b|\badd to cart\b|\bavailable\b/.test(t)) return "available";
  return "";
}

function inferImage(snippet, base) {
  const m = String(snippet || "").match(/<img\b[^>]*(?:src|data-src|data-original|data-image)=["']([^"']+)["'][^>]*>/i);
  if (!m) return "";
  try {
    return new URL(decodeHtmlEntities(m[1]), base).toString();
  } catch {
    return "";
  }
}

function chooseTitle(anchorTag, innerHtml, snippet, url) {
  const candidates = [];
  const attrTitle = getAttr(anchorTag, "title") || getAttr(anchorTag, "aria-label");
  if (attrTitle) candidates.push(attrTitle);

  const imgAlt = (innerHtml.match(/<img\b[^>]*\balt=["']([^"']+)["'][^>]*>/i) || [])[1];
  if (imgAlt) candidates.push(decodeHtmlEntities(imgAlt));

  const innerText = stripTags(innerHtml);
  if (innerText) candidates.push(innerText);

  const snippetText = stripTags(snippet);
  const slug = decodeURIComponent(new URL(url).pathname.split("/products/")[1] || "")
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  if (slug) candidates.push(slug);

  const cleaned = candidates
    .map((s) =>
      normalizeSpace(
        s
          .replace(/\b(quick view|view product|choose options|sold out|sale|regular price|unit price)\b/gi, " ")
          .replace(/\$\s*\d[\d,.]*/g, " ")
      )
    )
    .filter((s) => s && s.length > 3);

  cleaned.sort((a, b) => {
    const ap = /open box|bundle|\+/.test(a.toLowerCase()) ? 1 : 0;
    const bp = /open box|bundle|\+/.test(b.toLowerCase()) ? 1 : 0;
    return ap - bp || b.length - a.length;
  });
  return cleaned[0] || "";
}

function parseSearchHtml(html, baseUrl, trace) {
  const map = new Map();
  const anchorRe = /<a\b([^>]*)\bhref\s*=\s*("([^"]*\/products\/[^"]*)"|'([^']*\/products\/[^']*)'|([^\s"'<>]*\/products\/[^\s"'<>]*))([^>]*)>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = anchorRe.exec(html))) {
    const href = m[3] || m[4] || m[5] || "";
    const url = absolutizeProductUrl(href, baseUrl);
    if (!url) continue;

    const start = Math.max(0, m.index - 1800);
    const end = Math.min(html.length, anchorRe.lastIndex + 2200);
    const snippet = html.slice(start, end);
    const anchorTag = `<a ${m[1] || ""} ${m[6] || ""}>`;
    const title = chooseTitle(anchorTag, m[7] || "", snippet, url);

    if (!map.has(url)) {
      map.set(url, {
        title,
        url,
        brand: "",
        availability: inferAvailability(snippet),
        image: inferImage(snippet, baseUrl),
        sku: "",
        score: 0,
        evidence: {
          source: "search_html",
          matched_url_in_search_results: true,
          search_result_text: normalizeSpace(stripTags(snippet)).slice(0, 500),
        },
        _snippets: [snippet],
        _order: map.size,
      });
    } else {
      const cur = map.get(url);
      if (!cur.title || (title && title.length > cur.title.length)) cur.title = title;
      if (!cur.availability) cur.availability = inferAvailability(snippet);
      if (!cur.image) cur.image = inferImage(snippet, baseUrl);
      cur._snippets.push(snippet);
    }
  }

  parseJsonLdProducts(html, baseUrl, map);
  trace.push({ step: "parse_search_html", product_links: map.size });
  return [...map.values()];
}

function parseJsonLdProducts(html, baseUrl, map) {
  const scripts = String(html || "").match(/<script\b[^>]*type=["']application\/ld\+json["'][^>]*>[\s\S]*?<\/script>/gi) || [];
  for (const script of scripts) {
    const body = decodeHtmlEntities(script.replace(/^<script\b[^>]*>/i, "").replace(/<\/script>$/i, "").trim());
    const parsed = safeJsonParse(body);
    if (!parsed) continue;
    const stack = [parsed];
    while (stack.length) {
      const node = stack.pop();
      if (!node || typeof node !== "object") continue;
      if (Array.isArray(node)) {
        for (const x of node) stack.push(x);
        continue;
      }
      const type = Array.isArray(node["@type"]) ? node["@type"].join(" ") : String(node["@type"] || "");
      if (/Product/i.test(type) && node.url) {
        const url = absolutizeProductUrl(String(node.url), baseUrl);
        if (url) {
          const cur =
            map.get(url) ||
            {
              title: "",
              url,
              brand: "",
              availability: "",
              image: "",
              sku: "",
              score: 0,
              evidence: { source: "json_ld", matched_url_in_search_results: false },
              _snippets: [],
              _order: map.size,
            };
          if (node.name && !cur.title) cur.title = normalizeSpace(node.name);
          if (node.brand && !cur.brand) {
            cur.brand = normalizeSpace(typeof node.brand === "string" ? node.brand : node.brand.name || "");
          }
          if (node.sku && !cur.sku) cur.sku = normalizeSpace(node.sku);
          if (node.image && !cur.image) cur.image = Array.isArray(node.image) ? String(node.image[0] || "") : String(node.image || "");
          if (!map.has(url)) map.set(url, cur);
        }
      }
      for (const v of Object.values(node)) {
        if (v && typeof v === "object") stack.push(v);
      }
    }
  }
}

function parseSuggestJson(json, baseUrl) {
  const out = [];
  const products =
    json?.resources?.results?.products ||
    json?.resources?.results?.product ||
    json?.results?.products ||
    json?.products ||
    [];
  if (!Array.isArray(products)) return out;

  for (const p of products) {
    const rawUrl = p.url || p.handle || p.path;
    if (!rawUrl) continue;
    let href = String(rawUrl);
    if (!href.includes("/products/") && p.handle) href = `/products/${p.handle}`;
    const url = absolutizeProductUrl(href, baseUrl);
    if (!url) continue;
    out.push({
      title: normalizeSpace(p.title || p.name || ""),
      url,
      brand: normalizeSpace(p.vendor || p.brand || ""),
      availability: typeof p.available === "boolean" ? (p.available ? "available" : "sold out / unavailable") : "",
      image: typeof p.image === "string" ? p.image : p.image?.src || p.featured_image || "",
      sku: normalizeSpace(p.sku || ""),
      score: 0,
      evidence: { source: "search_suggest_json", matched_url_in_search_results: true },
      _snippets: [],
      _order: out.length,
    });
  }
  return out;
}

function mergeCandidates(lists) {
  const map = new Map();
  for (const list of lists) {
    for (const c of list) {
      if (!c.url) continue;
      if (!map.has(c.url)) {
        map.set(c.url, { ...c, evidence: { ...(c.evidence || {}) } });
      } else {
        const cur = map.get(c.url);
        for (const k of ["title", "brand", "availability", "image", "sku"]) {
          if (!cur[k] && c[k]) cur[k] = c[k];
          if (k === "title" && c[k] && c[k].length > String(cur[k] || "").length) cur[k] = c[k];
        }
        cur.evidence.sources = [...new Set([cur.evidence.source, ...(cur.evidence.sources || []), c.evidence?.source].filter(Boolean))];
        cur.evidence.matched_url_in_search_results =
          Boolean(cur.evidence.matched_url_in_search_results) || Boolean(c.evidence?.matched_url_in_search_results);
      }
    }
  }
  return [...map.values()];
}

function scoreCandidate(c, query, expectedTerms) {
  const hay = textNorm([c.title, c.brand, c.sku, c.url].join(" "));
  const qn = textNorm(query);
  const tokens = tokenSet(query, expectedTerms);
  const ids = [...new Set([...extractIdentifiers(query), ...expectedTerms.flatMap(extractIdentifiers)])];

  let score = 0;
  if (qn && hay.includes(qn)) score += 120;
  for (const id of ids) {
    if (hay.includes(id)) score += 35;
  }
  for (const t of tokens) {
    if (hay.includes(t)) score += 4;
  }
  if (/\/products\//.test(c.url)) score += 10;
  if (/open box|refurbished|renewed|scratch|dent/i.test(c.title || c.url)) score -= 8;
  if (/\+|bundle|kit|knife set|accessory/i.test(c.title || "")) score -= 4;
  if (c.evidence?.source === "search_html" || c.evidence?.sources?.includes("search_html")) score += 8;
  return score;
}

async function enrichWithProductJson(candidates, trace) {
  const selected = candidates.slice(0, MAX_PRODUCT_JSON_FETCHES);
  for (const c of selected) {
    const url = productJsonUrl(c.url);
    try {
      const res = await fetchWithTimeout(
        url,
        { headers: { accept: "application/json,text/plain;q=0.9,*/*;q=0.5" } },
        PRODUCT_JSON_TIMEOUT_MS
      );
      trace.push({ step: "fetch_product_json", url, status: res.status, ok: res.ok });
      if (!res.ok || !/json|javascript|text/.test(res.contentType)) continue;
      const json = safeJsonParse(res.text);
      if (!json || typeof json !== "object") continue;

      if (json.title) c.title = normalizeSpace(json.title);
      if (json.vendor && !c.brand) c.brand = normalizeSpace(json.vendor);
      if (typeof json.available === "boolean") c.availability = json.available ? "available" : "sold out / unavailable";
      if (json.featured_image && !c.image) {
        try {
          c.image = new URL(json.featured_image, "https://www.beachcamera.com").toString();
        } catch {
          c.image = String(json.featured_image || "");
        }
      }
      const variants = Array.isArray(json.variants) ? json.variants : [];
      const skus = [...new Set(variants.map((v) => normalizeSpace(v.sku || "")).filter(Boolean))];
      if (skus.length && !c.sku) c.sku = skus[0];
      c.evidence.product_json_checked = true;
      if (skus.length) c.evidence.skus = skus.slice(0, 5);
    } catch (e) {
      trace.push({ step: "fetch_product_json", url, error: String(e && e.message ? e.message : e).slice(0, 160) });
    }
  }
}

function cleanCandidate(c) {
  const evidence = { ...(c.evidence || {}) };
  delete evidence.source_html;
  return {
    title: c.title || "",
    url: c.url,
    brand: c.brand || "",
    availability: c.availability || "",
    image: c.image || "",
    sku: c.sku || "",
    score: c.score || 0,
    evidence,
  };
}

async function main() {
  const trace = [];
  const input = safeJsonParse(process.argv[2] || "");
  if (!input || typeof input !== "object") {
    return { success: false, error: "Invalid or missing JSON argument in process.argv[2]", fatal: true, trace };
  }

  const query = normalizeSpace(input.query || asArray(input.expected_terms).join(" "));
  const expectedTerms = asArray(input.expected_terms).map(normalizeSpace).filter(Boolean);
  const maxCandidates = Math.max(0, Math.min(50, Number.isFinite(Number(input.max_candidates)) ? Number(input.max_candidates) : 10));

  if (!query) {
    return { success: false, error: "Missing query and expected_terms", fatal: true, trace };
  }

  const base = "https://www.beachcamera.com";
  const searchUrl = `${base}/search?${new URLSearchParams({ q: query }).toString()}`;
  const suggestUrl = `${base}/search/suggest.json?${new URLSearchParams({
    q: query,
    "resources[type]": "product",
    "resources[limit]": String(Math.max(10, Math.min(20, maxCandidates || 10))),
  }).toString()}`;

  const evidence = {
    site_url_input: input.site_url || "",
    canonical_site_url: base,
    query,
    expected_terms: expectedTerms,
    search_url: searchUrl,
    suggest_url: suggestUrl,
  };

  const lists = [];

  let searchFetchSucceeded = false;
  try {
    trace.push({ step: "fetch_search_html", url: searchUrl });
    const res = await fetchWithTimeout(searchUrl, {}, DEFAULT_TIMEOUT_MS);
    searchFetchSucceeded = true;
    evidence.search_status = res.status;
    evidence.final_search_url = res.url;
    evidence.search_content_type = res.contentType;
    trace.push({ step: "fetch_search_html", status: res.status, ok: res.ok, bytes: res.text.length });
    if (res.text) lists.push(parseSearchHtml(res.text, res.url || searchUrl, trace));
  } catch (e) {
    trace.push({ step: "fetch_search_html", error: String(e && e.message ? e.message : e).slice(0, 160) });
  }

  try {
    trace.push({ step: "fetch_search_suggest_json", url: suggestUrl });
    const res = await fetchWithTimeout(
      suggestUrl,
      { headers: { accept: "application/json,*/*;q=0.5" } },
      PRODUCT_JSON_TIMEOUT_MS
    );
    evidence.suggest_status = res.status;
    trace.push({ step: "fetch_search_suggest_json", status: res.status, ok: res.ok, bytes: res.text.length });
    const json = safeJsonParse(res.text);
    if (json) {
      const suggestCandidates = parseSuggestJson(json, base);
      trace.push({ step: "parse_search_suggest_json", product_links: suggestCandidates.length });
      lists.push(suggestCandidates);
    }
  } catch (e) {
    trace.push({ step: "fetch_search_suggest_json", error: String(e && e.message ? e.message : e).slice(0, 160) });
    if (!searchFetchSucceeded) {
      return {
        success: false,
        error: "Unable to fetch Beach Camera search HTML or suggest JSON",
        fatal: true,
        trace,
      };
    }
  }

  let candidates = mergeCandidates(lists);
  for (const c of candidates) c.score = scoreCandidate(c, query, expectedTerms);

  candidates.sort((a, b) => b.score - a.score || (a._order || 0) - (b._order || 0) || a.url.localeCompare(b.url));

  await enrichWithProductJson(candidates.slice(0, Math.max(maxCandidates, MAX_PRODUCT_JSON_FETCHES)), trace);

  for (const c of candidates) c.score = scoreCandidate(c, query, expectedTerms);
  candidates.sort((a, b) => b.score - a.score || (a._order || 0) - (b._order || 0) || a.url.localeCompare(b.url));

  evidence.candidate_count_before_limit = candidates.length;
  candidates = candidates.slice(0, maxCandidates).map(cleanCandidate);
  evidence.candidate_count = candidates.length;

  return { success: true, candidates, evidence, trace };
}

const result = await main().catch((e) => ({
  success: false,
  error: String(e && e.message ? e.message : e),
  fatal: true,
  trace: [{ step: "unhandled_exception", error: String(e && e.stack ? e.stack : e).slice(0, 1000) }],
}));

process.stdout.write(JSON.stringify(result));
