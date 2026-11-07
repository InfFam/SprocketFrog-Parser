# SprocketFrog Parser 🐸⚙️

Парсер каталога товаров маркетплейса **SprocketFrog** — production-ориентированный
скрапер на Python с ретраями, ротацией User-Agent, поддержкой прокси и
checkpoint-ами.

---

## Возможности

- Постраничный обход каталога с автоопределением следующей страницы
- Извлечение полей: `sku`, `title`, `price`, `old_price`, `currency`,
  `discount_percent`, `rating`, `reviews_count`, `availability`, `category`,
  `url`, `image_url`, `tags`, `scraped_at`
- Автоматический расчёт скидки из `price` / `old_price`
- Ретраи с backoff на уровне `HTTPAdapter`, повтор на 429/5xx
- Ротация User-Agent, заголовки, имитирующие реальный браузер
- Поддержка HTTP/SOCKS-прокси
- Checkpoint: при падении продолжит с последней страницы
- Экспорт в CSV (`utf-8-sig` — корректно открывается в Excel)
- CLI через `argparse`

---

## Установка

```bash
git clone https://github.com/<your-nick>/sprocketfrog-parser.git
cd sprocketfrog-parser

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## Использование

```bash
# Базовый запуск — 10 страниц в output/products.csv
python sprocketfrog_parser.py

# Больше страниц, подробный лог
python sprocketfrog_parser.py --max-pages 50 -v

# Свой output, свой прокси
python sprocketfrog_parser.py -o data/frogs.csv --proxy http://user:pass@127.0.0.1:8080

# Игнорировать checkpoint
python sprocketfrog_parser.py --no-resume
```

### CLI-флаги

| Флаг | Назначение | По умолчанию |
|---|---|---|
| `-o, --output` | CSV-файл для результата | `output/products.csv` |
| `--max-pages` | Максимум страниц | `10` |
| `--delay` | Базовая задержка, сек | `1.0` |
| `--proxy` | Прокси URL | — |
| `--no-resume` | Сбросить checkpoint | — |
| `-v, --verbose` | DEBUG-лог | — |

---

## Структура проекта

```
sprocketfrog-parser/
├── README.md
├── requirements.txt
├── sprocketfrog_parser.py
└── output/
    ├── .gitkeep
    └── products.csv
```

---

## Как устроен парсинг

1. **`build_session()`** — `requests.Session` с `Retry`-адаптером
   (backoff на 429/5xx) и пулом соединений.
2. **`fetch()`** — GET с рандомным UA и `Referer`, определяет кодировку по
   `apparent_encoding`, добавляет джиттер к задержке.
3. **`parse_card()`** — извлекает поля через набор fallback-селекторов
   (`.product-card__price` → `.price` → `[data-price]`), нормализует цены
   и рейтинги.
4. **`find_next_page()`** — ищет `a[rel="next"]` в блоке пагинации.
5. **`crawl()`** — основной цикл, сохраняет checkpoint после каждой страницы.
6. **`save_products()`** — пишет `Product`-датаклассы в CSV.

---

## Ограничения

- Не обходит JS-рендеринг — нужен SSR/SSG или Playwright/Selenium.
- Селекторы привязаны к вёрстке SprocketFrog; при изменениях разметки
  правьте `parse_card()`.
- Нет распределённого режима (один процесс).
- Уважайте `robots.txt` и ToS сайтов, которые парсите.

---

## Лицензия

MIT — используйте, форкайте, улучшайте.

---
