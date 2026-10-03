from __future__ import annotations

from urllib.parse import quote, quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from .types import Problem, Reference, SiteUnavailable


_CHALLENGE = ("captcha", "人机验证", "安全验证", "请输入验证码", "滑动验证")


class ReferenceSearch:
    def __init__(self, max_pages: int):
        self.max_pages = max_pages

    def search(self, problem: Problem) -> list[Reference]:
        if self.max_pages == 0:
            return []
        query = quote_plus(f"{problem.ref.slug} leetcode python solution")
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Agent4PS/1.0)"}
        try:
            with httpx.Client(headers=headers, follow_redirects=True, timeout=20) as client:
                response = client.get(f"https://www.bing.com/search?q={query}")
                response.raise_for_status()
                self._check_challenge(response.text)
                soup = BeautifulSoup(response.text, "html.parser")
                hits = soup.select("li.b_algo h2 a")
                results: list[Reference] = []
                pages_read = 0
                slug_words = [word for word in problem.ref.slug.split("-") if len(word) > 2]
                for hit in hits:
                    if pages_read >= self.max_pages:
                        break
                    url = hit.get("href", "")
                    if urlparse(url).scheme != "https":
                        continue
                    title = hit.get_text(" ", strip=True)
                    lowered = title.lower()
                    if not (problem.ref.title in title or all(word in lowered for word in slug_words)):
                        continue
                    pages_read += 1
                    try:
                        page = client.get(url)
                        page.raise_for_status()
                    except httpx.HTTPError:
                        continue
                    if urlparse(str(page.url)).hostname == "www.bing.com":
                        continue
                    text = BeautifulSoup(page.text, "html.parser").get_text("\n", strip=True)[:5000]
                    self._check_challenge(text)
                    results.append(Reference(title[:120], str(page.url), text))
                if pages_read < self.max_pages:
                    fallback = self._doocs(client, problem)
                    if fallback:
                        results.append(fallback)
                return results
        except httpx.HTTPError as exc:
            raise SiteUnavailable("web search is unavailable") from exc

    @staticmethod
    def _doocs(client: httpx.Client, problem: Problem) -> Reference | None:
        number = problem.ref.number
        start = (number - 1) // 100 * 100
        bucket = f"{start:04d}-{start + 99:04d}"
        api = f"https://api.github.com/repos/doocs/leetcode/contents/solution/{bucket}"
        try:
            listing = client.get(api, headers={"Accept": "application/vnd.github+json"})
            listing.raise_for_status()
            folders = listing.json()
            matches = [item for item in folders if item.get("type") == "dir" and item.get("name", "").startswith(f"{number:04d}.")]
            if not matches:
                return None
            folder = matches[0]["path"]
            for filename in ("README_EN.md", "README.md"):
                path = quote(f"{folder}/{filename}", safe="/")
                raw = client.get(f"https://raw.githubusercontent.com/doocs/leetcode/main/{path}")
                if raw.status_code == 404:
                    continue
                raw.raise_for_status()
                text = raw.text[:5000]
                if len(text) < 100:
                    return None
                return Reference(
                    f"doocs/leetcode: {matches[0]['name']}",
                    f"https://github.com/doocs/leetcode/blob/main/{path}",
                    text,
                )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise SiteUnavailable("public solution repository is unavailable") from exc
        return None

    @staticmethod
    def _check_challenge(text: str) -> None:
        if any(word in text.lower() for word in _CHALLENGE):
            raise SiteUnavailable("web search requested a verification challenge")
