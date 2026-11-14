import io
import os

BASE = os.path.dirname(os.path.abspath(__file__))
FRAG = os.path.join(BASE, "web_src", "robot-diy-blocks.html")
LUCIDE = os.path.join(BASE, "web", "vendor", "lucide-1.17.0.js")
BRIDGE = os.path.join(BASE, "web_bridge.js")
OUT = os.path.join(BASE, "web", "index.html")

frag = io.open(FRAG, encoding="utf-8").read()
lucide = io.open(LUCIDE, encoding="utf-8").read()
bridge = io.open(BRIDGE, encoding="utf-8").read()

css = """
:root{
  --font-size-base:14px;
  --background:#0b0f17; --foreground:#e7ecf3;
  --card:#121826; --card-foreground:#e7ecf3;
  --popover:#161d2c; --popover-foreground:#e7ecf3;
  --primary:#38bdf8; --primary-foreground:#04121b;
  --secondary:#1b2434; --secondary-foreground:#e7ecf3;
  --muted:#1a2231; --muted-foreground:#9aa7b8;
  --accent:#1d2a3d; --accent-foreground:#e7ecf3;
  --destructive:#ef4444; --border:#283246; --input:#283246; --ring:#38bdf8;
  --blue:#60a5fa; --orange:#fb923c; --green:#34d399; --red:#f87171; --purple:#c084fc; --yellow:#fbbf24;
}
*{box-sizing:border-box}
html{color-scheme:dark}
html,body{margin:0;padding:0;background:var(--background);color:var(--foreground);
  font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  font-size:var(--font-size-base);line-height:1.5;font-weight:400}
body{padding:16px;max-width:1500px;margin:0 auto}
h2{font-size:18px;font-weight:500;margin:0}
.text-small{font-size:12px}
.text-muted{color:var(--muted-foreground)}
.card{background:var(--card);color:var(--card-foreground);border:1px solid var(--border);border-radius:12px;padding:12px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:8px;border:1px solid var(--border);
  background:var(--secondary);color:var(--secondary-foreground);font:inherit;cursor:pointer}
.btn:hover{background:var(--accent)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:var(--primary);color:var(--primary-foreground);border-color:var(--primary)}
.btn-primary:hover{filter:brightness(1.05)}
.btn-ghost{background:transparent;border-color:transparent}
.form-control,.form-select{width:100%;padding:6px 8px;border:1px solid var(--input);border-radius:8px;
  background:var(--background);color:var(--foreground);font:inherit}
.progress{height:8px;background:var(--muted);border-radius:999px;overflow:hidden}
.progress-bar{height:100%;background:var(--primary);border-radius:999px;transition:width .12s linear}
.viz-badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;
  background:var(--accent);color:var(--accent-foreground);font-size:12px}
"""

html = (
    "<!doctype html>\n"
    "<html lang=\"zh-CN\">\n<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>机器人 DIY 指令卡 · G1</title>\n"
    "<style>" + css + "</style>\n"
    "</head>\n<body>\n"
    "<script>" + lucide + "</script>\n"
    "<script>window.ROBOT_CARD_REMOTE = true;</script>\n"
    + frag + "\n"
    "<script>" + bridge + "</script>\n"
    "</body>\n</html>\n"
)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
io.open(OUT, "w", encoding="utf-8").write(html)
print("written", OUT, len(html))
