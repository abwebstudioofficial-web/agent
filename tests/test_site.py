from __future__ import annotations

import datetime as dt
import json

import pytest

from research_agent.site import Page, SiteMap, SiteNavigator, build_site_prompt
from research_agent.web import EXAMPLE_SITE_MAP

PAGES = [
    Page("/shipments", "Shipments", "all shipments"),
    Page("/shipments/{id}", "Shipment details"),
    Page("/fleet", "Fleet"),
]


def test_find_matches_paths_templates_and_query_strings():
    site = SiteMap(PAGES)
    assert site.find("/fleet").title == "Fleet"
    assert site.find("/shipments/SH-1042").title == "Shipment details"
    assert site.find("/shipments?status=delayed&lane=TX-CA").title == "Shipments"
    assert site.find("/shipments/SH-1042?tab=docs").title == "Shipment details"


@pytest.mark.parametrize("path", [
    "/unknown", "/fleet/extra", "/shipments/a/b", "/shipments/", "fleet",
    "https://evil.example/fleet", "//evil.example/fleet", "/fleet?x=<script>", "/fleet?x=\"",
    "/shipments/a b",
])
def test_find_rejects_other_paths(path):
    assert SiteMap(PAGES).find(path) is None


def test_site_map_validation():
    with pytest.raises(ValueError):
        SiteMap([])
    with pytest.raises(ValueError):
        SiteMap([Page("https://x.example", "External")])
    with pytest.raises(ValueError):
        SiteMap([Page("//x.example", "Protocol-relative")])


def test_navigator_records_destination_once():
    nav = SiteNavigator(SiteMap(PAGES))

    content, is_error = nav.run_tool("navigate_to", {"path": "/shipments/SH-7", "go_now": True})

    assert not is_error and "taken to Shipment details" in content
    dest = nav.take_destination()
    assert dest.to_dict() == {"path": "/shipments/SH-7", "title": "Shipment details", "go_now": True}
    assert nav.take_destination() is None


def test_navigator_button_mode_and_errors():
    nav = SiteNavigator(SiteMap(PAGES))

    content, is_error = nav.run_tool("navigate_to", {"path": "/fleet", "go_now": False})
    assert not is_error and "button" in content

    for bad in [{"path": "/nope", "go_now": False}, {"path": "/fleet"}, {"path": 1, "go_now": True}, "x"]:
        _, is_error = nav.run_tool("navigate_to", bad)
        assert is_error, bad
    assert nav.run_tool("other", {})[1] is True
    # A failed call does not replace an earlier good one.
    assert nav.take_destination().path == "/fleet"


def test_site_prompt_lists_pages():
    prompt = build_site_prompt("Logistix", SiteMap(PAGES), today=dt.date(2026, 9, 26))

    assert "built into Logistix" in prompt
    assert "- /shipments — Shipments: all shipments" in prompt
    assert "- /shipments/{id} — Shipment details" in prompt
    assert "{placeholders}" in prompt
    assert prompt.endswith("Today's date is 2026-09-26.")


def test_example_site_map_loads():
    site = SiteMap.from_json(EXAMPLE_SITE_MAP)
    assert site.find("/shipments/SH-1042").title == "Shipment details"
    assert len(site.pages) == len(json.loads(EXAMPLE_SITE_MAP.read_text()))
