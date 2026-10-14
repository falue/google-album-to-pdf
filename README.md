# google-album-to-pdf

Turns a public Google Photos album into a clean PDF.

## What it does

- Title page: album cover filling the whole page, album title on top with a white outline
- 3–5 photos per page, laid out to use the space well, never cropped
- In-between album titles start a new page, title on top
- Videos are included as still thumbnails
- Photos are downloaded at exactly the size they are printed
- Comments and reactions are printed under their photo, album comments on the title page
- Uses macOS system fonts (Arial, Apple Color Emoji, Arial Unicode); on other systems pass `--font`

## Usage

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python google2pdf.py https://photos.app.goo.gl/XXXX
```

| Option | Default | Meaning |
|---|---|---|
| `-o name.pdf` | album title | output name, saved in `out/` |
| `-p A4` | A4 | page size: A3, A4, A5, Letter, Legal |
| `-l` | portrait | landscape |
| `-q 200` | 200 | photo resolution in DPI |
| `-c 10` | 10 | JPEG compression 0–99, higher = smaller file |
| `-i` | off | ignore comments and reactions |
| `-n` | off | no album cover on the title page |
| `--font X.ttf` | Arial | text font |

## Compression

Test album with 23 photos, A4, 200 DPI:

| `-c` | JPEG quality | PDF size |
|---|---|---|
| 0 | 100 | 7.3 MB |
| 10 (default) | 90 | 3.6 MB |
| 50 | 50 | 1.8 MB |
| 90 | 10 | 1.2 MB |
