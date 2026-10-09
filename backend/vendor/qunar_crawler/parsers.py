from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import City, PoiRecord, PoiSummary
from .utils import china_now, clean_text, json_text, stable_id


BASE_URL = "https://sight.qunar.com"
_POI_HREF_RE = re.compile(r"^/(\d+)$")
_USER_ID_RE = re.compile(r"headshotsById/(\d+)", re.I)
_BG_URL_RE = re.compile(r"url\((['\"]?)(.*?)\1\)", re.I)
_LEVEL_RE = re.compile(r"([1-5]A)", re.I)
_META_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})(?:\s*\|\s*(?P<location>.*))?")


@dataclass(frozen=True)
class PoiListPage:
    city_name: str
    items: list[PoiSummary]
    total_pages: int


@dataclass(frozen=True)
class CommentPage:
    scenic_name: str
    comments: list[dict[str, Any]]
    total_pages: int


def _json_ld(soup: BeautifulSoup) -> list[Any]:
    values: list[Any] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            values.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return values


def _find_json_ld_type(soup: BeautifulSoup, expected: str) -> dict[str, Any] | None:
    for value in _json_ld(soup):
        if isinstance(value, dict):
            kind = value.get("@type")
            if kind == expected or (isinstance(kind, list) and expected in kind):
                return value
    return None


def parse_city_index(html: str, base_url: str = BASE_URL) -> list[City]:
    soup = BeautifulSoup(html, "html.parser")
    cities: dict[str, City] = {}
    for anchor in soup.select('a[href^="/city/"]'):
        href = clean_text(anchor.get("href"))
        match = re.fullmatch(r"/city/([a-z0-9_-]+)", href, flags=re.I)
        if not match:
            continue
        city_id = clean_text(anchor.get("data-ce-t")) or match.group(1)
        city_name = clean_text(anchor.get_text(" ", strip=True))
        city_name = re.sub(r"景点$", "", city_name).strip()
        if city_id and city_name:
            cities.setdefault(city_id, City(city_id, city_name, urljoin(base_url, href)))
    if cities:
        return list(cities.values())

    item_list = _find_json_ld_type(soup, "ItemList") or {}
    for element in item_list.get("itemListElement", []):
        if not isinstance(element, dict):
            continue
        url = clean_text(element.get("url"))
        match = re.search(r"/city/([a-z0-9_-]+)$", url, flags=re.I)
        name = re.sub(r"景点$", "", clean_text(element.get("name"))).strip()
        if match and name:
            city_id = match.group(1)
            cities.setdefault(city_id, City(city_id, name, url))
    return list(cities.values())


def _max_page_from_links(soup: BeautifulSoup, parameter: str) -> int:
    pages = [1]
    for anchor in soup.select(f'a[href*="{parameter}="]'):
        query = parse_qs(urlparse(anchor.get("href", "")).query)
        for value in query.get(parameter, []):
            if str(value).isdigit():
                pages.append(int(value))
    return max(pages)


def parse_poi_list(html: str) -> PoiListPage:
    soup = BeautifulSoup(html, "html.parser")
    city_name = ""
    heading = soup.find(["h1", "h2"], string=re.compile(r"景点(?:大全|列表)"))
    if heading:
        city_name = re.sub(r"景点.*$", "", clean_text(heading.get_text(" ", strip=True)))
    if not city_name:
        crumb = soup.select_one('.sg-crumb a[href^="/city/"]')
        if crumb:
            city_name = re.sub(r"景点$", "", clean_text(crumb.get_text(" ", strip=True)))

    items: list[PoiSummary] = []
    seen: set[str] = set()
    anchors = soup.select("a.sg-item[href]") or soup.select('a[href^="/"]')
    for anchor in anchors:
        href = clean_text(anchor.get("href"))
        match = _POI_HREF_RE.fullmatch(href)
        if not match:
            continue
        poi_id = match.group(1)
        if poi_id in seen:
            continue
        heading_node = anchor.find("h3")
        name = clean_text(heading_node.get_text(" ", strip=True) if heading_node else "")
        level_node = anchor.select_one(".lv, .sg-item-tag")
        level_match = _LEVEL_RE.search(clean_text(level_node.get_text(" ", strip=True) if level_node else ""))
        level = level_match.group(1).upper() if level_match else ""
        if level and name.endswith(level):
            name = name[: -len(level)].strip()
        if not name:
            name_node = anchor.select_one(".nm")
            name = clean_text(name_node.get_text(" ", strip=True) if name_node else "")
        address_node = anchor.select_one(".sg-item-key")
        address = clean_text(address_node.get_text(" ", strip=True) if address_node else "").lstrip("📍").strip()
        if name:
            seen.add(poi_id)
            items.append(PoiSummary(poi_id=poi_id, poi_name=name, address=address, scenic_level=level))
    return PoiListPage(city_name=city_name, items=items, total_pages=_max_page_from_links(soup, "page"))


def _extract_policy(soup: BeautifulSoup) -> str:
    entries: list[str] = []
    for row in soup.select("table.sg-policy-table tbody tr"):
        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
        if len(cells) >= 3:
            entries.append(f"人群：{cells[0]}｜使用条件：{cells[1]}｜优待政策：{cells[2]}")
        elif cells:
            entries.append("｜".join(cells))
    foot = soup.select_one("table.sg-policy-table tfoot")
    if foot:
        note = clean_text(foot.get_text(" ", strip=True))
        if note:
            entries.append(note if note.startswith("补充说明") else f"补充说明：{note}")
    if entries:
        return "；".join(entries)

    faq = _find_json_ld_type(soup, "FAQPage") or {}
    for entity in faq.get("mainEntity", []):
        if "优待政策" in clean_text(entity.get("name")):
            answer = entity.get("acceptedAnswer") or {}
            return clean_text(answer.get("text"), preserve_newlines=True)
    return ""


def _city_from_breadcrumb(soup: BeautifulSoup) -> tuple[str, str]:
    for anchor in soup.select('a[href^="/city/"]'):
        href = clean_text(anchor.get("href"))
        match = re.fullmatch(r"/city/([a-z0-9_-]+)", href, flags=re.I)
        if match:
            return match.group(1), re.sub(r"景点$", "", clean_text(anchor.get_text(" ", strip=True)))
    return "", ""


def parse_poi_detail(
    html: str,
    poi_id: str,
    *,
    city_id: str = "",
    city_name: str = "",
    scenic_level: str = "",
    fallback_name: str = "",
    fallback_address: str = "",
) -> PoiRecord:
    soup = BeautifulSoup(html, "html.parser")
    attraction = _find_json_ld_type(soup, "TouristAttraction") or {}
    breadcrumb_city_id, breadcrumb_city_name = _city_from_breadcrumb(soup)
    city_id = city_id or breadcrumb_city_id
    city_name = city_name or breadcrumb_city_name

    poi_name = clean_text(attraction.get("name")) or fallback_name
    if not poi_name:
        heading = soup.find("h1")
        poi_name = clean_text(heading.get_text(" ", strip=True) if heading else "")

    address_value = attraction.get("address") or {}
    if isinstance(address_value, dict):
        address = clean_text(address_value.get("streetAddress"))
        city_name = city_name or clean_text(address_value.get("addressLocality"))
    else:
        address = clean_text(address_value)
    address = address or fallback_address

    opening = attraction.get("openingHours") or []
    if isinstance(opening, str):
        opening = [opening]
    open_time = "；".join(clean_text(value, preserve_newlines=True).replace("\n", " ") for value in opening if clean_text(value))
    if not open_time:
        hour_rows = [clean_text(node.get_text(" ", strip=True)) for node in soup.select(".sg-hours .p")]
        open_time = "；".join(value for value in hour_rows if value)

    telephone = attraction.get("telephone") or []
    if isinstance(telephone, str):
        telephone = [telephone]
    tel = ";".join(dict.fromkeys(clean_text(value) for value in telephone if clean_text(value)))
    if not tel:
        values = [clean_text(anchor.get_text(" ", strip=True)) for anchor in soup.select(".sg-phones a[href^='tel:']")]
        tel = ";".join(dict.fromkeys(value for value in values if value))

    intro = clean_text(attraction.get("description"), preserve_newlines=True)
    if not intro:
        intro_node = soup.select_one(".sg-introduce-html")
        intro = clean_text(intro_node.get_text("\n", strip=True) if intro_node else "", preserve_newlines=True)

    amenities = [clean_text(node.get_text(" ", strip=True)) for node in soup.select(".sg-fac-grid-name")]
    amenity = "；".join(dict.fromkeys(value for value in amenities if value))

    return PoiRecord(
        city_id=city_id,
        city_name=city_name,
        poi_id=str(poi_id),
        poi_name=poi_name,
        address=address,
        open_time=open_time,
        tel=tel,
        scenic_intro=intro,
        discount_policy=_extract_policy(soup),
        amenity=amenity,
        scenic_level=scenic_level,
    )


def _next_f_text(soup: BeautifulSoup) -> str:
    chunks: list[str] = []
    prefix = "self.__next_f.push("
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        raw = raw.strip()
        if not raw.startswith(prefix) or not raw.endswith(")"):
            continue
        try:
            payload = json.loads(raw[len(prefix) : -1])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], str):
            chunks.append(payload[1])
    return "".join(chunks)


def _embedded_comments(soup: BeautifulSoup) -> list[dict[str, Any]]:
    text = _next_f_text(soup)
    marker = '"comment":'
    decoder = json.JSONDecoder()
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        index = text.find(marker, offset)
        if index < 0:
            break
        start = index + len(marker)
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            offset = start
            continue
        offset = end
        if not isinstance(value, dict):
            continue
        comment_id = clean_text(value.get("id"))
        if comment_id and comment_id not in seen:
            seen.add(comment_id)
            comments.append(value)
    return comments


def _style_url(node: Tag | None) -> str:
    if node is None:
        return ""
    match = _BG_URL_RE.search(clean_text(node.get("style")))
    if not match:
        return ""
    url = match.group(2)
    return f"https:{url}" if url.startswith("//") else url


def _fallback_dom_comments(soup: BeautifulSoup) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for item in soup.select(".sg-cm-item"):
        content_node = item.select_one(".sg-cm-text")
        if not content_node:
            continue
        name_node = item.select_one(".sg-cm-head .who b")
        meta_node = item.select_one(".sg-cm-head .meta")
        score_node = item.select_one(".sg-cm-pill")
        avatar = item.select_one(".sg-cm-head-avatar")
        meta = clean_text(meta_node.get_text(" ", strip=True) if meta_node else "")
        meta_match = _META_RE.search(meta)
        date = meta_match.group("date") if meta_match else ""
        location = clean_text(meta_match.group("location")) if meta_match and meta_match.group("location") else ""
        content = clean_text(content_node.get_text("\n", strip=True), preserve_newlines=True)
        user = clean_text(name_node.get_text(" ", strip=True) if name_node else "")
        score_match = re.search(r"\d+(?:\.\d+)?", clean_text(score_node.get_text(" ", strip=True) if score_node else ""))
        images = [{"big": _style_url(node)} for node in item.select(".sg-cm-img") if _style_url(node)]
        head_img = _style_url(avatar)
        comment_id = stable_id(user, date, content, prefix="generated-")
        ticket_node = item.select_one(".sg-cm-ticket")
        package_node = item.select_one(".sg-cm-pkg")
        comments.append(
            {
                "id": comment_id,
                "user": user,
                "date": date,
                "score": float(score_match.group()) if score_match else None,
                "text": content,
                "from": location,
                "headImg": head_img,
                "ticketName": clean_text(ticket_node.get_text(" ", strip=True) if ticket_node else ""),
                "dayTripPackageName": clean_text(package_node.get_text(" ", strip=True) if package_node else ""),
                "imgs": images,
                "tags": [],
            }
        )
    return comments


def _scenic_name_from_comments(soup: BeautifulSoup) -> str:
    for value in _json_ld(soup):
        if not isinstance(value, dict) or value.get("@type") != "ItemList":
            continue
        elements = value.get("itemListElement") or []
        if elements:
            item = elements[0].get("item", {}) if isinstance(elements[0], dict) else {}
            reviewed = item.get("itemReviewed", {}) if isinstance(item, dict) else {}
            name = clean_text(reviewed.get("name")) if isinstance(reviewed, dict) else ""
            if name:
                return name
    title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    return re.sub(r"怎么样.*$", "", title).strip()


def _published_datetime(value: object) -> str:
    date = clean_text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return f"{date} 00:00:00"
    return date


def _normalize_undefined(value: object) -> object:
    if value == "$undefined":
        return None
    return value


def comment_to_row(comment: dict[str, Any], scenic_id: str, scenic_name: str, crawl_time: str) -> dict[str, Any]:
    comment_id = clean_text(comment.get("id")) or stable_id(
        scenic_id, comment.get("user"), comment.get("date"), comment.get("text"), prefix="generated-"
    )
    head_img = clean_text(comment.get("headImg"))
    user_match = _USER_ID_RE.search(head_img)
    commenter_id = user_match.group(1) if user_match else ""
    images: list[str] = []
    for image in comment.get("imgs") or []:
        if isinstance(image, dict):
            url = clean_text(image.get("big") or image.get("small"))
        else:
            url = clean_text(image)
        if url:
            images.append(urljoin(BASE_URL, url))

    score = _normalize_undefined(comment.get("score"))
    ticket_name = _normalize_undefined(comment.get("ticketName"))
    package_name = _normalize_undefined(comment.get("dayTripPackageName"))
    tags = _normalize_undefined(comment.get("tags")) or []
    extra = {
        "score": score,
        "ticket_name": ticket_name,
        "day_trip_package_name": package_name,
        "source_tags": tags,
        "source_url": f"{BASE_URL}/{scenic_id}/comment",
        "source_limitations": "likes, replies, and videos are not published on this page",
    }
    return {
        "scenic_id": scenic_id,
        "scenic_name": scenic_name,
        "channel": "qunaer",
        "work_id": scenic_id,
        "comment_level": "level_1",
        "comment_parent_id": "",
        "comment_id": comment_id,
        "commenter_id": commenter_id,
        "image_list": json_text(images),
        "video_list": "[]",
        "location": clean_text(comment.get("from")),
        "content": clean_text(comment.get("text"), preserve_newlines=True),
        "likes": 0,
        "extra_content": json_text(extra),
        "sentiment_label": "",
        "sentiment_score": "",
        "dimension_tags": "[]",
        "entity_tags": "[]",
        "keyword_tags": "[]",
        "label_review_flag": 0,
        "publish_time": _published_datetime(comment.get("date")),
        "crawl_time": crawl_time,
        "commenter_name": clean_text(comment.get("user")),
        "root_comment_id": comment_id,
        "sub_comment_count": 0,
    }


def parse_comment_page(html: str, scenic_id: str, *, scenic_name: str = "", crawl_time: str | None = None) -> CommentPage:
    soup = BeautifulSoup(html, "html.parser")
    scenic_name = scenic_name or _scenic_name_from_comments(soup)
    raw_comments = _embedded_comments(soup) or _fallback_dom_comments(soup)
    captured_at = crawl_time or china_now()
    rows = [comment_to_row(comment, str(scenic_id), scenic_name, captured_at) for comment in raw_comments]
    return CommentPage(scenic_name=scenic_name, comments=rows, total_pages=_max_page_from_links(soup, "pageNum"))


def flatten_regions(payload: Any, source_url: str) -> list[dict[str, str]]:
    if isinstance(payload, dict):
        payload = payload.get("response", {}).get("data", {}).get("children", payload.get("data", payload))
    if not isinstance(payload, list):
        raise ValueError("administrative division payload is not a list/tree")
    rows: list[dict[str, str]] = []
    for province in payload:
        if not isinstance(province, dict):
            continue
        province_code = clean_text(province.get("c") or province.get("code"))
        province_name = clean_text(province.get("n") or province.get("name"))
        cities = province.get("ch") or province.get("children") or []
        if not cities:
            rows.append(
                {
                    "province_code": province_code,
                    "province_name": province_name,
                    "city_code": "",
                    "city_name": "",
                    "district_code": "",
                    "district_name": "",
                    "source_url": source_url,
                }
            )
            continue
        for city in cities:
            city_code = clean_text(city.get("c") or city.get("code"))
            city_name = clean_text(city.get("n") or city.get("name"))
            districts = city.get("ch") or city.get("children") or []
            if not districts:
                districts = [{}]
            for district in districts:
                rows.append(
                    {
                        "province_code": province_code,
                        "province_name": province_name,
                        "city_code": city_code,
                        "city_name": city_name,
                        "district_code": clean_text(district.get("c") or district.get("code")),
                        "district_name": clean_text(district.get("n") or district.get("name")),
                        "source_url": source_url,
                    }
                )
    return rows


def normalize_region_name(name: str) -> str:
    value = clean_text(name)
    suffixes = (
        "特别行政区",
        "维吾尔自治区",
        "壮族自治区",
        "回族自治区",
        "藏族自治州",
        "蒙古族藏族自治州",
        "土家族苗族自治州",
        "布依族苗族自治州",
        "苗族侗族自治州",
        "哈萨克自治州",
        "傣族自治州",
        "傣族景颇族自治州",
        "彝族自治州",
        "自治州",
        "自治区",
        "地区",
        "市",
        "省",
        "盟",
    )
    for suffix in suffixes:
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


def attach_qunar_cities(rows: Iterable[dict[str, str]], cities: Iterable[City]) -> list[dict[str, str]]:
    exact: dict[str, City] = {}
    normalized: dict[str, list[City]] = {}
    for city in cities:
        exact.setdefault(city.city_name, city)
        normalized.setdefault(normalize_region_name(city.city_name), []).append(city)

    enriched: list[dict[str, str]] = []
    for row in rows:
        candidates = [row.get("district_name", ""), row.get("city_name", "")]
        match: City | None = None
        for name in candidates:
            if name in exact:
                match = exact[name]
                break
            values = normalized.get(normalize_region_name(name), [])
            if len(values) == 1:
                match = values[0]
                break
        result = dict(row)
        result.update(
            {
                "qunar_city_id": match.city_id if match else "",
                "qunar_city_name": match.city_name if match else "",
                "qunar_city_url": match.city_url if match else "",
            }
        )
        enriched.append(result)
    return enriched
