#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
READINESS PROBE — проверяемый аудит «может ли ИИ-агент купить этот товар».

Никакой эвристики «на глаз»: каждая метрика выводится из конкретного
сигнала в ответе сервера и воспроизводится повторным запуском.

Что измеряется (по тому, что агент реально может прочитать без браузера):
  status        — код ответа на анонимный GET
  rendered      — размер серверного HTML (SPA-оболочка отличима от витрины)
  jsonld        — блоки application/ld+json
  product_jsonld— сколько из них Product/Offer
  microdata     — разметка itemtype=
  offers        — цена/наличие в разметке
  gtin_brand_sku— идентификаторы, без которых агент не отличает товары
  img_big       — картинки >= 600x600 (требование каналов ИИ-агентов)
  rss_feed      — открытый товарный фид (yml/xml/csv) по robots/sitemap
  agents_txt    — /agents.txt, llms.txt, /.well-known/ — при наличии
  checkout_api  — следы открытого agent-checkout (ACP/UCP/YCP-эндпоинты)

Формат вывода: JSON (машино) + Markdown (человек).

Запуск:
    python readiness_probe.py --targets targets.json --out out/
    python readiness_probe.py --url "https://example.ru/cat/" --name Example
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 25
RETRY_SLEEP = 3.0

# Поля, без которых агент покупателя отбрасывает позицию из сравнения.
REQUIRED_ATTRS = ("name", "brand", "gtin", "sku", "price", "priceCurrency", "availability")

# Каналы ИИ-агентов отбрасывают карточку без структурированных данных.
# Порог из публичных исследований: ~40% ассистентов пропускают такие позиции.
INCOMPLETE_ATTR_DROP_RATE = 0.40


def fetch(url: str, method: str = "GET", timeout: int = TIMEOUT) -> tuple[int, str, str]:
    """Возвращает (status, body, final_url). Сетевые ошибки — status 0."""
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read(6_000_000)
            enc = r.headers.get_content_charset() or "utf-8"
            if raw[:2] == b"\x1f\x8b":
                try:
                    import gzip

                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return r.status, raw.decode(enc, errors="replace"), r.geturl()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(500_000)
            enc = e.headers.get_content_charset() or "utf-8"
            return e.code, raw.decode(enc, errors="replace"), url
        except Exception:
            return e.code, "", url
    except Exception as e:  # noqa: BLE001
        return 0, f"__error__:{type(e).__name__}:{e}", url


def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


def extract_jsonld(html: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                blocks.append(item)
    return blocks


def walk_types(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out.extend(str(x) for x in t)
        for v in node.values():
            walk_types(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_types(v, out)


ENTITY_TYPES = {
    "product",
    "productgroup",
    "productmodel",
    "course",
    "vehicle",
    "book",
    "softwareapplication",
    "softwareapplicationcategory",
}


def find_products(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Возвращает сущности, которые САМИ объявляют тип товара.

    Разметка бывает трёх видов, и все три нужно понимать одинаково:

      {"@type": "Product", "name": ..., "offers": {...}}            — обычный Product
      {"@type": "ListItem", "item": {"@type": "Course", "name": ...}} — список
      {"schema": {"@type": "ItemList", "itemListElement": [...]}}      — обёртка

    Поэтому: ListItem разворачиваем в item; внешнюю обёртку (schema) игнорируем;
    в найденную сущность не спускаемся глубже.
    """
    found: list[dict[str, Any]] = []

    def own_types(node: dict[str, Any]) -> set[str]:
        t = node.get("@type")
        if isinstance(t, str):
            return {t.lower()}
        if isinstance(t, list):
            return {str(x).lower() for x in t}
        return set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            own = own_types(node)
            if "listitem" in own and isinstance(node.get("item"), dict):
                visit(node["item"])
                return
            if own & ENTITY_TYPES:
                found.append(node)
                return
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    for b in blocks:
        visit(b)
    return found


def _gtin_of(entity: dict[str, Any], offers: list[dict[str, Any]]) -> Any:
    """Строгий GTIN/EAN/UPC. productID и identifier без имени типа не считаются."""
    for src in (entity, *offers):
        for k in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
            v = src.get(k)
            if v not in (None, "", [], {}):
                return v
        idents = src.get("identifier")
        if isinstance(idents, dict):
            idents = [idents]
        if isinstance(idents, list):
            for it in idents:
                if not isinstance(it, dict):
                    continue
                name = str(it.get("name", "")).lower()
                value = it.get("value")
                if value not in (None, "") and any(
                    s in name for s in ("gtin", "ean", "upc", "isbn")
                ):
                    return value
    return None


def product_completeness(p: dict[str, Any]) -> dict[str, Any]:
    """Считает покрытие обязательных полей по сущности товара.

    Имя берётся у сущности, цена/валюта/наличие — у её offers (они часто
    лежат в отдельном Offer-узле, без имени).
    """
    offers = p.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        offers = []
    offers = [o for o in offers if isinstance(o, dict)]

    def get(*keys: str) -> Any:
        for src in (p, *offers):
            for k in keys:
                v = src.get(k)
                if v not in (None, "", [], {}):
                    return v
        return None

    checks = {
        "name": get("name", "title", "headline") is not None,
        "brand": get("brand", "manufacturer", "provider") is not None,
        "gtin": _gtin_of(p, offers) is not None,
        "sku": get("sku", "mpn", "productID") is not None,
        "price": get("price", "lowPrice", "priceSpecification") is not None,
        "priceCurrency": get("priceCurrency") is not None,
        "availability": get("availability") is not None,
    }
    img = p.get("image")
    images: list[str] = []
    if isinstance(img, str):
        images = [img]
    elif isinstance(img, list):
        images = [i for i in img if isinstance(i, str)]
    elif isinstance(img, dict):
        if isinstance(img.get("url"), str):
            images = [img["url"]]
    return {"checks": checks, "complete": all(checks.values()), "images": images[:5]}


def large_images(html: str, min_side: int = 600) -> dict[str, Any]:
    """Ищет размеры картинок, объявленные в HTML (width/height или srcset)."""
    total = 0
    big = 0
    for m in re.finditer(r'<img\b[^>]*>', html, re.I):
        total += 1
        tag = m.group(0)
        w = re.search(r'\bwidth=["\']?(\d+)', tag, re.I)
        h = re.search(r'\bheight=["\']?(\d+)', tag, re.I)
        declared = False
        if w and h:
            declared = int(w.group(1)) >= min_side and int(h.group(1)) >= min_side
        if not declared:
            src = re.search(r'\bsrcset=["\']([^"\']+)', tag, re.I)
            if src and re.search(r"\s(\d{3,4})w", src.group(1)):
                declared = True
        if declared:
            big += 1
    return {"img_total": total, "img_big": big}


def detect_feed(html: str, base: str) -> list[str]:
    """Ищет ссылки на товарные фиды в разметке и служебных файлах."""
    found: list[str] = []
    for m in re.finditer(r'href=["\']([^"\']*(?:feed|yml|xml|csv|price)[^"\']*)["\']', html, re.I):
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = base.rstrip("/") + u
        if u.startswith("http") and u not in found:
            found.append(u)
    return found[:12]


def detect_agent_surfaces(base: str) -> dict[str, Any]:
    """Проверяет наличие агентных точек входа."""
    out: dict[str, Any] = {}
    probes = {
        "robots_txt": "/robots.txt",
        "agents_txt": "/agents.txt",
        "llms_txt": "/llms.txt",
        "well_known_agent": "/.well-known/agent.json",
        "well_known_ucp": "/.well-known/ucp",
        "openapi": "/openapi.json",
    }
    for key, path in probes.items():
        status, body, _ = fetch(base.rstrip("/") + path, method="GET")
        marker = ""
        if status == 200 and body:
            low = body.lower()
            # Декларация агента — это именованные директивы (GPTBot, ClaudeBot,
            # PerplexityBot, ...). Универсальное "User-agent: *" декларацией не
            # является: оно есть в каждом robots.txt и не говорит ни о чём.
            named = {
                m
                for m in re.findall(r"user-agent\s*:\s*([^\r\n]+)", low)
                if m.strip() not in ("*", "")
            }
            if named:
                marker = "declared_agents:" + ",".join(sorted(named)[:4])
            elif "agent" in low or "llm" in low:
                marker = "agent_hint"
        out[key] = {"status": status, "marker": marker}
        time.sleep(0.4)
    return out


def detect_checkout_api(html: str, base: str) -> dict[str, Any]:
    """Ищет следы открытого agent-checkout в разметке и в endpoint-ссылках."""
    text = html.lower()
    hits: list[str] = []
    for needle, label in (
        ("checkout_sessions", "ACP-style checkout_sessions"),
        ("/checkout/session", "ACP-style checkout session"),
        ("/.well-known/ucp", "UCP profile"),
        ("ycp", "YCP mention"),
        ("agentic-checkout", "agentic checkout"),
        ("shopping-agent", "shopping agent"),
        ("agent_id", "agent id field"),
        ("x-agent-", "X-Agent-* header"),
    ):
        if needle in text:
            hits.append(label)
    return {"signals": hits, "has_open_agent_checkout": bool(hits)}


@dataclass
class Result:
    name: str
    url: str
    status: int = 0
    final_url: str = ""
    rendered_bytes: int = 0
    text_chars: int = 0
    jsonld_blocks: int = 0
    product_nodes: int = 0
    product_complete: int = 0
    microdata_items: int = 0
    microdata_products: int = 0
    img_total: int = 0
    img_big: int = 0
    feeds: list[str] = field(default_factory=list)
    surfaces: dict[str, Any] = field(default_factory=dict)
    checkout: dict[str, Any] = field(default_factory=dict)
    agent_visible_share: float | None = None
    notes: list[str] = field(default_factory=list)
    error: str = ""
    score: dict[str, Any] = field(default_factory=dict)
    pages_probed: int = 0
    sitemap_product_urls: int = 0
    field_coverage: dict[str, int] = field(default_factory=dict)
    field_total: int = 0


IMAGE_EXT = (".webp", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".avif", ".css", ".js", ".ico")


ID_PATTERN = re.compile(r"\d{4,}|[a-z\-]{3,}-\d{3,}")


def discover_links(base_url: str, html: str, hints: tuple[str, ...], limit: int) -> list[str]:
    """Находит на странице ссылки по подсказкам в пути — как это сделал бы агент.

    Дополнительно принимаются «страницы с идентификатором» вида /smartfony-22899/:
    так устроено большинство российских витрин, и по одному лишь наличию
    сегмента с цифрами карточка отличается от служебной страницы.
    """
    out: list[str] = []
    root = "/".join(base_url.split("/")[:3])
    for m in re.finditer(r'href=["\']([^"\'#?]{5,220})["\']', html, re.I):
        u = m.group(1)
        low = u.lower()
        if low.endswith(IMAGE_EXT):
            continue
        last = low.rstrip("/").split("/")[-1]
        if last.endswith(".xml") or last.endswith(".gz") or last.endswith(".txt"):
            continue
        hinted = any(h in low for h in hints)
        id_like = bool(ID_PATTERN.search(last)) and len(last) > 4
        if not (hinted or id_like):
            continue
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = root + u
        elif not u.startswith("http"):
            continue
        if u.startswith("http") and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


PRODUCT_HINTS = ("/product/", "/item/", "/goods/", "/prod/", "/tovar/", "/offer/", "/p/", "/view/")
CATEGORY_HINTS = ("/catalog/", "/category/", "/rubric", "/section/", "/products/", "/list/", "/c/")


def fetch_sitemap(base: str, limit: int = 400) -> list[str]:
    """Берёт карточки товаров из публичного sitemap — так же, как любой краулер."""
    locs: list[str] = []
    roots = [base.rstrip("/") + "/sitemap.xml", base.rstrip("/") + "/sitemap_index.xml"]
    seen_sitemaps: list[str] = []
    for s in roots:
        st, body, _ = fetch_with_retry(s)
        if st == 200 and body and "<loc" in body:
            seen_sitemaps.append(s)
            locs.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I))
    # sitemap_index может ссылаться на дочерние карты
    for s in list(seen_sitemaps):
        st, body, _ = fetch_with_retry(s)
        if st != 200 or not body:
            continue
        children = [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I) if u.endswith(".xml")]
        for child in children[:6]:
            cst, cbody, _ = fetch_with_retry(child)
            if cst == 200 and cbody:
                locs.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", cbody, re.I))
    out: list[str] = []
    product_like: list[str] = []
    leaf_like: list[str] = []
    for u in locs:
        low = u.lower()
        if low.endswith(IMAGE_EXT):
            continue
        # Сами файлы sitemap — это не карточки товаров.
        last = low.rstrip("/").split("/")[-1]
        if last.endswith(".xml") or last.endswith(".gz") or last.endswith(".txt"):
            continue
        path = low.split("://", 1)[-1].split("/", 1)[-1]
        depth = len([s for s in path.split("/") if s])
        if any(h in low for h in PRODUCT_HINTS):
            product_like.append(u)
        elif depth >= 2 and not any(h in low for h in CATEGORY_HINTS):
            # Листовые страницы: у многих витрин товарный URL без маркера
            # /product/, поэтому отбрасываем только явные категории.
            leaf_like.append(u)
    out = product_like + leaf_like
    return out[:limit]


def fetch_with_retry(url: str, tries: int = 3) -> tuple[int, str, str]:
    """Повторяет запрос при сетевых сбоях: часть витрин рвёт TLS из-за нагрузки."""
    last = (0, "", url)
    for i in range(tries):
        last = fetch(url)
        if last[0] != 0:
            return last
        if i < tries - 1:
            time.sleep(2.0 * (i + 1))
    return last


def probe(name: str, url: str, deep: bool = True, discover: int = 4) -> Result:
    r = Result(name=name, url=url)
    status, body, final = fetch_with_retry(url)
    r.status = status
    r.final_url = final
    if status == 0:
        r.error = body[:200]
        r.notes.append("Сеть недоступна или соединение прервано")
        return r
    if status >= 400:
        r.notes.append(f"Анонимный GET вернул {status} — агент без браузера страницу не читает")
        r.rendered_bytes = len(body)
        if deep:
            base = "/".join(final.split("/")[:3]) + "/"
            r.surfaces = detect_agent_surfaces(base)
            r.checkout = detect_checkout_api(body, base)
        return r

    r.rendered_bytes = len(body)
    r.text_chars = len(strip_tags(body))

    blocks = extract_jsonld(body)
    r.jsonld_blocks = len(blocks)
    products = find_products(blocks)
    r.product_nodes = len(products)
    r.pages_probed = 1
    all_products: list[dict[str, Any]] = list(products)

    # Агент не останавливается на главной: берём карточки из sitemap, затем из ссылок.
    site_base = "/".join(final.split("/")[:3]) + "/"
    sitemap_urls = fetch_sitemap(site_base) if discover > 0 else []
    r.sitemap_product_urls = len(sitemap_urls)

    product_urls = list(sitemap_urls[: discover * 2])
    if not product_urls:
        product_urls = discover_links(final, body, PRODUCT_HINTS, limit=discover)
    category_urls = discover_links(final, body, CATEGORY_HINTS, limit=2)

    pages_to_open: list[str] = list(product_urls)
    if not pages_to_open and category_urls:
        for cat in category_urls:
            st, cbody, cfinal = fetch_with_retry(cat)
            if st != 200 or not cbody:
                r.notes.append(f"Категория {cat.rstrip('/').split('/')[-1][:40]} → HTTP {st}")
                continue
            r.pages_probed += 1
            found = discover_links(cfinal, cbody, PRODUCT_HINTS, limit=discover)
            for f in found:
                if f not in pages_to_open:
                    pages_to_open.append(f)
            pb = extract_jsonld(cbody)
            pp = find_products(pb)
            if pp:
                r.product_nodes += len(pp)
                r.jsonld_blocks += len(pb)
                all_products.extend(pp)
                for p in pp:
                    if product_completeness(p)["complete"]:
                        r.product_complete += 1

    for cand in pages_to_open[: discover + 2]:
        st, cbody, cfinal = fetch_with_retry(cand)
        if st != 200 or not cbody:
            r.notes.append(
                f"Карточка {cand.rstrip('/').split('/')[-1][:44]} → HTTP {st}: товар агент не прочитает"
            )
            continue
        r.pages_probed += 1
        pblocks = extract_jsonld(cbody)
        pprods = find_products(pblocks)
        if pprods:
            r.product_nodes += len(pprods)
            r.jsonld_blocks += len(pblocks)
            all_products.extend(pprods)
            for p in pprods:
                if product_completeness(p)["complete"]:
                    r.product_complete += 1
            im = large_images(cbody)
            r.img_total += im["img_total"]
            r.img_big += im["img_big"]
        else:
            r.notes.append(
                f"Карточка {cand.rstrip('/').split('/')[-1][:44]}: JSON-LD Product нет — "
                "агент не получит атрибуты"
            )

    if not pages_to_open:
        r.notes.append(
            "Ссылки на карточки товаров с витрины не обнаружены — "
            "проверьте, не отдаётся ли каталог только клиентским JS"
        )

    if r.product_nodes:
        r.agent_visible_share = round(r.product_complete / r.product_nodes, 4)
        if r.product_complete < r.product_nodes:
            r.notes.append(
                f"Из {r.product_nodes} Product-узлов полными являются {r.product_complete} — "
                f"остальные выпадают из сравнения агента"
            )
    else:
        r.notes.append(
            "Структурированных данных о товарах не найдено ни на одной открытой странице"
        )

    # Покрытие по полям: какое именно поле отсутствует у большинства.
    # Одна бинарная цифра «полных карточек 0» бесполезна без разбора.
    coverage = {k: 0 for k in REQUIRED_ATTRS}
    checked = 0
    for p in all_products:
        checks = product_completeness(p)["checks"]
        checked += 1
        for k, v in checks.items():
            if v:
                coverage[k] = coverage.get(k, 0) + 1
    r.field_coverage = coverage
    r.field_total = checked

    r.microdata_items = len(re.findall(r"itemtype=", body, re.I))
    r.microdata_products = len(re.findall(r'itemtype=["\'][^"\']*Product', body, re.I))

    if r.microdata_products and not r.product_nodes:
        r.notes.append("Есть microdata, но нет JSON-LD — часть агентских читателей берёт только JSON-LD")

    if r.text_chars < 4000 and r.product_nodes == 0:
        r.notes.append(
            "Сервер отдал SPA-оболочку: текста мало, разметки нет. "
            "Агент без браузерной автоматизации каталог не прочитает"
        )

    base = "/".join(final.split("/")[:3]) + "/"
    r.feeds = detect_feed(body, base)
    r.checkout = detect_checkout_api(body, base)

    if deep:
        r.surfaces = detect_agent_surfaces(base)

    return r


def score(r: Result) -> dict[str, Any]:
    """Итоговая оценка читаемости каталога агентом (0..100) и вердикт."""
    reasons: list[str] = []
    s = 0.0
    if r.status == 200:
        s += 15
    else:
        reasons.append(f"анонимный GET → {r.status}")
    if r.product_nodes > 0:
        s += 25
    else:
        reasons.append("нет JSON-LD Product")
    if r.agent_visible_share is not None:
        s += 30 * r.agent_visible_share
        if r.agent_visible_share < 0.8:
            reasons.append(f"полных карточек только {r.agent_visible_share:.0%}")
    else:
        reasons.append("доля не измеряется")
    if r.microdata_products:
        s += 5
    if r.img_big > 0:
        s += 5
    else:
        reasons.append("нет картинок >= 600x600 в разметке")
    if r.checkout.get("has_open_agent_checkout"):
        s += 10
    else:
        reasons.append("нет открытого agent-checkout")
    if r.feeds:
        s += 5
    else:
        reasons.append("нет открытого товарного фида")
    if r.surfaces.get("agents_txt", {}).get("status") == 200:
        s += 5
    score_val = max(0.0, min(100.0, s))
    if score_val >= 70:
        verdict = "READY"
    elif score_val >= 35:
        verdict = "PARTIAL"
    else:
        verdict = "INVISIBLE"
    return {"score": round(score_val, 1), "verdict": verdict, "reasons": reasons}


def md_table(rows: list[dict[str, Any]]) -> str:
    head = "| Цель | HTTP | JSON-LD Product | Полных | Доля | ≥600px | Фиды | Agent-checkout | Балл | Вердикт |"
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    out = [head, sep]
    for r in rows:
        share = r["agent_visible_share"]
        share_s = "—" if share is None else f"{share:.0%}"
        out.append(
            "| {name} | {status} | {pn} | {pc} | {share} | {img} | {feeds} | {co} | {score} | **{verdict}** |".format(
                name=r["name"],
                status=r["status"],
                pn=r["product_nodes"],
                pc=r["product_complete"],
                share=share_s,
                img=f"{r['img_big']}/{r['img_total']}",
                feeds="да" if r["feeds"] else "нет",
                co="да" if r["checkout"].get("has_open_agent_checkout") else "нет",
                score=r["score"]["score"],
                verdict=r["score"]["verdict"],
            )
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", help="JSON: [{\"name\":..., \"url\":...}]")
    ap.add_argument("--url")
    ap.add_argument("--name", default="target")
    ap.add_argument("--out", default="out")
    ap.add_argument("--no-deep", action="store_true", help="без зондов /robots.txt и т.п.")
    ap.add_argument("--sleep", type=float, default=RETRY_SLEEP, help="пауза между целями, сек")
    ap.add_argument("--discover", type=int, default=4, help="сколько карточек товара открыть на каждой цели")
    args = ap.parse_args()

    targets: list[dict[str, str]] = []
    if args.targets:
        with open(args.targets, "r", encoding="utf-8") as f:
            targets = json.load(f)
    if args.url:
        targets.append({"name": args.name, "url": args.url})
    if not targets:
        ap.error("нужен --targets или --url")

    os.makedirs(args.out, exist_ok=True)
    results: list[Result] = []
    for t in targets:
        print(f"→ {t['name']} {t['url']}", file=sys.stderr, flush=True)
        r = probe(t["name"], t["url"], deep=not args.no_deep, discover=args.discover)
        r.score = score(r)
        results.append(r)
        time.sleep(args.sleep)

    payload = {
        "tool": "readiness-probe",
        "version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "anonymous_get": True,
            "browser_automation": False,
            "required_attrs": list(REQUIRED_ATTRS),
            "min_image_side": 600,
        },
        "results": [asdict(r) for r in results],
    }
    jp = os.path.join(args.out, "readiness.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    lines = [
        "# Отчёт о готовности каталогов к покупкам ИИ-агентом",
        "",
        f"Сгенерировано: {payload['generated_at']}",
        "",
        "Метод: анонимный GET без браузерной автоматизации, тем же способом, "
        "которым работает ИИ-агент покупателя. Каждая цифра воспроизводится повторным запуском.",
        "",
        md_table([asdict(r) for r in results]),
        "",
        "## Что означают вердикты",
        "",
        "- **READY** — агент может прочитать позицию и оформить заказ.",
        "- **PARTIAL** — позиции видны частично; часть выпадает из сравнения.",
        "- **INVISIBLE** — анонимный агент каталог не читает.",
        "",
        "## Подробности",
        "",
    ]
    for r in results:
        lines.append(f"### {r.name}")
        lines.append("")
        lines.append(f"- URL: {r.url} → {r.final_url}")
        lines.append(f"- HTTP: {r.status}; серверный HTML: {r.rendered_bytes} байт; видимый текст: {r.text_chars} символов")
        lines.append(f"- JSON-LD блоков: {r.jsonld_blocks}; Product-узлов: {r.product_nodes}; полных: {r.product_complete}")
        lines.append(f"- Микроразметка itemtype: {r.microdata_items}; Product: {r.microdata_products}")
        lines.append(f"- Картинки >= 600px: {r.img_big} из {r.img_total}")
        lines.append(f"- Балл: {r.score['score']} — вердикт {r.score['verdict']}")  # type: ignore[attr-defined]
        if r.field_total:
            cov = ", ".join(
                f"{k} {r.field_coverage.get(k, 0)}/{r.field_total}" for k in REQUIRED_ATTRS
            )
            lines.append(f"- Покрытие полей: {cov}")
        if r.notes:
            lines.append("- Замечания:")
            for n in r.notes:
                lines.append(f"  - {n}")
        if r.checkout.get("signals"):
            lines.append(f"- Сигналы agent-checkout: {', '.join(r.checkout['signals'])}")
        if r.feeds:
            lines.append(f"- Найденные фиды: {', '.join(r.feeds[:5])}")
        surf = {k: v for k, v in r.surfaces.items() if v.get("status") == 200}
        if surf:
            lines.append(f"- Открытые служебные файлы: {', '.join(surf)}")
        lines.append("")

    mp = os.path.join(args.out, "readiness.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # Сводное покрытие полей по всем целям сразу: это и есть главный вывод.
    total = sum(r.field_total for r in results)
    if total:
        lines.append("")
        lines.append("## Сводное покрытие обязательных полей")
        lines.append("")
        lines.append(f"Всего Product-узлов разобрано: **{total}**")
        lines.append("")
        lines.append("| Поле | Заполнено | Доля |")
        lines.append("|---|---|---|")
        for k in REQUIRED_ATTRS:
            got = sum(r.field_coverage.get(k, 0) for r in results)
            lines.append(f"| `{k}` | {got} | {got / total:.0%} |")
        full = sum(r.product_complete for r in results)
        lines.append("")
        lines.append(f"Полным набором полей обладают **{full}** из {total} карточек ({full / total:.0%}).")
        lines.append("")
        with open(mp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    print(f"JSON:  {jp}")
    print(f"MD:    {mp}")
    for r in results:
        print(f"  {r.name:28} HTTP {r.status:>3}  score {r.score['score']:>5}  {r.score['verdict']}")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
