from leetcode_agent.catalog import ProblemCatalog
from leetcode_agent.types import ProblemRef


def test_find_does_not_skip_number_when_catalog_pages_are_out_of_order(monkeypatch):
    catalog = ProblemCatalog("https://leetcode.cn")
    pages = [
        {100: ProblemRef(100, "Hundred", "hundred", "https://leetcode.cn/problems/hundred/")},
        {8: ProblemRef(8, "Eight", "eight", "https://leetcode.cn/problems/eight/")},
    ]

    def load_page():
        catalog.refs.update(pages.pop(0))
        catalog.exhausted = not pages

    monkeypatch.setattr(catalog, "_load_page", load_page)
    assert catalog.find(8).slug == "eight"
    assert catalog.find(7) is None
    assert not pages


def test_sorted_catalog_stops_after_passing_missing_number(monkeypatch):
    catalog = ProblemCatalog("https://leetcode.cn")
    pages = [
        [(1, "one"), (2, "two")],
        [(4, "four"), (5, "five")],
        [(6, "six")],
    ]
    requests = []

    class Response:
        def __init__(self, questions):
            self.questions = questions

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"problemsetQuestionList": {
                "hasMore": True,
                "questions": [{"frontendQuestionId": str(number), "titleSlug": slug}
                              for number, slug in self.questions],
            }}}

    def post(url, json, headers, timeout):
        requests.append(json["variables"])
        return Response(pages.pop(0))

    monkeypatch.setattr("leetcode_agent.catalog.httpx.post", post)
    assert catalog.find(3) is None
    assert len(requests) == 2
    assert requests[0]["filters"] == {"orderBy": "FRONTEND_ID", "sortOrder": "ASCENDING"}
    assert catalog.find(4).slug == "four"
    assert len(requests) == 2
    assert len(pages) == 1


def test_catalog_does_not_assume_order_if_first_page_is_unexpected(monkeypatch):
    catalog = ProblemCatalog("https://leetcode.cn")
    pages = [[(100, "hundred")], [(8, "eight")]]

    class Response:
        def __init__(self, questions):
            self.questions = questions

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"problemsetQuestionList": {
                "hasMore": bool(pages),
                "questions": [{"frontendQuestionId": str(number), "titleSlug": slug}
                              for number, slug in self.questions],
            }}}

    monkeypatch.setattr("leetcode_agent.catalog.httpx.post",
                        lambda *args, **kwargs: Response(pages.pop(0)))
    assert catalog.find(8).slug == "eight"
    assert catalog.ordered is False

