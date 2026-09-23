"""The theme is a system, not a collection of literals.

Every colour must come from PALETTE, every stylesheet variable must exist in
the registered theme, and nothing in the UI may use a double-width glyph,
which would break column alignment in the tables.
"""
import re
import unicodedata

import pytest

CSS_BLOCK = re.compile(r'(?:DEFAULT_)?CSS\s*=\s*"""(.*?)"""', re.S)
HEX = re.compile(r"#[0-9a-fA-F]{6}")


@pytest.fixture(scope="module")
def source(src_path):
    return src_path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def after_palette(source):
    """Source with the palette definition itself removed."""
    return source.partition("def build_theme")[2]


class TestPalette:
    def test_values_are_unique(self, sd):
        values = list(sd.PALETTE.values())
        assert len(values) == len(set(values)), "two tokens share a colour"

    def test_every_value_is_a_hex_colour(self, sd):
        for name, value in sd.PALETTE.items():
            assert HEX.fullmatch(value), f"{name}={value!r}"

    def test_constants_mirror_the_table(self, sd):
        for attr in dir(sd.C):
            if attr.startswith("_"):
                continue
            assert getattr(sd.C, attr) in sd.PALETTE.values(), attr

    def test_no_colour_outside_the_palette(self, sd, after_palette):
        """Catches a stray literal creeping back into the UI code."""
        allowed = {v.lower() for v in sd.PALETTE.values()}
        used = {h.lower() for h in HEX.findall(after_palette)}
        assert used <= allowed, f"not palette colours: {sorted(used - allowed)}"

    def test_no_named_rich_colours(self, after_palette):
        """Named colours render differently per terminal palette."""
        named = re.findall(r'style="(?:bold )?(?:red|green|yellow|blue|cyan|'
                           r'magenta|white|black|dim)"', after_palette)
        assert not named, named

    def test_no_named_markup_tags(self, after_palette):
        tags = re.findall(r'\[(?:bold )?(?:red|green|yellow|blue|cyan|magenta|'
                          r'white|black|dim)\]', after_palette)
        assert not tags, tags


class TestStylesheets:
    def test_stylesheets_contain_no_raw_colours(self, source):
        """Stylesheets reference tokens so the theme stays swappable."""
        stray = []
        for block in CSS_BLOCK.findall(source):
            stray += HEX.findall(block)
        assert not stray, f"raw colours in CSS: {sorted(set(stray))}"

    def test_every_variable_used_is_defined(self, sd, source):
        defined = set(sd.build_theme().variables)
        used = set()
        for block in CSS_BLOCK.findall(source):
            used |= set(re.findall(r"\$(sq-[a-z0-9-]+)", block))
        missing = used - defined
        assert not missing, f"undefined theme variables: {sorted(missing)}"

    def test_no_variable_is_dead_weight(self, sd, source):
        """A token nothing uses is a token that will drift out of date."""
        used = set()
        for block in CSS_BLOCK.findall(source):
            used |= set(re.findall(r"\$(sq-[a-z0-9-]+)", block))
        # constants are used from Python rather than CSS
        python_side = {"sq-surface-alt", "sq-info", "sq-amber",
                       "sq-primary-soft", "sq-fg-dim"}
        unused = set(sd.build_theme().variables) - used - python_side
        assert not unused, f"unused theme variables: {sorted(unused)}"


class TestTheme:
    def test_registers_and_activates(self, sd):
        theme = sd.build_theme()
        assert theme.name == "sqdash"
        assert theme.dark is True

    def test_semantic_roles_map_to_the_palette(self, sd):
        theme = sd.build_theme()
        assert theme.primary == sd.PALETTE["primary"]
        assert theme.success == sd.PALETTE["ok"]
        assert theme.warning == sd.PALETTE["warn"]
        assert theme.error == sd.PALETTE["err"]
        assert theme.background == sd.PALETTE["bg"]
        assert theme.surface == sd.PALETTE["surface"]


class TestStateColours:
    @pytest.mark.parametrize("state", [
        "RUNNING", "R", "PENDING", "PD", "COMPLETED", "CD", "FAILED", "F",
        "CANCELLED", "CA", "TIMEOUT", "TO", "OUT_OF_MEMORY", "NODE_FAIL",
        "PREEMPTED", "SUSPENDED", "SOMETHING_NEW",
    ])
    def test_every_state_style_uses_the_palette(self, sd, state):
        colour = sd.state_style(state).replace("bold ", "").strip()
        assert colour in sd.PALETTE.values(), f"{state} -> {colour}"

    @pytest.mark.parametrize("pct", [0, 50, 69, 70, 89, 90, 100])
    def test_threshold_bars_use_the_palette(self, sd, pct):
        colour = sd.bar_color(pct).replace("bold ", "").strip()
        assert colour in sd.PALETTE.values()

    def test_thresholds_escalate(self, sd):
        assert sd.bar_color(50) != sd.bar_color(75) != sd.bar_color(95)


class TestGlyphs:
    def test_no_double_width_characters(self, source):
        """Emoji are two cells wide and misalign every column after them."""
        wide = sorted({
            ch for ch in set(source)
            if ord(ch) > 0x2000
            and unicodedata.east_asian_width(ch) in ("W", "F")
        })
        assert not wide, f"double-width glyphs: {[(c, hex(ord(c))) for c in wide]}"
