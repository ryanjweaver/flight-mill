"""Limited source regression protection; actual native popup pixels need visual QA.

The small parser covers the shared option palette, not the complete CSS cascade.
No browser engine or native selected/hovered color behavior is inferred here.
"""
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

UI = Path(__file__).resolve().parents[1] / "src/flightmill/ui"


def test_enabled_options_have_a_paired_readable_palette():
    css = (UI / "static/css/app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    variables = {}
    palette = {}
    for selectors, body in rules:
        declarations = dict(part.strip().split(":", 1) for part in body.split(";")
                            if ":" in part)
        declarations = {k.strip(): v.strip() for k, v in declarations.items()}
        selectors = [s.strip() for s in selectors.split(",")]
        if ":root" in selectors:
            variables.update(declarations)
        if "select option" in selectors:
            palette.update(declarations)
    assert "color" in palette and "background-color" in palette, (
        "Native options need an explicit paired foreground/background independent of hover"
    )

    def luminance(value):
        if value.startswith("var("):
            value = variables[value[4:-1]]
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), "Use an opaque measurable palette"
        rgb = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                  for v in rgb]
        return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722)))

    a, b = sorted([luminance(palette["color"]), luminance(palette["background-color"])])
    assert (b + 0.05) / (a + 0.05) >= 4.5
    assert not re.search(r"forced-color-adjust\s*:\s*none", css)
    assert not re.search(r"option\s*:(hover|checked)", css), "Keep native highlight handling"


class SelectContracts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.selects = []
        self.current = None
        self.option = None
        self.labels = []
        self.label_stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "label":
            self.label_stack.append(attrs)
            self.labels.append(attrs)
        if tag == "select":
            self.current = {"attrs": attrs, "options": [], "wrapped_label": bool(self.label_stack)}
            self.selects.append(self.current)
        if tag == "option":
            self.option = [attrs, ""]
            self.current["options"].append(self.option)

    def handle_data(self, data):
        if self.option is not None:
            self.option[1] += data

    def handle_endtag(self, tag):
        if tag == "option":
            self.option = None
        if tag == "select":
            self.current = None
        if tag == "label":
            self.label_stack.pop()


@pytest.mark.parametrize("index,attrs,options,selected", [
    (0, {"id": "source-select"}, [("serial", "USB serial device"), ("simulation", "Simulation")],
     "serial"),
    (1, {"name": "interval_mode"}, [("default_150ms", "150 ms · default"),
                                    ("legacy_50ms", "50 ms · explicit legacy mode")], None),
    (2, {"id": "chart-window"}, [("60", "Last 60 sec"), ("300", "Last 5 min"),
                                 ("0", "Retained history")], None),
])
def test_native_select_contracts(index, attrs, options, selected):
    parser = SelectContracts()
    parser.feed((UI / "templates/index.html").read_text(encoding="utf-8"))
    assert len(parser.selects) == 3
    select = parser.selects[index]
    assert select["attrs"] == attrs
    # The approved acquisition default is USB serial, explicitly selected in HTML.
    assert select["options"] == [
        [{"value": value, **({"selected": None} if value == selected else {})}, label]
        for value, label in options
    ]
    assert select["wrapped_label"] or any(
        label.get("for") == attrs.get("id") for label in parser.labels
    )
