from __future__ import annotations

import httpx

from .types import ProblemRef, SiteUnavailable


_QUERY = """
query AgentQuestionList($skip: Int, $limit: Int, $filters: QuestionListFilterInput) {
  problemsetQuestionList(categorySlug: "algorithms", skip: $skip, limit: $limit, filters: $filters) {
    total
    hasMore
    questions { frontendQuestionId titleCn titleSlug paidOnly }
  }
}
"""


class ProblemCatalog:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.refs: dict[int, ProblemRef] = {}
        self.paid: set[int] = set()
        self.offset = 0
        self.highest = 0
        self.ordered: bool | None = None
        self.exhausted = False

    def find(self, number: int) -> ProblemRef | None:
        while (number not in self.refs and not self.exhausted
               and (self.ordered is not True or self.highest < number)):
            self._load_page()
        return self.refs.get(number)

    def is_paid(self, number: int) -> bool:
        return number in self.paid

    def _load_page(self) -> None:
        try:
            response = httpx.post(
                f"{self.base_url}/graphql/",
                json={"query": _QUERY, "operationName": "AgentQuestionList", "variables": {
                    "skip": self.offset, "limit": 100,
                    "filters": {"orderBy": "FRONTEND_ID", "sortOrder": "ASCENDING"},
                }},
                headers={"User-Agent": "Agent4PS/1.0", "Referer": f"{self.base_url}/problemset/algorithms/"},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("errors"):
                raise ValueError("GraphQL returned errors")
            listing = data["data"]["problemsetQuestionList"]
            questions = listing["questions"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise SiteUnavailable("LeetCode algorithm catalog is unavailable") from exc
        if not isinstance(questions, list):
            raise SiteUnavailable("LeetCode algorithm catalog has an invalid response")
        previous_highest = self.highest
        numeric_ids = []
        for item in questions:
            raw_id = str(item.get("frontendQuestionId", ""))
            if not raw_id.isdecimal():
                continue
            number = int(raw_id)
            numeric_ids.append(number)
            slug = item.get("titleSlug")
            if not isinstance(slug, str) or not slug:
                continue
            self.refs[number] = ProblemRef(
                number, item.get("titleCn") or slug, slug,
                f"{self.base_url}/problems/{slug}/",
            )
            self.highest = max(self.highest, number)
            if item.get("paidOnly"):
                self.paid.add(number)
        if self.ordered is None:
            self.ordered = bool(numeric_ids) and numeric_ids[0] == 1 and numeric_ids == sorted(numeric_ids)
        elif self.ordered and numeric_ids:
            self.ordered = numeric_ids[0] > previous_highest and numeric_ids == sorted(numeric_ids)
        self.offset += len(questions)
        self.exhausted = not listing.get("hasMore") or not questions
        if self.exhausted and self.highest < 1:
            raise SiteUnavailable("LeetCode algorithm catalog is empty")

