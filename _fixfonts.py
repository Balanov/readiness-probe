import os
import re

site = r"F:\GitHub\readiness-probe\site"
css_path = os.path.join(site, "_theme.css")
fonts = os.path.join(site, "assets", "fonts")
names = os.listdir(fonts)


def pick(prefix, weight):
    for f in names:
        if f.startswith(prefix) and "-%d-" % weight in f:
            return f
    return None


lines = []
for w in (400, 500, 600, 700, 800):
    f = pick("manrope-latin", w)
    if f:
        lines.append(
            '@font-face{font-family:Manrope;font-style:normal;font-display:swap;'
            'font-weight:%d;src:url(./assets/fonts/%s) format("woff2")}' % (w, f)
        )
for w in (400, 500, 600):
    f = pick("jetbrains-mono-latin", w)
    if f:
        lines.append(
            '@font-face{font-family:"JetBrains Mono";font-style:normal;font-display:swap;'
            'font-weight:%d;src:url(./assets/fonts/%s) format("woff2")}' % (w, f)
        )
block = "\n".join(lines) + "\n"

src = open(css_path, encoding="utf-8").read()
start = src.index("@font-face")
end = src.index("/* READINESS PROBE")
src = src[:start] + block + "\n" + src[end:]
open(css_path, "w", encoding="utf-8").write(src)
print(block)

urls = re.findall(r"url\(\./assets/fonts/([^\)]+)\)", src)
missing = [u for u in urls if not os.path.exists(os.path.join(fonts, u))]
print("declared:", len(urls), "| missing:", missing if missing else "none")
