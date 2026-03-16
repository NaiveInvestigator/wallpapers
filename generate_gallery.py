import os
import re

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")

images = []

for root, dirs, files in os.walk("."):
    # if ".git" in root:
    if ".git" in root or ".github" in root:
        continue

    for f in files:
        if f.lower().endswith(IMAGE_EXT):
            path = os.path.join(root, f).replace("./", "")
            images.append(path)

images.sort()

cols = 4
rows = []

for i in range(0, len(images), cols):
    chunk = images[i:i+cols]

    preview_row = "| " + " | ".join(
        f'[<img src="{img}" width="150">]({img})' for img in chunk
    ) + " |"

    rows.append(preview_row)

table = "\n".join(rows)

gallery = f"""
| | | | |
|---|---|---|---|
{table}
"""

with open("README.md") as f:
    readme = f.read()

pattern = r"<!-- gallery:start -->.*?<!-- gallery:end -->"

replacement = f"""<!-- gallery:start -->
{gallery}
<!-- gallery:end -->"""

readme = re.sub(pattern, replacement, readme, flags=re.S)

with open("README.md", "w") as f:
    f.write(readme)
