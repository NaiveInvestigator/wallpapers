import re
from pathlib import Path
from urllib.parse import quote

# 1. Find and sort images
valid_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', ".JPG", ".PNG", ".JPEG"}
images = sorted(
    # Use as_posix() to ensure forward slashes for web links (fixes Windows backslashes)
    p.as_posix() for p in Path(".").rglob("*")
    if p.suffix.lower() in valid_exts and not {".git", ".github"} & set(p.parts)
)

# 2. Build the markdown table rows
cols = 4
rows = []
for i in range(0, len(images), cols):
    row_cells = []
    for img in images[i:i+cols]:
        # URL-encode the path to handle spaces, parentheses, commas, etc.
        safe_url = quote(img)
        row_cells.append(f'[<img src="{safe_url}" width="150">]({safe_url})')
    
    # Optional but recommended: pad the last row with empty cells so the Markdown table renders correctly
    while len(row_cells) < cols:
        row_cells.append("")
        
    rows.append("| " + " | ".join(row_cells) + " |")

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
