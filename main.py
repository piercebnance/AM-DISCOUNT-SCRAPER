import logging
import os
import re
from datetime import date
from urllib.parse import quote_plus

from dotenv import load_dotenv
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, status, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static") #Static files directory

templates = Jinja2Templates(directory="templates") #HTML templates directory

SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY", "")


def build_amazon_search_url(query: str) -> str:
    return f"https://www.amazon.com/s?k={quote_plus(query)}"


def extract_price(result) -> str:
    for price_container in result.select("span.a-price"):
        offscreen = price_container.select_one("span.a-offscreen")
        if offscreen:
            text = offscreen.get_text(strip=True)
            if text:
                return text

        whole = price_container.select_one("span.a-price-whole")
        fraction = price_container.select_one("span.a-price-fraction")
        if whole:
            text = whole.get_text(strip=True)
            if fraction:
                text += f".{fraction.get_text(strip=True)}"
            if text:
                return text

    offscreen = result.select_one("span.a-offscreen")
    if offscreen:
        text = offscreen.get_text(strip=True)
        if text:
            return text

    whole = result.select_one("span.a-price-whole")
    fraction = result.select_one("span.a-price-fraction")
    if whole:
        text = whole.get_text(strip=True)
        if fraction:
            text += f".{fraction.get_text(strip=True)}"
        if text:
            return text

    return ""


async def fetch_amazon_search_page(query: str) -> str:
    if not SCRAPINGBEE_API_KEY:
        raise RuntimeError("Missing SCRAPINGBEE_API_KEY environment variable.")

    params = {
        "api_key": SCRAPINGBEE_API_KEY,
        "url": build_amazon_search_url(query),
        "premium_proxy": "true",
        "country_code": "us",
        "render_js": "true",
    }

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get("https://app.scrapingbee.com/api/v1", params=params, headers=headers)
        response.raise_for_status()
        text = response.text
        if "Sorry! Something went wrong!" in text or "automated access" in text.lower():
            logger.error("Amazon returned a blocked page or 503. Response length=%s", len(text))
            raise RuntimeError("Amazon blocked the request or returned an error page.")
        return text


def parse_amazon_discounted_products(html: str, query: str, max_items: int = 20) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for result in soup.select("div.s-result-item[data-asin], div[data-asin], div[data-component-type='s-search-result']"):
        asin = result.get("data-asin", "").strip()
        if not asin:
            continue

        title_tag = _extract_title(result)
        link_tag = result.select_one("h2 a.a-link-normal") or result.select_one("h2 a")

        price_data = _extract_price_data(result)
        current_price = price_data["current_price"]
        original_price = price_data["original_price"]
        discount_label = price_data["discount_label"]
        current_num = price_data["current_num"]
        original_num = price_data["original_num"]
        discount_score = price_data["discount_score"]
        image_url = _extract_image_url(result)

        if not title_tag or not current_price or not link_tag:
            continue

        relative_link = link_tag["href"] if link_tag and link_tag.has_attr("href") else ""
        product_url = (
            f"https://www.amazon.com{relative_link}"
            if relative_link.startswith("/")
            else relative_link
        )

        content_parts = [f"<strong>Current price:</strong> {current_price}"]
        if original_price and original_num is not None and current_num is not None and original_num > current_num:
            content_parts.append(f"<strong>Original price:</strong> {original_price}")
        if discount_label:
            content_parts.append(f"<strong>Discount price:</strong> {current_price}")
            content_parts.append(f"<strong>Discount info:</strong> {discount_label}")
        else:
            content_parts.append("<strong>Discount info:</strong> No explicit discount signal detected")

        if not discount_label and not (original_num is not None and current_num is not None and original_num > current_num):
            continue

        results.append(
            {
                "id": len(results) + 1,
                "author": "Amazon Deals",
                "title": title_tag,
                "current_price": current_price,
                "original_price": original_price if original_price and original_num is not None and current_num is not None and original_num > current_num else "",
                "discount_price": current_price if discount_label else "",
                "discount_label": discount_label,
                "content": "<br>".join(content_parts),
                "date_posted": date.today().strftime("%B %d, %Y"),
                "url": product_url,
                "image_url": image_url,
                "discounted": bool(discount_label),
                "discount_score": discount_score,
            }
        )

    results.sort(key=lambda item: item.get("discount_score", 0), reverse=True)
    return results[:max_items]


def _extract_price_number(price_text: str) -> float | None:
    if not price_text:
        return None

    match = re.search(r"(\d+(?:,\d{3})*(?:\.\d{1,2})?)", price_text)
    if not match:
        return None

    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_original_price(result) -> str:
    original_price_tag = (
        result.select_one("span.a-price.a-text-price span.a-offscreen")
        or result.select_one("span.a-text-price span.a-offscreen")
        or result.select_one("span.a-price.a-text-price")
        or result.select_one("span.a-text-price")
    )
    return original_price_tag.get_text(strip=True) if original_price_tag else ""


def _extract_price_data(result) -> dict:
    current_price = extract_price(result)
    original_price = _extract_original_price(result)
    current_num = _extract_price_number(current_price)
    original_num = _extract_price_number(original_price)

    savings_text = ""
    for node in result.find_all(string=True):
        if not isinstance(node, str):
            continue
        text = node.strip()
        if not text:
            continue
        if _is_discount_text(text):
            savings_text = text
            break

    badge_tag = result.select_one("span.a-badge-text")
    badge_text = badge_tag.get_text(strip=True) if badge_tag and _is_discount_text(badge_tag.get_text(strip=True)) else ""

    discount_label = ""
    if savings_text:
        discount_label = savings_text
    elif badge_text:
        discount_label = badge_text
    elif current_num is not None and original_num is not None and original_num > current_num:
        percent_drop = (original_num - current_num) / original_num * 100 if original_num else 0
        if percent_drop >= 20:
            discount_label = f"{percent_drop:.0f}% off"

    discount_score = 0
    if savings_text:
        discount_score += 100
    elif badge_text:
        discount_score += 80
    elif discount_label and current_num is not None and original_num is not None and original_num > current_num:
        discount_score += 60

    discount_price = current_price if discount_label else ""
    return {
        "current_price": current_price,
        "original_price": original_price,
        "discount_price": discount_price,
        "discount_label": discount_label,
        "current_num": current_num,
        "original_num": original_num,
        "discount_score": discount_score,
    }


def _extract_image_url(result) -> str:
    for candidate in result.select("img"):
        for attr in ("data-src", "data-old-hires", "src", "data-image"):
            value = candidate.get(attr, "")
            if not value:
                continue
            cleaned = value.strip()
            if cleaned.startswith("//"):
                return f"https:{cleaned}"
            if cleaned.startswith("http://") or cleaned.startswith("https://"):
                return cleaned

    for candidate in result.select("img"):
        srcset = candidate.get("srcset", "")
        if srcset:
            first_url = srcset.split(",")[0].strip().split()[0]
            if first_url.startswith("//"):
                return f"https:{first_url}"
            if first_url.startswith("http://") or first_url.startswith("https://"):
                return first_url

    return ""


def _sanitize_title(text: str) -> str:
    if not text:
        return ""

    sanitized = text.strip()
    sanitized = re.sub(r"\s+", " ", sanitized)

    ignore_title_patterns = [
        r"lowest price in \d+ days",
        r"in \d+ days",
        r"more buying choices",
        r"compare with",
        r"add to list",
        r"free shipping",
        r"prime",
        r"amazon's choice",
        r"best seller",
        r"ships from",
        r"recycled materials? \+ \d+ more",
        r"products highlighted as overall pick",
        r"products highlighted as 'overall pick' are",
        r"recycled materials",
        r"count",
        r"small business",
        r"safer chemicals",
        r"free delivery",
        r"free shipping",
        r"Amazon Store Card",
        r"forestry practices",
    ]
    if any(re.search(pattern, sanitized.lower()) for pattern in ignore_title_patterns):
        return ""

    return sanitized


def _extract_title(result) -> str | None:
    candidates = [
        result.select_one("h2 a span"),
        result.select_one("span.a-size-medium.a-color-base.a-text-normal"),
        result.select_one("span.a-size-base-plus.a-color-base.a-text-normal"),
        result.select_one("span.a-size-base.a-color-base"),
        result.select_one("h2 span"),
    ]

    for candidate in candidates:
        if candidate:
            title = _sanitize_title(candidate.get_text(strip=True))
            if title:
                return title

    fallback = result.select_one("h2 a")
    if fallback:
        title = _sanitize_title(fallback.get_text(strip=True))
        if title:
            return title

    return None


def _is_discount_text(text: str) -> bool:
    if not text:
        return False

    lowered = text.strip().lower()
    ignore_patterns = [
        r"recycled",
        r"contains.*recycled",
        r"%\s*recycled",
        r"lowest price in \d+ days",
        r"in \d+ days",
        r"made of",
        r"eco",
        r"sustainable",
        r"organic",
        r"renewable",
        r"amazon's choice",
        r"best seller",
        r"count",
        r"feet",
        r"inch",
        r"oz",
        r"safer chemicals",
        r"free delivery",
        r"free shipping",
        r"Amazon Store Card",
        r"forestry practices",
    ]
    if any(re.search(pattern, lowered) for pattern in ignore_patterns):
        return False

    if re.search(r"\b(select from \d+ plans|per month|per week|per year|upon approval|amazon store card|no annual fee|subscription|subscribe)\b", lowered):
        return False

    if re.search(r"\b(save|savings|discount|deal|coupon|off|was|now|sale|clearance)\b", lowered):
        return True
    if re.search(r"\d+(?:\.\d+)?\s*%", lowered) and re.search(r"\b(off|save|discount|deal)\b", lowered) and not re.search(r"recycled|in \d+ days|lowest price in", lowered):
        return True
    if re.search(r"\$\s*\d+", lowered) and re.search(r"\b(save|off|deal|now|was)\b", lowered):
        return True

    return False


def parse_amazon_search_results(html: str, query: str, max_items: int = 50) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    for result in soup.select("div[data-asin][data-component-type='s-search-result'], div[data-asin], div.s-result-item[data-asin], div[data-component-type='s-search-result']"):
        asin = result.get("data-asin", "").strip()
        if not asin:
            continue

        title_tag = _extract_title(result)
        price_data = _extract_price_data(result)
        current_price = price_data["current_price"]
        original_price = price_data["original_price"]
        discount_label = price_data["discount_label"]
        current_num = price_data["current_num"]
        original_num = price_data["original_num"]
        image_url = _extract_image_url(result)
        link_tag = result.select_one("h2 a.a-link-normal") or result.select_one("h2 a") or result.select_one("a.a-link-normal")

        if not title_tag or not current_price or not link_tag:
            continue

        relative_link = link_tag["href"] if link_tag.has_attr("href") else ""
        product_url = (
            f"https://www.amazon.com{relative_link}"
            if relative_link.startswith("/")
            else relative_link
        )

        content_parts = [f"<strong>Current price:</strong> {current_price}"]
        if original_price and original_num is not None and current_num is not None and original_num > current_num:
            content_parts.append(f"<strong>Original price:</strong> {original_price}")
        if discount_label:
            content_parts.append(f"<strong>Discount info:</strong> {discount_label}")

        if not discount_label and not (original_num is not None and current_num is not None and original_num > current_num):
            continue

        results.append(
            {
                "id": len(results) + 1,
                "author": "Amazon Search",
                "title": title_tag,
                "current_price": current_price,
                "original_price": original_price if original_price and original_num is not None and current_num is not None and original_num > current_num else "",
                "discount_price": current_price if discount_label else "",
                "discount_label": discount_label,
                "content": "<br>".join(content_parts),
                "date_posted": date.today().strftime("%B %d, %Y"),
                "url": product_url,
                "image_url": image_url,
                "discounted": bool(discount_label),
            }
        )

        if len(results) >= max_items:
            break

    return results


posts: list[dict] = [
    {
        "id": 1,
        "title": "Welcome to the Discount Scraper!",
        "content": "This is a site that scrapes Amazon for discounted products based on your search queries. Use the search bar above to find the best deals!",
    },
    
]

# Empty the post lists by default
posts: list[dict] = []
@app.get("/", include_in_schema=False)
@app.get("/home", include_in_schema=False)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {"posts": [], "title": "Home", "query": None})

@app.get("/search", include_in_schema=False)
async def search(request: Request, q: str | None = None):
    if not q:
        return templates.TemplateResponse(request, "home.html", {"posts": posts, "title": "Home", "query": None})

    try:
        html = await fetch_amazon_search_page(q)
        search_results = parse_amazon_discounted_products(html, q)
        if not search_results:
            search_results = parse_amazon_search_results(html, q)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    search_message = ""

    if search_results:
        posts_to_render = search_results[:50]
        discounted_results = [result for result in search_results if result.get("discounted")]
        if discounted_results:
            search_message = f"Showing Amazon results for '{q}'. Remember, if you aren't finding the results you like, click the search button to refresh some of the results!"
    else:
        posts_to_render = [
            {
                "id": 1,
                "author": "AM Discount Finder",
                "title": "No products found",
                "content": f"Amazon returned no products for \"{q}\". Try a broader search term or check your ScrapingBee settings.",
                "date_posted": date.today().strftime("%B %d, %Y"),
                "url": "",
            }
        ]
        search_message = ""

    if not posts_to_render:
        posts_to_render = [
            {
                "id": 1,
                "author": "AM Discount Finder",
                "title": "No products found",
                "content": f"Amazon returned no products for \"{q}\". Try a broader search term or check your ScrapingBee settings.",
                "date_posted": date.today().strftime("%B %d, %Y"),
                "url": "",
            }
        ]
        search_message = ""

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "posts": posts_to_render,
            "title": f"Search results for {q}",
            "query": q,
            "search_message": search_message,
        },
    )

@app.get("/api/search")
async def api_search(q: str):
    html = await fetch_amazon_search_page(q)
    return parse_amazon_discounted_products(html, q, max_items=50)

@app.get("/posts/{post_id}", include_in_schema=False) ##specific posts page
def post_page(request: Request, post_id: int):
    for post in posts:
        if post["id"] == post_id:
            title = post["title"][:50]
            return templates.TemplateResponse(request, "post.html", {"post": post, "title": title})
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

@app.get("/api/posts") ##JSON format for all posts
def get_posts():
    return posts

@app.get("/api/posts/{post_id}") ##JSON response for a specific post
def get_post(post_id: int):
    for post in posts:
        if post["id"] == post_id:
            return post
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")


## StarletteHTTPException Handler
@app.exception_handler(StarletteHTTPException)
def general_http_exception_handler(request: Request, exception: StarletteHTTPException):
    message = (
        exception.detail
        if exception.detail
        else "An error occurred. Please check your request and try again."
    )

    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=exception.status_code,
            content={"detail": message},
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": exception.status_code,
            "title": exception.status_code,
            "message": message,
        },
        status_code=exception.status_code,
    )

### RequestValidationError Handler
@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exception: RequestValidationError):
    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": exception.errors()},
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": status.HTTP_422_UNPROCESSABLE_CONTENT,
            "title": status.HTTP_422_UNPROCESSABLE_CONTENT,
            "message": "Invalid request. Please check your input and try again.",
        },
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )