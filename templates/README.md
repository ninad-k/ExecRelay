# templates/ and static/

Repo-root scaffold for any **new** server-rendered (Jinja) tool — not for
`apps/portal-api` (pure JSON, consumed by the separate `apps/portal-web`
Next.js app — do not add HTML rendering there) and not for
`scripts/trade_dashboard.py` (deliberately dependency-free: stdlib
`http.server` with HTML inlined as a Python string, no Jinja, no Flask). Both
of those are intentional architecture choices — see `CLAUDE.md`.

Use this when you're building a small internal HTML tool (an admin page, a
one-off report viewer, a packaged exe per `packaging/dashboard/`) and want
Jinja templates instead of hand-rolled string formatting.

## Layout

```
templates/
  base.html               # shared layout: <head>, topbar, block content
  example_dashboard.html  # extends base.html — copy this as a starting point
static/
  css/dashboard.css       # design tokens matching scripts/trade_dashboard.py's palette
  js/                     # empty — add your own
  img/                    # empty — add your own
```

`static/css/dashboard.css` intentionally mirrors the CSS custom properties
`scripts/trade_dashboard.py` inlines in its own `<style>` block (same
`--color-*` tokens, same light/dark convention via an `html.light` class), so
a new templated tool looks visually consistent with the existing dashboard
without copy-pasting its whole stylesheet.

## Wiring it up (FastAPI)

Every Python service in `apps/` already depends on FastAPI. To mount these
directories in a new app:

```python
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        "example_dashboard.html",
        {"request": request, "positions": []},
    )
```

`Jinja2Templates`/`StaticFiles` need `jinja2` and `python-multipart`
installed — add them to that service's `requirements.txt`, they are not
repo-wide dependencies.

## Wiring it up (Flask)

If a future tool uses Flask instead (e.g. because it embeds the
`MetaTrader5` package the way `packaging/dashboard/` does, and Flask is
already the path of least resistance there):

```python
from flask import Flask, render_template
app = Flask(__name__, template_folder="templates", static_folder="static")
```

## Packaging a Jinja app with PyInstaller

If you build a standalone exe from a Jinja-based tool (see
`packaging/dashboard/build_exe.py` for the general pattern), bundle both
directories as data files and resolve their path at runtime so it works both
as a script and frozen:

```python
import sys, os
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
```

```
pyinstaller your_tool.py --name=YourTool --onefile \
    --add-data="templates;templates" \
    --add-data="static;static"
```
