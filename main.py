"""
SprocketFrog Catalog Parser
===========================
Production-ориентированный парсер каталога маркетплейса SprocketFrog.
Обходит пагинацию, извлекает карточки товаров и выгружает их в CSV.

Особенности:
    * ротация User-Agent, ретраи с экспоненциальной задержкой;
    * поддержка HTTP/SOCKS-прокси;
    * настраиваемые селекторы и лимит страниц;
    * потокобезопасное логирование;
    * возобновление с последней успешной страницы (checkpoint).

Запуск:
    python sprocketfrog_parser.py --output data/products.csv --max-pages 20
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

BASE_URL = "https://sprocketfrog.net"
CATALOG_PATH = "/catalog"
DEFAULT_OUTPUT = Path("output/products.csv")
CHECKPOINT_FILE = Path("output/.checkpoint.json")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/17.2 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

log = logging.getLogger("sprocketfrog")


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #

@dataclass
class Product:
    sku: str
    title: str
    price: Optional[float]
    old_price: Optional[float]
    currency: str
    discount_percent: Optional[int]
    rating: Optional[float]
    reviews_count: Optional[int]
    availability: str
    category: str
    url: str
    image_url: str
    tags: list[str] = field(default_factory=list)
    scraped_at: str = ""


# --------------------------------------------------------------------------- #
# HTTP-клиент
# --------------------------------------------------------------------------- #

def build_session(proxy: Optional[str] = None) -> requests.Session:
    """Собирает requests.Session с ретраями на уровне адаптера."""
    session = requests.Session()

    retry = Retry(
        total=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    session.headers.update(DEFAULT_HEADERS)
    return session


def _ua() -> str:
    return random.choice(USER_AGENTS)


def fetch(session: requests.Session, url: str, *, timeout: int = 15,
          delay: float = 1.0) -> Optional[str]:
    """Загружает страницу, применяя вежливую задержку до и после запроса."""
    try:
        time.sleep(delay + random.uniform(0, 0.5))
        headers = {"User-Agent": _ua(), "Referer": url}
        log.debug("GET %s", url)
        resp = session.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 404:
            log.warning("404 Not Found: %s", url)
            return None
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except requests.RequestException as exc:
        log.error("Ошибка запроса %s: %s", url, exc)
        return None


# --------------------------------------------------------------------------- #
# Утилиты нормализации
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_DIGITS_RE = re.compile(r"\d+")


def parse_money(raw: Optional[str]) -> Optional[float]:
    """'1 299,00 ₽' -> 1299.0; '$89.99' -> 89.99; None при ошибке."""
    if not raw:
        return None
    cleaned = raw.replace("\u00a0", "").replace("\u202f", "")
    match = _NUM_RE.search(cleaned.replace(" ", ""))
    if not match:
        return None
    value = match.group(0).replace(",", ".")
    try:
        return round(float(value), 2)
    except ValueError:
        return None


def parse_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    digits = _DIGITS_RE.findall(raw)
    return int("".join(digits)) if digits else None


def detect_currency(raw: str) -> str:
    if "₽" in raw or "руб" in raw.lower():
        return "RUB"
    if "$" in raw or "usd" in raw.lower():
        return "USD"
    if "€" in raw or "eur" in raw.lower():
        return "EUR"
    return "RUB"


def absolute_url(href: str, base: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")


def _text(el: Optional[Tag]) -> str:
    return el.get_text(" ", strip=True) if el else ""


# --------------------------------------------------------------------------- #
# Парсинг карточки
# --------------------------------------------------------------------------- #

def parse_card(card: Tag, base_url: str) -> Optional[Product]:
    link = card.select_one("a.product-card__link") or card.select_one("a[href]")
    if not link:
        return None

    title = _text(card.select_one(".product-card__title, h3, .title"))
    if not title:
        return None

    price_raw = _text(card.select_one(".product-card__price, .price, [data-price]"))
    old_price_raw = _text(card.select_one(".product-card__price--old, .old-price, s"))
    rating_raw = _text(card.select_one(".product-card__rating, .rating"))
    reviews_raw = _text(card.select_one(".product-card__reviews, .reviews-count"))
    stock_raw = _text(card.select_one(".product-card__stock, .stock, .availability"))
    category_raw = _text(card.select_one(".product-card__category, .category"))
    img_el = card.select_one("img.product-card__image, img[data-src], img")

    price = parse_money(price_raw)
    old_price = parse_money(old_price_raw)

    discount = None
    if price and old_price and old_price > price > 0:
        discount = int(round((1 - price / old_price) * 100))

    sku = (
        link.get("data-sku")
        or card.get("data-sku")
        or card.get("data-product-id")
        or ""
    )

    image_url = ""
    if img_el:
        image_url = img_el.get("data-src") or img_el.get("src") or ""

    tags = [
        t.get_text(" ", strip=True)
        for t in card.select(".product-card__tag, .tag, .badge")
        if t.get_text(strip=True)
    ]

    return Product(
        sku=str(sku),
        title=title,
        price=price,
        old_price=old_price,
        currency=detect_currency(price_raw + " " + old_price_raw),
        discount_percent=discount,
        rating=parse_money(rating_raw),
        reviews_count=parse_int(reviews_raw),
        availability=stock_raw or "unknown",
        category=category_raw,
        url=absolute_url(link.get("href", ""), base_url),
        image_url=absolute_url(image_url, base_url),
        tags=tags,
        scraped_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def parse_catalog(html: str, base_url: str) -> Iterator[Product]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("article.product-card, .product-card, li.product-item")
    log.info("На странице найдено карточек: %d", len(cards))
    for card in cards:
        try:
            product = parse_card(card, base_url)
            if product:
                yield product
        except Exception as exc:  # noqa: BLE001
            log.warning("Пропущена карточка: %s", exc)


def find_next_page(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    link = (
        soup.select_one("a.pagination__next[rel='next']")
        or soup.select_one("a[rel='next']")
        or soup.select_one("a.next-page, .pagination .next a")
    )
    if not link:
        return None
    return absolute_url(link.get("href", ""), base_url)


# --------------------------------------------------------------------------- #
# Checkpoint
# --------------------------------------------------------------------------- #

def load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_checkpoint(data: dict) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


# --------------------------------------------------------------------------- #
# Запись результата
# --------------------------------------------------------------------------- #

FIELDNAMES = [
    "sku", "title", "price", "old_price", "currency", "discount_percent",
    "rating", "reviews_count", "availability", "category", "url",
    "image_url", "tags", "scraped_at",
]


def save_products(products: Iterable[Product], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(products)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for p in rows:
            row = asdict(p)
            row["tags"] = "; ".join(p.tags)
            writer.writerow(row)
    log.info("Сохранено %d записей в %s", len(rows), path)
    return len(rows)


# --------------------------------------------------------------------------- #
# Основной цикл
# --------------------------------------------------------------------------- #

def crawl(*, max_pages: int, delay: float, proxy: Optional[str],
          output: Path, resume: bool) -> int:
    session = build_session(proxy)
    state = load_checkpoint() if resume else {}
    start_page = state.get("next_page", 1)
    collected: list[Product] = []
    next_url = state.get("next_url") or f"{BASE_URL}{CATALOG_PATH}"

    page = start_page
    while next_url and page <= max_pages:
        log.info("── Страница %d: %s", page, next_url)
        html = fetch(session, next_url, delay=delay)
        if html is None:
            log.error("Не удалось загрузить страницу %d, прерываемся.", page)
            break

        page_products = list(parse_catalog(html, BASE_URL))
        collected.extend(page_products)

        nxt = find_next_page(html, BASE_URL)
        save_checkpoint({"next_url": nxt, "next_page": page + 1})
        next_url = nxt
        page += 1

    if collected:
        save_products(collected, output)
    else:
        log.warning("Товары не найдены — проверить селекторы или доступность сайта.")
    return len(collected)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sprocketfrog-parser",
        description="Парсер каталога товаров SprocketFrog.",
    )
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                   help=f"CSV-файл для результата (по умолчанию {DEFAULT_OUTPUT})")
    p.add_argument("--max-pages", type=int, default=10,
                   help="Максимальное число страниц (по умолчанию 10)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Базовая задержка между запросами, сек (по умолчанию 1.0)")
    p.add_argument("--proxy", type=str, default=None,
                   help="Прокси в формате http://user:pass@host:port")
    p.add_argument("--no-resume", action="store_true",
                   help="Игнорировать checkpoint и начать сначала")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Подробный лог")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("SprocketFrog parser: start")
    try:
        total = crawl(
            max_pages=args.max_pages,
            delay=args.delay,
            proxy=args.proxy,
            output=args.output,
            resume=not args.no_resume,
        )
    except KeyboardInterrupt:
        log.warning("Прервано пользователем.")
        return 130
    log.info("Готово. Всего записей: %d", total)
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
