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
# A whole string that is nothing but a Rich style built from a named colour.
NAMED_STYLE = re.compile(
    r"(?:(?:bold|dim|italic|underline|reverse|blink|strike)\s+)*"
    r"(?:red|green|yellow|blue|cyan|magenta|white|black|dim)"
    r"(?:\s+on\s+\w+)?", re.IGNORECASE)


def _relative_luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(fg: str, bg: str) -> float:
    a, b = _relative_luminance(fg), _relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


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

    def test_no_named_rich_colours(self, src_path):
        """Named colours render differently per terminal palette.

        Walks the AST rather than grepping for `style="..."`: the first
        version of this test only matched that one spelling and missed 31
        literals hiding in return values, dict values, default arguments and
        conditional expressions.
        """
        import ast
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if NAMED_STYLE.fullmatch(node.value.strip()):
                offenders.append(f"line {node.lineno}: {node.value!r}")
        assert not offenders, (
            "named colour used as a style; use a palette constant:\n  "
            + "\n  ".join(offenders))

    def test_structural_tokens_are_never_text(self, after_palette):
        """Surface and border tokens must not be used to draw text.

        Regression: section headings were styled `bold {C.LINE}`, which is
        the hairline colour — 1.32:1 against the panel, i.e. invisible.
        """
        offenders = []
        for token in ("LINE", "LINE_STRONG", "ELEVATED", "SURFACE",
                      "SURFACE_ALT", "PANEL", "BG"):
            for pattern in (f'bold {{C.{token}}}', f'style=C.{token})',
                            f'"{{C.{token}}}"'):
                if pattern in after_palette:
                    offenders.append(pattern)
        assert not offenders, f"structural token used as text colour: {offenders}"

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


class TestContrast:
    """Colours that carry text must actually be readable on our surfaces."""

    SURFACES = ("bg", "surface", "surface_alt", "panel")
    # WCAG AA is 4.5:1 for body text and 3:1 for large or secondary text.
    TEXT_TOKENS = {"fg": 4.5, "fg_muted": 4.5, "fg_faint": 3.0}
    STATE_TOKENS = {"ok": 3.0, "info": 3.0, "warn": 3.0, "err": 3.0,
                    "violet": 3.0, "amber": 3.0, "primary": 3.0,
                    "primary_soft": 3.0}

    @pytest.mark.parametrize("token,minimum", sorted(TEXT_TOKENS.items()))
    def test_text_tokens_are_readable(self, sd, token, minimum):
        for surface in self.SURFACES:
            ratio = contrast_ratio(sd.PALETTE[token], sd.PALETTE[surface])
            assert ratio >= minimum, (
                f"{token} on {surface} is {ratio:.2f}:1, needs {minimum}:1")

    @pytest.mark.parametrize("token,minimum", sorted(STATE_TOKENS.items()))
    def test_state_tokens_are_readable(self, sd, token, minimum):
        for surface in self.SURFACES:
            ratio = contrast_ratio(sd.PALETTE[token], sd.PALETTE[surface])
            assert ratio >= minimum, (
                f"{token} on {surface} is {ratio:.2f}:1, needs {minimum}:1")

    def test_structural_tokens_are_too_low_contrast_for_text(self, sd):
        """Documents why they are banned as text colours above."""
        for token in ("line", "elevated"):
            ratio = contrast_ratio(sd.PALETTE[token], sd.PALETTE["surface"])
            assert ratio < 3.0, (
                f"{token} now has {ratio:.2f}:1 — if it became readable, "
                "revisit test_structural_tokens_are_never_text")
