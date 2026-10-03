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

