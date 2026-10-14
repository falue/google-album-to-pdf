#!/usr/bin/env python3
"""Turn a public Google Photos album into a clean PDF."""
import argparse
import io
import itertools
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fpdf import FPDF

RPC_URL = "https://photos.google.com/_/PhotosUi/data/batchexecute"
ORDER_KEY = "101428965"  # position of photos and text in the album
TEXT_KEY = "99218341"  # text of an in-between title


@dataclass
class Photo:
    url: str
    width: int
    height: int

    @property
    def aspect(self):
        return self.width / self.height


@dataclass
class Section:
    title: str | None
    photos: list[Photo] = field(default_factory=list)


def fetch_album(url):
    """Return (album title, sections) of a public album link."""
    with urllib.request.urlopen(url) as resp:  # follows photos.app.goo.gl redirect
        share = urllib.parse.urlparse(resp.geturl())
    album_id = share.path.rstrip("/").split("/")[-1]
    key = urllib.parse.parse_qs(share.query)["key"][0]

    entries, texts, token = [], {}, None
    while True:
        req = json.dumps([[["snAcKc", json.dumps([album_id, token, None, key]), None, "generic"]]])
        body = urllib.parse.urlencode({"f.req": req}).encode()
        with urllib.request.urlopen(RPC_URL, body) as resp:
            line = next(l for l in resp.read().decode().splitlines() if l.startswith('[["wrb.fr"'))
        data = json.loads(json.loads(line)[0][2])
        title = data[3][1]
        for item in data[1]:  # videos included: their url serves the thumbnail
            url, w, h = item[1][:3]
            entries.append((item[-1].get(ORDER_KEY, [0, ""])[1], Photo(url, w, h)))
        for text_id, meta in data[4] or []:
            if TEXT_KEY in meta:
                texts[text_id] = (meta[ORDER_KEY][1], "".join(seg[1][0] for seg in meta[TEXT_KEY]))
        token = data[2]
        if not token:
            break

    sections = [Section(None)]
    for _, entry in sorted(entries + list(texts.values()), key=lambda e: e[0]):
        if isinstance(entry, str):
            sections.append(Section(entry))
        else:
            sections[-1].photos.append(entry)
    return title, [s for s in sections if s.title or s.photos]


def arrange(aspects, w, h, gap):
    """Best uncropped layout of photos inside a w*h box: (score, [(x, y, w, h)]).

    Score is the covered fraction of the box, lowered when photo sizes are uneven.
    """

    def rows(aspects, w, h, splits):
        groups = [aspects[a:b] for a, b in zip((0, *splits), (*splits, len(aspects)))]
        heights = [(w - gap * (len(g) - 1)) / sum(g) for g in groups]
        scale = min(1, (h - gap * (len(groups) - 1)) / sum(heights))
        heights = [rh * scale for rh in heights]
        y = (h - sum(heights) - gap * (len(groups) - 1)) / 2
        boxes = []
        for g, rh in zip(groups, heights):
            x = (w - sum(a * rh for a in g) - gap * (len(g) - 1)) / 2
            for a in g:
                boxes.append((x, y, a * rh, rh))
                x += a * rh + gap
            y += rh + gap
        return boxes

    best = (-1, [])
    n = len(aspects)
    for cut in itertools.product((False, True), repeat=n - 1):
        splits = [i + 1 for i, c in enumerate(cut) if c]
        by_rows = rows(aspects, w, h, splits)
        by_cols = [(y, x, bh, bw) for x, y, bw, bh in rows([1 / a for a in aspects], h, w, splits)]
        for boxes in (by_rows, by_cols):
            score = sum((bw * bh) ** 0.5 for _, _, bw, bh in boxes) ** 2 / (n * w * h)
            if score > best[0]:
                best = (score, boxes)
    return best


def paginate(aspects, w, first_h, h, gap, min_per_page=3, max_per_page=5):
    """Split photos into pages, maximizing layout scores; returns [(start, end, boxes)]."""
    n = len(aspects)
    cost = [0.0] + [float("inf")] * n
    choice = [None] * (n + 1)
    for end in range(1, n + 1):
        for start in range(max(0, end - max_per_page), end):
            score, boxes = arrange(aspects[start:end], w, first_h if start == 0 else h, gap)
            c = cost[start] + (1 - score) + (end - start < min_per_page)
            if c < cost[end]:
                cost[end], choice[end] = c, (start, boxes)
    pages, end = [], n
    while end:
        start, boxes = choice[end]
        pages.append((start, end, boxes))
        end = start
    return pages[::-1]


def download(url):
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def build_pdf(title, sections, out, page="A4", landscape=False, dpi=200, compression=10, font=None,
              margin=12, gap=4, title_size=32, heading_size=20):
    pdf = FPDF(orientation="L" if landscape else "P", unit="mm", format=page)
    pdf.set_auto_page_break(False)
    pdf.set_margins(margin, margin)
    if font:
        pdf.add_font("custom", fname=font)
    family = "custom" if font else "Helvetica"
    clean = (lambda s: s) if font else (lambda s: s.encode("latin-1", "replace").decode("latin-1"))
    content_w, content_h = pdf.w - 2 * margin, pdf.h - 2 * margin

    pdf.add_page()
    pdf.set_font(family, size=title_size)
    lines = len(pdf.multi_cell(content_w, text=clean(title), dry_run=True, output="LINES"))
    pdf.set_y((pdf.h - lines * pdf.font_size * 1.3) / 2)
    pdf.multi_cell(content_w, pdf.font_size * 1.3, clean(title), align="C")

    # Lay out every page first, so each photo is downloaded at the size it is printed.
    pages = []  # (heading or None, [(photo, x, y, w, h)])
    pdf.set_font(family, size=heading_size)
    line_h = pdf.font_size * 1.3
    for section in sections:
        heading = section.title and clean(section.title)
        top = 0
        if heading:
            top = len(pdf.multi_cell(content_w, text=heading, dry_run=True, output="LINES")) * line_h + gap
        layout = paginate([p.aspect for p in section.photos], content_w, content_h - top, content_h, gap)
        if not layout:
            pages.append((heading, []))
        for i, (start, end, boxes) in enumerate(layout):
            dy = margin + (top if i == 0 else 0)
            placed = [(p, x + margin, y + dy, bw, bh) for p, (x, y, bw, bh) in zip(section.photos[start:end], boxes)]
            pages.append((heading if i == 0 else None, placed))

    px = lambda mm: max(1, round(mm / 25.4 * dpi))
    # -no: no play button on video thumbnails, -l: JPEG quality
    urls = [f"{p.url}=w{px(bw)}-h{px(bh)}-no-l{min(100, max(1, 100 - compression))}" for _, placed in pages for p, _, _, bw, bh in placed]
    with ThreadPoolExecutor(8) as pool:
        images = pool.map(download, urls)
        for heading, placed in pages:
            pdf.add_page()
            if heading:
                pdf.multi_cell(content_w, line_h, heading, align="C")
            for _, x, y, bw, bh in placed:
                pdf.image(io.BytesIO(next(images)), x, y, bw, bh)
    pdf.output(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="public album link (photos.app.goo.gl/... or photos.google.com/share/...)")
    parser.add_argument("-o", "--output", help="PDF name or path, relative to out/ (default: album title)")
    parser.add_argument("-p", "--page", default="A4", help="page size: A3, A4, A5, Letter, Legal (default A4)")
    parser.add_argument("-l", "--landscape", action="store_true", help="landscape instead of portrait")
    parser.add_argument("-q", "--quality", type=int, default=200, help="photo resolution in DPI (default 200)")
    parser.add_argument("-c", "--compression", type=int, default=10,
                        help="JPEG compression 0-99, higher = smaller file (default 10)")
    parser.add_argument("--font", help="TTF font file, needed for titles beyond Latin-1 (e.g. emoji, CJK)")
    args = parser.parse_args()

    title, sections = fetch_album(args.url)
    name = args.output or (re.sub(r"[^\w\- ]+", "", title).strip() or "album") + ".pdf"
    out = Path(__file__).parent / "out" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"{title}: {sum(len(s.photos) for s in sections)} photos, {len(sections)} sections -> {out}")
    build_pdf(title, sections, out, args.page, args.landscape, args.quality, args.compression, args.font)


if __name__ == "__main__":
    main()
