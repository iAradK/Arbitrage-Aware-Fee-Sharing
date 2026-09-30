#!/usr/bin/env bash
# Compile main-sigmetrics.tex in a clean build directory and report errors, undefined references and
# citations, the page count and the body length (everything before the bibliography).
# Run from Windows:  wsl -e bash paper/build.sh   (TeX Live is installed in WSL)
# Usage: paper/build.sh [TEXFILE] [BUILD_DIR]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEX="${1:-main-sigmetrics.tex}"
BUILD="${2:-$REPO/build}"
NAME="$(basename "$TEX" .tex)"
mkdir -p "$BUILD/figures"
cp "$REPO/$TEX" "$BUILD/"
cp "$REPO"/paper/figures/*.pdf "$BUILD/figures/"
for f in references.bib hook_lifecycle.png; do
  [ -f "$REPO/$f" ] && cp "$REPO/$f" "$BUILD/" || echo "WARNING: $f missing in the repo root"
done
cd "$BUILD" || exit 1
# TeX Live 2023: amssymb clashes with acmart's newtx fonts (\Bbbk already defined). Patch the build copy only.
if ! grep -q 'let\\Bbbk\\relax' "$NAME.tex"; then
  sed -i '0,/^\\usepackage{amssymb}/s//\\let\\Bbbk\\relax\\usepackage{amssymb}/' "$NAME.tex"
  echo "note: build copy patched with \\let\\Bbbk\\relax before amssymb"
fi
# Body-end marker (build copy only): page and vertical position of the point just before the bibliography,
# plus the geometry needed to turn it into a fraction of the text block.
python3 - "$NAME.tex" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
bs = chr(92)
marker = (bs + "par" + bs + "makeatletter" + bs + "pdfsavepos" + bs + "write" + bs + "@auxout{" + bs + "string" + bs
          + "gdef" + bs + "string" + bs + "BODYEND{" + bs + "thepage;" + bs + "the" + bs + "pdflastypos;" + bs + "the"
          + bs + "textheight;" + bs + "the" + bs + "paperheight;" + bs + "the" + bs + "topmargin;" + bs + "the"
          + bs + "headheight;" + bs + "the" + bs + "headsep;" + bs + "the" + bs + "voffset}}" + bs + "makeatother\n")
key = bs + "bibliographystyle"
i = s.find(key)
if i < 0:
    print("note: no \\bibliographystyle, body-end marker not inserted")
else:
    j = s.rfind(bs + "clearpage", 0, i)
    at = j if j >= 0 and s[j:i].strip() == bs + "clearpage" else i
    s = s[:at] + marker + s[at:]
    open(p, "w", encoding="utf-8").write(s)
PY
latexmk -pdf -shell-escape -interaction=nonstopmode "$NAME.tex" > latexmk.out 2>&1
LOG="$NAME.log"
echo "== LaTeX errors:          $(grep -c '^! ' "$LOG")"
grep -A2 '^! ' "$LOG" | head -30
echo "== undefined references: $(grep -c 'Reference .* undefined' "$LOG")"
grep 'Reference .* undefined' "$LOG" | sort -u | head -20
echo "== undefined citations:  $(grep -c 'Citation .* undefined' "$LOG")"
grep 'Citation .* undefined' "$LOG" | sort -u | head -20
echo "== overfull hboxes:      $(grep -c 'Overfull \\hbox' "$LOG")"
python3 - "$NAME" <<'PY'
import re, subprocess, sys
name = sys.argv[1]
info = subprocess.run(["pdfinfo", name + ".pdf"], capture_output=True, text=True).stdout
m = re.search(r"Pages:\s+(\d+)", info)
print(f"== pages: {m.group(1) if m else '?'}")
aux = open(name + ".aux", encoding="latin-1").read()
m = re.search(r"BODYEND\{([^}]*)\}", aux)
if not m:
    print("== body-end marker not found")
    sys.exit()
page, y, th, ph, tm, hh, hs, vo = m.group(1).split(";")
pt = lambda d: float(d.replace("pt", ""))
y_pt = int(y) / 65536.0                       # \pdflastypos: sp from the bottom of the page
top = pt(ph) - (72.27 + pt(tm) + pt(hh) + pt(hs) + pt(vo))   # top of the text block, pt from the bottom
frac = min(max((top - y_pt) / pt(th), 0.0), 1.0)
body = int(page) - 1 + frac
print(f"== body length (before the bibliography): {body:.2f} pages (ends {100 * frac:.0f}% down page {page})")
PY
