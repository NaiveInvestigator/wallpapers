import re
from pathlib import Path

# 1. Find and sort images
valid_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
images = sorted(
    str(p) for p in Path(".").rglob("*")
    if p.suffix.lower() in valid_exts and not {".git", ".github"} & set(p.parts)
)

# 2. Build the markdown table rows
cols = 4
rows = [
    "| " + " | ".join(f'[<img src="{img}" width="150">]({img})' for img in images[i:i+cols]) + " |"
    for i in range(0, len(images), cols)
]
gallery = "\n".join(["| | | | |", "|---|---|---|---|"] + rows)

# 3. Read, update, and save README.md
readme = Path("README.md")
updated_text = re.sub(
    r"(<!-- gallery:start -->).*?(<!-- gallery:end -->)", 
    rf"\1\n{gallery}\n\2", 
    readme.read_text(encoding="utf-8"), 
    flags=re.DOTALL
)

readme.write_text(updated_text, encoding="utf-8")
