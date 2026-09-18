"""Fold an exported snapshot into one self-contained .html file.

`searchiq export-site` writes a folder: a page, its stylesheet, its script and a
`data/` directory the page fetches at runtime. That is the right shape for a
static host, and the wrong shape for anything you want to email, open straight
off a USB stick, or publish somewhere relative paths are not guaranteed to
resolve.

This inlines all of it into a single file. The front-end is untouched: rather
than patch `app.js` to read from a variable, the page installs a `fetch` shim
that answers `data/*.json` from an object baked into it, so the same code runs
against the same JSON it always did.

    python build_standalone.py site search-pulse-standalone.html
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKSLASH = chr(92)

# A closing tag inside a JavaScript or JSON string still ends the block: the
# HTML parser reaches it before the script ever runs. Escaping the slash keeps
# the string's value identical while hiding the tag from the parser.
ESCAPED_SLASH = "<" + BACKSLASH + "/"

SHIM = """
window.SEARCHPULSE_INLINE = __PAYLOAD__;
/* Answer the page's own data requests from the object above. Anything else
   falls through to the real fetch, so nothing else changes behaviour. */
(function (realFetch) {
  var pattern = /(?:^|[/])data[/]([A-Za-z0-9._-]+)[.]json$/;
  window.fetch = function (input) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var hit = pattern.exec(url);
    if (hit && Object.prototype.hasOwnProperty.call(window.SEARCHPULSE_INLINE, hit[1])) {
      return Promise.resolve(new Response(
        JSON.stringify(window.SEARCHPULSE_INLINE[hit[1]]),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      ));
    }
    return realFetch.apply(window, arguments);
  };
}(window.fetch.bind(window)));

/* "Export approved rules" is an <a href>, not a fetch, so the shim above never
   sees it. The folder export points it at data/rules-export.json; in one file
   that path leads nowhere, so hand the same JSON over as a blob instead. This
   runs after the app's own static-mode pass, which sets that href. */
window.addEventListener('load', function () {
  setTimeout(function () {
    var link = document.getElementById('export-rules');
    if (!link || !window.SEARCHPULSE_INLINE['rules-export']) return;
    var blob = new Blob([JSON.stringify(window.SEARCHPULSE_INLINE['rules-export'], null, 2)],
                        { type: 'application/json' });
    link.href = URL.createObjectURL(blob);
    link.download = 'rules-export.json';
  }, 0);
});
"""


def _safe(text: str) -> str:
    """Hide closing tags from the HTML parser without changing string values."""
    return text.replace("</script", ESCAPED_SLASH + "script").replace(
        "</style", ESCAPED_SLASH + "style"
    )


def build(site: Path, output: Path) -> Path:
    html = (site / "index.html").read_text(encoding="utf-8")
    css = (site / "static" / "styles.css").read_text(encoding="utf-8")
    app = (site / "static" / "app.js").read_text(encoding="utf-8")
    config = (site / "static" / "config.js").read_text(encoding="utf-8")

    data = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((site / "data").glob("*.json"))
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace(
        "</", ESCAPED_SLASH
    )
    shim = config + SHIM.replace("__PAYLOAD__", payload)

    replacements = {
        '<link rel="stylesheet" href="static/styles.css">': f"<style>\n{_safe(css)}\n</style>",
        '<link rel="stylesheet" href="/static/styles.css">': f"<style>\n{_safe(css)}\n</style>",
        '<script src="static/config.js"></script>': f"<script>\n{_safe(shim)}\n</script>",
        '<script src="/static/config.js"></script>': f"<script>\n{_safe(shim)}\n</script>",
        '<script src="static/app.js"></script>': f"<script>\n{_safe(app)}\n</script>",
        '<script src="/static/app.js"></script>': f"<script>\n{_safe(app)}\n</script>",
    }

    for tag, inlined in replacements.items():
        html = html.replace(tag, inlined)

    for leftover in ("static/styles.css", "static/app.js", "static/config.js"):
        if leftover in html:
            raise SystemExit(f"error: {leftover} is still referenced; nothing was inlined for it")

    output.write_text(html, encoding="utf-8")
    return output


def main(argv: list[str]) -> int:
    site = Path(argv[1] if len(argv) > 1 else "site")
    output = Path(argv[2] if len(argv) > 2 else "search-pulse-standalone.html")
    if not (site / "index.html").is_file():
        print(f"error: {site} is not an exported snapshot (no index.html)", file=sys.stderr)
        return 2
    built = build(site, output)
    print(f"{built}  {built.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
