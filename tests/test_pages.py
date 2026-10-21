"""The browser pages: every script must at least parse (a syntax error blanks the whole page)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "studio" / "static"
NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="needs Node.js to parse the page scripts")
@pytest.mark.parametrize("page", ["app.html", "admin.html", "guest.html", "client.html", "_theme.html", "_theme_toggle.html"])
def test_page_script_parses(page, tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", (STATIC / page).read_text(encoding="utf-8"), re.S)
    assert scripts
    for n, js in enumerate(scripts):
        f = tmp_path / f"{page}.{n}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not NODE, reason="needs Node.js")
def test_pin_field_pattern_survives_the_template_string(tmp_path):
    """The PIN form is built in a JS template string, where a lone \\d would turn into a plain d."""
    s = (STATIC / "app.html").read_text(encoding="utf-8")
    line = next(ln for ln in s.splitlines() if 'name="pin" inputmode' in ln)
    src = line[line.index("<input"):line.index(">", line.index("<input")) + 1]
    f = tmp_path / "t.js"
    f.write_text("console.log(`" + src + "`)", encoding="utf-8")
    out = subprocess.run([NODE, str(f)], capture_output=True, text=True).stdout
    assert 'pattern="\\d{4,8}"' in out, out   # what the browser receives: \d (digits)
